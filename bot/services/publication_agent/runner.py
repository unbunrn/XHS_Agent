"""Bounded model/tool loop for one publication task."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ...adapters.types import BudgetExceeded, LLMError
from ...config import Config
from ...timeutil import Clock, parse_iso, to_iso
from .contracts import AgentDecision, PublicationBudgetExceeded, TaskError
from .store import PublicationStore
from .tools import PublicationContext, PublicationTools

SYSTEM = """你负责把一篇已批准的小红书作品发布到正确账号并确认结果。
每轮只选择一个工具；工具结果是唯一事实来源，不得编造 published 或“确定未发布”。
未知结果先只读对账；不得改批准内容、跳过批准/配额或自动重发。需要主人操作时提出一个具体问题并等待。
只输出结构化动作和简短目的。"""


@dataclass
class RunResult:
    task_id: int
    state: str
    steps: int
    error: str | None = None


class PublicationRunner:
    def __init__(self, db, config: Config, clock: Clock, llm, runtime):
        self.db = db
        self.config = config
        self.clock = clock
        self.llm = llm
        self.runtime = runtime
        self.store = PublicationStore(db, clock)
        self.tools = PublicationTools(runtime)

    def run(
        self, task_id: int, *, diagnose_only: bool = False,
        reconcile_only: bool = False,
    ) -> RunResult:
        owner = f"publication-runner:{os.getpid()}:{id(self)}"
        if not self.store.acquire_lease(task_id, owner, seconds=600):
            return RunResult(task_id, "busy", 0, "another publication worker holds the lease")
        try:
            return self._run_locked(
                task_id, diagnose_only=diagnose_only, reconcile_only=reconcile_only,
            )
        finally:
            self.store.release_lease(task_id, owner)

    def _run_locked(
        self, task_id: int, *, diagnose_only: bool = False,
        reconcile_only: bool = False,
    ) -> RunResult:
        task = self.store.get(task_id=task_id)
        if task is None:
            return RunResult(task_id, "missing", 0, "task not found")
        if reconcile_only:
            return self._reconcile_unknown(task)
        if task.cancel_requested or task.state == "cancelled":
            unresolved = self._latest_unresolved_attempt(task.id)
            if unresolved is not None:
                if diagnose_only:
                    # A diagnostic may observe a pending result, but applying
                    # reconciliation or changing cancellation state is a
                    # lifecycle write and belongs to a normal run.
                    self.runtime.preview_result(task)
                    return self._diagnosis_stop(
                        task, 0, "存在未知发布结果，需等待正式对账"
                    )
                # A cancellation cannot erase an in-flight/unknown send. Reconcile
                # first; a late success wins and must never be reported as cancelled.
                self.runtime.check_result(task)
                current = self.store.get(task_id=task.id)
                if current is not None and current.state in {"published", "simulated"}:
                    return RunResult(task.id, current.state, 0, "late success after cancellation")
                self.store.update(
                    task.id, state="waiting_owner",
                    wait_reason="cancellation pending: publish result is still unknown; reconcile with the platform",
                )
                return RunResult(task.id, "waiting_owner", 0, "cancellation pending reconciliation")
            if diagnose_only:
                return self._diagnosis_stop(task, 0, "任务已请求取消，诊断不改变取消状态")
            self.store.update(task.id, state="cancelled", wait_reason="cancelled before execution")
            return RunResult(task.id, "cancelled", 0)
        if task.state == "waiting_owner" and not diagnose_only:
            if not self._has_unresolved_attempt(task.id):
                return RunResult(task.id, task.state, 0)
        if task.state in {"published", "cancelled", "simulated"}:
            return RunResult(task.id, task.state, 0)
        if not diagnose_only and task.state in {"paused", "waiting_login", "waiting_approval"}:
            return RunResult(task.id, task.state, 0)
        if task.state == "retry_wait" and not diagnose_only:
            next_wake = parse_iso(task.next_wake_at) if task.next_wake_at else None
            if next_wake is not None and next_wake > self.clock.now():
                return RunResult(task.id, "retry_wait", 0, "retry is not due")
        if not diagnose_only and task.state not in {"ready", "diagnosing", "retry_wait", "waiting_owner", "submitting", "verifying"}:
            return RunResult(task.id, task.state, 0)
        package = self.store.package(task)
        self.store.batch_start(task.id)
        allowed = self._allowed_actions(task, allow_publish=not diagnose_only, diagnose_only=diagnose_only)
        max_steps = max(1, int(self.config.get("publication_agent.max_steps", 6)))
        errors = 0
        steps = 0
        if not diagnose_only:
            self.store.update(task.id, state="diagnosing")
        for _ in range(max_steps):
            task = self.store.get(task_id=task.id)
            if task is None:
                return RunResult(task_id, "missing", steps, "task disappeared")
            if task.cancel_requested or task.state == "cancelled":
                unresolved = self._latest_unresolved_attempt(task.id)
                if unresolved is not None:
                    if diagnose_only:
                        self.runtime.preview_result(task)
                        return self._diagnosis_stop(
                            task, steps, "存在未知发布结果，需等待正式对账"
                        )
                    self.runtime.check_result(task)
                    current = self.store.get(task_id=task.id)
                    if current is not None and current.state in {"published", "simulated"}:
                        return RunResult(task.id, current.state, steps, "late success after cancellation")
                    self.store.update(
                        task.id, state="waiting_owner",
                        wait_reason="cancellation pending: publish result is still unknown; reconcile with the platform",
                    )
                    return RunResult(task.id, "waiting_owner", steps, "cancellation pending reconciliation")
                if diagnose_only:
                    return self._diagnosis_stop(task, steps, "任务已请求取消，诊断不改变取消状态")
                self.store.update(task.id, state="cancelled", wait_reason="cancelled during execution")
                return RunResult(task.id, "cancelled", steps)
            if task.state in {"published", "cancelled", "simulated"}:
                return RunResult(task.id, task.state, steps)
            if not diagnose_only and task.state in {"paused", "waiting_owner", "waiting_login", "waiting_approval"}:
                return RunResult(task.id, task.state, steps)
            allowed = self._allowed_actions(task, allow_publish=not diagnose_only, diagnose_only=diagnose_only)
            obs = self._observation(task, package, allowed)
            max_model_calls = int(self.config.get("publication_agent.max_model_calls", 6))
            max_daily_calls = int(self.config.get("publication_agent.max_daily_model_calls", 100))
            batch_calls, _ = self.store.batch_usage(task.id)
            if max_model_calls >= 0 and batch_calls >= max_model_calls:
                reason = f"publication model budget exhausted ({batch_calls}/{max_model_calls})"
                if diagnose_only:
                    return self._diagnosis_stop(task, steps, "本轮模型预算已用尽")
                self.store.update(task.id, state="waiting_owner", wait_reason=reason)
                self.runtime.notify(task, f"发布任务 {task.code} 已达到本轮模型预算，等待主人继续。")
                return RunResult(task.id, "waiting_owner", steps, reason)

            def _reserve() -> None:
                self.store.reserve_model_attempt(
                    task.id, max_batch=max_model_calls, max_daily=max_daily_calls,
                )

            try:
                response = self.llm.complete_json(
                    purpose="publication_decide",
                    payload={"system": SYSTEM, "observation": obs, "tools": self.tools.schemas()},
                    max_output_tokens=700,
                    before_attempt=_reserve,
                )
                raw = getattr(response, "data", response)
                decision = AgentDecision.from_dict(raw, actions=set(self.tools.names()))
                self.store.add_step(
                    task.id, action="model_decision", args={},
                    intent_summary="model decision", observation={}, evidence_refs=[], status="ok",
                )
            except (BudgetExceeded, PublicationBudgetExceeded) as exc:
                reason = f"model budget: {exc}"
                if diagnose_only:
                    return self._diagnosis_stop(task, steps, "模型预算不可用")
                self.store.update(task.id, state="waiting_owner", wait_reason=reason)
                self.runtime.notify(task, f"发布任务 {task.code} 已达到模型预算，等待主人继续。")
                return RunResult(task.id, "waiting_owner", steps, str(exc))
            except (LLMError, ValueError, TypeError) as exc:
                errors += 1
                self.store.add_step(task.id, action="invalid_decision", args={}, intent_summary="", observation={"error": str(exc)}, evidence_refs=[], status="failed")
                if errors >= int(self.config.get("publication_agent.max_invalid_decisions", 2)):
                    if diagnose_only:
                        return self._diagnosis_stop(task, steps, "模型连续给出无效动作")
                    self.store.update(task.id, state="waiting_owner", wait_reason=f"invalid model decision: {exc}")
                    return RunResult(task.id, "waiting_owner", steps, str(exc))
                continue
            steps += 1
            ctx = PublicationContext(store=self.store, task=task, package=package, runtime=self.runtime, config=self.config, clock=self.clock, allowed_actions=allowed, diagnose_only=diagnose_only)
            if decision.action not in allowed:
                result = {"ok": False, "error": "action_not_allowed", "observation": {"allowed": allowed}}
            else:
                result = self.tools.execute(decision.action, ctx, decision.args)
            self.store.add_step(
                task.id,
                action=decision.action,
                args=decision.args,
                intent_summary=decision.intent_summary,
                observation=result.get("observation"),
                evidence_refs=decision.evidence_refs,
                status="ok" if result.get("ok") else "failed",
            )
            task = self.store.get(task_id=task.id)
            if task is None:
                return RunResult(task_id, "missing", steps, "task disappeared")
            if result.get("terminal") or task.state in {"published", "simulated", "cancelled"}:
                return RunResult(task.id, task.state, steps, None if result.get("ok") else result.get("error"))
            if diagnose_only and result.get("error") in {"diagnosis_action_not_allowed", "action_not_allowed"}:
                return self._diagnosis_stop(task, steps, "诊断尝试了只读模式不允许的动作")
            if not diagnose_only and task.state in {"waiting_owner", "waiting_login", "waiting_approval", "retry_wait"}:
                return RunResult(task.id, task.state, steps, None if result.get("ok") else result.get("error"))
        if diagnose_only:
            return RunResult(task.id, task.state, steps, "diagnosis complete")
        self.store.update(task.id, state="waiting_owner", wait_reason="publication agent step limit reached")
        return RunResult(task.id, "waiting_owner", steps, "step limit reached")

    def _diagnosis_stop(self, task, steps: int, reason: str) -> RunResult:
        label = {
            "paused": "暂停",
            "waiting_owner": "等待负责人处理",
            "waiting_login": "等待登录",
            "waiting_approval": "等待审批",
            "retry_wait": "等待重试",
            "cancelled": "已取消",
        }.get(task.state, task.state)
        self.runtime.notify(
            task,
            f"诊断未完成：{reason}。作品仍保持{label}状态，未改变发布安排。",
        )
        return RunResult(task.id, task.state, steps, reason)

    def _has_unresolved_attempt(self, task_id: int) -> bool:
        return self.db.fetchone(
            "SELECT 1 FROM publish_attempts WHERE publication_task_id=? LIMIT 1",
            (int(task_id),),
        ) is not None

    def _latest_unresolved_attempt(self, task_id: int):
        return self.db.fetchone(
            "SELECT * FROM publish_attempts WHERE publication_task_id=? "
            "AND state IN ('unknown','publishing') ORDER BY id DESC LIMIT 1",
            (int(task_id),),
        )

    def _reconcile_unknown(self, task) -> RunResult:
        """Advance one queued, state-writing reconciliation attempt.

        This path never calls the model and never submits. It is used by an
        explicit owner repair request and by its own bounded retry jobs.
        """
        if task.state in {"published", "simulated", "cancelled"}:
            return RunResult(task.id, task.state, 0)
        if self._latest_unresolved_attempt(task.id) is None:
            return RunResult(task.id, task.state, 0, "no unresolved publish attempt")
        self.runtime.check_result(task)
        current = self.store.get(task_id=task.id)
        if current is None:
            return RunResult(task.id, "missing", 0, "task disappeared")
        if current.state in {"published", "simulated"}:
            if current.state == "published":
                row = self.db.fetchone(
                    "SELECT external_post_id, external_url FROM publish_attempts "
                    "WHERE publication_task_id=? AND state='published' ORDER BY id DESC LIMIT 1",
                    (task.id,),
                )
                note_id = str(row["external_post_id"] or "").strip() if row is not None else ""
                self.runtime.notify(
                    current,
                    f"发布成功：任务 {current.code} 已在平台确认"
                    + (f"，笔记 ID {note_id}。" if note_id else "。"),
                )
            return RunResult(task.id, current.state, 0, "late result reconciled")
        attempts = int(self.db.scalar(
            "SELECT COUNT(*) FROM publication_agent_steps "
            "WHERE task_id=? AND action='reconcile_wait'",
            (task.id,),
        ) or 0)
        if attempts >= 3:
            self.store.update(
                task.id, state="waiting_owner",
                wait_reason="publish outcome still unknown after bounded reconciliation; owner review required",
            )
            self.runtime.notify(
                current,
                f"发布任务 {current.code} 多次只读核对仍未找到可信结果，已停止自动核对；请主人在平台核实。",
            )
            return RunResult(task.id, "waiting_owner", attempts, "reconciliation exhausted")
        delay = 30 * (2 ** attempts)
        next_wake = self.clock.now() + timedelta(seconds=delay)
        self.store.update(
            task.id, state="waiting_owner",
            next_wake_at=to_iso(next_wake),
            wait_reason="publish outcome unknown; bounded reconciliation scheduled",
        )
        self.store.add_step(
            task.id, action="reconcile_wait",
            args={"delay_seconds": delay, "attempt": attempts + 1},
            intent_summary="read-only publication result reconciliation",
            observation={"state": current.state, "attempts": attempts + 1},
            evidence_refs=[], status="waiting",
        )
        self.runtime.schedule(task.id, next_wake, reconcile_only=True)
        self.runtime.notify(
            current,
            f"暂未确认发布结果（任务 {current.code}）；将在 {delay} 秒后继续只读核对，不会重发。",
        )
        return RunResult(task.id, "waiting_owner", attempts + 1, "reconciliation scheduled")

    def _allowed_actions(self, task, *, allow_publish: bool = True, diagnose_only: bool = False) -> list[str]:
        if diagnose_only:
            # Diagnosis is a read-only capability.  It may inspect the outside
            # world and record what it sees, but it cannot change the publish
            # lifecycle or schedule a normal (publish-capable) job.
            return [
                "inspect_task",
                "check_bridge_health",
                "check_login",
                "verify_assets",
                "inspect_failure",
                "check_publish_result",
                "notify_owner",
                "schedule_resume",
            ]
        base = ["inspect_task", "check_bridge_health", "check_login", "verify_assets", "restore_approved_assets", "inspect_failure", "check_publish_result", "request_owner_action", "schedule_resume", "notify_owner"]
        if allow_publish and task.state in {"ready", "diagnosing", "retry_wait"}:
            base.append("publish_approved_artifact")
        if task.active_attempt_id or task.state in {"submitting", "verifying"}:
            base.append("check_publish_result")
        return base

    def _observation(self, task, package, allowed) -> dict[str, Any]:
        attempts = self.db.fetchall(
            "SELECT id,attempt_no,state,error,external_post_id,created_at FROM publish_attempts "
            "WHERE publication_task_id=? ORDER BY id DESC LIMIT 8", (task.id,)
        )
        return {
            "task": task.as_dict(),
            "package": {k: package.get(k) for k in ("approval_code", "draft_id", "draft_version", "title", "body_sha256", "images_sha256", "images", "scheduled_at", "expires_at")},
            "history": self.store.steps(task.id, limit=8),
            "attempts": [dict(a) for a in attempts],
            "owner_request": self.store.open_owner_request(task.id),
            "allowed_actions": allowed,
            "budget": {
                "model_calls_in_batch": self.store.batch_usage(task.id)[0],
                "max_model_calls": int(self.config.get("publication_agent.max_model_calls", 6)),
                "max_daily_model_calls": int(self.config.get("publication_agent.max_daily_model_calls", 100)),
                "steps_remaining": max(0, int(self.config.get("publication_agent.max_steps", 6)) - len(self.store.steps(task.id, limit=200))),
            },
        }

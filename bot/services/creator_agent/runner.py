"""Model-driven Creator Agent V2 loop.

The loop is intentionally generic: the model chooses exactly one registered
tool per iteration; this module validates and bounds that choice, persists the
step before/after execution, and stops on terminal tools or hard limits.  It
does not know how to search, write, or publish.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from bot.adapters.types import BudgetExceeded
from bot.timeutil import to_iso

from .contracts import AgentDecision, ArtifactVersion, CreativeBrief
from .context import AgentContext
from .registry import ToolError, ToolRegistry, ToolResult
from .store import AgentStore

TERMINAL_TOOLS = {"submit_for_approval", "ask_owner"}
DEFAULT_LIMITS = {
    "enabled": False,
    "max_steps": 8,
    "max_revision_steps": 6,
    "max_review_cycles": 2,
    "max_searches": 5,
    "max_physical_fetches": 6,
    "max_model_calls": 14,
    "max_invalid_decisions": 2,
    "max_no_progress_steps": 2,
    "max_run_seconds": 600,
}


class DecisionLLM(Protocol):
    def complete_json(self, **kwargs: Any) -> Any: ...


@dataclass
class AgentResult:
    run_key: str
    status: str
    artifact: ArtifactVersion | None = None
    decision: AgentDecision | None = None
    error: str = ""
    steps: int = 0


@dataclass
class _State:
    counters: dict[str, int] = field(
        default_factory=lambda: {
            "steps": 0,
            "model_calls": 0,
            "searches": 0,
            "physical_fetches": 0,
            "review_cycles": 0,
            "invalid_decisions": 0,
            "no_progress": 0,
        }
    )
    last_signature: str = ""
    no_progress: int = 0
    started_at: float = field(default_factory=time.monotonic)


def _stable(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()


class AgentRunner:
    def __init__(
        self,
        *,
        db,
        config,
        clock,
        llm: DecisionLLM,
        registry: ToolRegistry,
        context: Any = None,
        policy_version: str = "creator-v2",
    ) -> None:
        self.db = db
        self.config = config
        self.clock = clock
        self.llm = llm
        self.registry = registry
        self.context = context
        self.store = AgentStore(db, clock)
        self.policy_version = policy_version
        self.limits = self._limits(config)
        if context is None:
            context = AgentContext(brief=CreativeBrief("", "", "manual", ""), runtime=None, store=self.store)
        self.context = context

    def _limits(self, config) -> dict[str, int]:
        raw = config.get("creator_agent", {}) if hasattr(config, "get") else {}
        out = dict(DEFAULT_LIMITS)
        for key, default in DEFAULT_LIMITS.items():
            if key == "enabled":
                continue
            try:
                out[key] = int(raw.get(key, default))
            except (TypeError, ValueError):
                out[key] = default
        out["enabled"] = bool(raw.get("enabled", DEFAULT_LIMITS["enabled"]))
        return out

    def run(self, brief: CreativeBrief) -> AgentResult:
        if not self.limits["enabled"]:
            return AgentResult(run_key=brief.run_key, status="disabled")
        run = self.store.ensure_run(brief, policy_version=self.policy_version)
        self.context.brief = brief
        if run["status"] == "deferred" and bool(self.config.get("creator_agent.resume_deferred", False)):
            checkpoint = dict(run.get("checkpoint") or {})
            reason = str(checkpoint.get("deferred_reason") or "")
            if reason in {"model_calls", "budget"}:
                old_limit = int(checkpoint.get("model_limit_at_defer") or 0)
                new_limit = int(self.config.get("creator_agent.max_model_calls", 14))
                if not bool(self.config.get("creator_agent.resume_budget_deferred", False)) and new_limit <= old_limit:
                    return AgentResult(
                        run_key=brief.run_key,
                        status="deferred",
                        error="model call budget exhausted; explicit new budget/window required",
                        steps=len(self.store.steps(brief.run_key, 100)),
                    )
            if int(run.get("generation") or 1) != int(brief.generation):
                return AgentResult(run_key=brief.run_key, status="failed", error="stale generation")
            checkpoint["elapsed_s"] = 0
            checkpoint["resumed"] = True
            self.store.update_run(brief.run_key, status="running", checkpoint=checkpoint)
            run = self.store.get_run(brief.run_key) or run
        elif run["status"] in {"completed", "deferred", "failed", "cancelled"}:
            return AgentResult(
                run_key=brief.run_key,
                status=str(run["status"]),
                steps=len(self.store.steps(brief.run_key, 100)),
            )

        checkpoint = run.get("checkpoint") or {}
        state = _State(counters={**{k: 0 for k in _State().counters}, **run.get("counters", {})})
        state.last_signature = str(checkpoint.get("last_signature") or "")
        state.no_progress = int(checkpoint.get("no_progress") or 0)
        state.started_at = time.monotonic() - float(checkpoint.get("elapsed_s") or 0)

        history = self.store.steps(brief.run_key, 100)
        max_steps = self.limits["max_steps"]
        if brief.origin == "revision":
            max_steps = self.limits["max_revision_steps"]

        def reserve_decision_attempt() -> None:
            if state.counters["model_calls"] >= self.limits["max_model_calls"]:
                raise BudgetExceeded("creator agent model call limit reached")
            state.counters["model_calls"] += 1
            state.counters["_runtime_model_calls"] = int(state.counters["model_calls"])
            # Persist the reservation before the HTTP attempt: a crash after
            # the provider received the request must not refund the call.
            self.store.update_run(brief.run_key, counters=state.counters)
            runtime = getattr(self.context, "runtime", None)
            if runtime is not None:
                runtime.model_calls = max(
                    int(getattr(runtime, "model_calls", 0)),
                    int(state.counters["model_calls"]),
                )
                runtime.model_limit = int(self.limits["max_model_calls"])

        while True:
            if state.counters["steps"] >= max_steps:
                return self._finish(brief, "failed", state, error="step limit reached")
            if time.monotonic() - state.started_at > self.limits["max_run_seconds"]:
                return self._finish(brief, "deferred", state, error="run time limit reached")
            if state.counters["model_calls"] >= self.limits["max_model_calls"]:
                return self._finish(brief, "deferred", state, error="model call limit reached")

            observation = self._observation(brief, history)
            calls_before = int(state.counters["model_calls"])
            try:
                observation["system"] = _agent_system()
                raw = self.llm.complete_json(
                    purpose="agent_decide",
                    payload=observation,
                    max_output_tokens=1800,
                    before_attempt=reserve_decision_attempt,
                )
            except BudgetExceeded as exc:
                return self._finish(brief, "deferred", state, error=str(exc))
            except Exception as exc:
                return self._finish(brief, "failed", state, error=f"agent model failed: {exc}")

            if hasattr(raw, "data"):
                raw = raw.data
            if int(state.counters["model_calls"]) == calls_before:
                # Some test doubles / alternative LLM implementations do not
                # invoke the per-attempt callback.  Count one logical attempt
                # rather than silently treating the decision as free.
                state.counters["model_calls"] += 1
                state.counters["_runtime_model_calls"] = int(state.counters["model_calls"])
                self.store.update_run(brief.run_key, counters=state.counters)
            try:
                decision = AgentDecision.from_dict(raw)
            except ValueError as exc:
                state.counters["invalid_decisions"] += 1
                self._step(
                    brief,
                    state,
                    action="invalid_decision",
                    args={"raw": raw},
                    status="invalid",
                    result={"error": str(exc)},
                )
                if state.counters["invalid_decisions"] > self.limits["max_invalid_decisions"]:
                    return self._finish(brief, "failed", state, error=f"invalid decision limit: {exc}")
                continue

            try:
                spec = self.registry.validate(decision.action, decision.args)
            except ToolError as exc:
                state.counters["invalid_decisions"] += 1
                self._step(
                    brief,
                    state,
                    action=decision.action,
                    args=decision.args,
                    status="invalid",
                    result={"error": str(exc)},
                )
                if state.counters["invalid_decisions"] > self.limits["max_invalid_decisions"]:
                    return self._finish(brief, "failed", state, error=f"invalid decision limit: {exc}")
                continue

            if spec.max_calls is not None and self._count_action(history, decision.action) >= spec.max_calls:
                state.counters["invalid_decisions"] += 1
                if state.counters["invalid_decisions"] > self.limits["max_invalid_decisions"]:
                    return self._finish(brief, "failed", state, error="invalid decision limit reached")
                continue
            if decision.action == "search" and state.counters["searches"] >= self.limits["max_searches"]:
                state.counters["invalid_decisions"] += 1
                if state.counters["invalid_decisions"] > self.limits["max_invalid_decisions"]:
                    return self._finish(brief, "failed", state, error="invalid decision limit reached")
                continue
            if decision.action == "review_artifact" and state.counters["review_cycles"] >= self.limits["max_review_cycles"]:
                state.counters["invalid_decisions"] += 1
                if state.counters["invalid_decisions"] > self.limits["max_invalid_decisions"]:
                    return self._finish(brief, "failed", state, error="invalid decision limit reached")
                continue
            if decision.action in {"submit_for_approval", "revise_artifact", "render_images", "inspect_layout", "review_artifact"}:
                latest = self.context.latest_artifact()
                if latest is None:
                    state.counters["invalid_decisions"] += 1
                    if state.counters["invalid_decisions"] > self.limits["max_invalid_decisions"]:
                        return self._finish(brief, "failed", state, error="invalid decision limit reached")
                    continue
                if decision.base_version is not None and int(decision.base_version) != int(latest.version):
                    state.counters["invalid_decisions"] += 1
                    if state.counters["invalid_decisions"] > self.limits["max_invalid_decisions"]:
                        return self._finish(brief, "failed", state, error="invalid decision limit reached")
                    continue
                if decision.action == "revise_artifact":
                    phase_problem = self._revise_phase_problem(history, latest)
                    if phase_problem:
                        state.counters["invalid_decisions"] += 1
                        self._step(
                            brief,
                            state,
                            action=decision.action,
                            args=decision.args,
                            status="invalid",
                            result={"error": phase_problem},
                        )
                        if state.counters["invalid_decisions"] > self.limits["max_invalid_decisions"]:
                            return self._finish(brief, "failed", state, error=phase_problem)
                        continue
            if decision.referenced_material_ids:
                known = {m.material_id for m in self.context.materials}
                if not set(decision.referenced_material_ids).issubset(known):
                    state.counters["invalid_decisions"] += 1
                    if state.counters["invalid_decisions"] > self.limits["max_invalid_decisions"]:
                        return self._finish(brief, "failed", state, error="invalid decision limit reached")
                    continue

            signature = _stable(
                {
                    "action": decision.action,
                    "args": decision.args,
                    "base": decision.base_version,
                    "generation": brief.generation,
                }
            )
            if signature == state.last_signature:
                state.no_progress += 1
            else:
                state.no_progress = 0
                state.last_signature = signature
            if state.no_progress >= self.limits["max_no_progress_steps"]:
                return self._finish(brief, "failed", state, error="no progress limit reached")

            idem = _stable(
                [brief.run_key, brief.generation, len(history), decision.action, decision.args]
            )
            # Journal the attempted action before any side effect.  If the
            # process dies mid-tool, recovery can see the unresolved step and
            # the submit bridge itself must still deduplicate the side effect.
            step_seq = self._step(
                brief,
                state,
                action=decision.action,
                args=decision.args,
                intent=decision.intent_summary,
                expected=decision.expected_result,
                result={"state": "running", "idempotency_key": idem},
                status="running",
                idem=idem,
            )
            try:
                result = self.registry.execute(decision.action, decision.args, self.context)
            except ToolError as exc:
                result = ToolResult(ok=False, error=str(exc))
            except Exception as exc:
                result = ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")

            if decision.action == "review_artifact":
                state.counters["review_cycles"] += 1
            if decision.action in {"search"}:
                state.counters["searches"] += 1
            if decision.action in {"search", "read_source"}:
                state.counters["physical_fetches"] += int(result.data.get("physical_fetches", 0))
            runtime = getattr(self.context, "runtime", None)
            if runtime is not None:
                seen = int(state.counters.get("_runtime_model_calls", 0))
                current = int(getattr(runtime, "model_calls", 0))
                if current > seen:
                    state.counters["model_calls"] += current - seen
                    state.counters["_runtime_model_calls"] = current

            step_result = dict(result.data) if result.ok else {
                "error": result.error,
                **dict(result.data or {}),
            }
            self.store.update_step(
                brief.run_key,
                step_seq,
                result=step_result,
                status="ok" if result.ok else "failed",
            )
            history = self.store.steps(brief.run_key, 100)

            if result.artifact is not None:
                artifact = result.artifact
                if not isinstance(artifact, ArtifactVersion):
                    return self._finish(brief, "failed", state, error="tool returned invalid artifact")
                if artifact.run_key != brief.run_key:
                    return self._finish(brief, "failed", state, error="artifact run mismatch")
                artifact.brief_hash = artifact.brief_hash or brief.brief_hash
                self.store.save_artifact(artifact)
                history = self.store.steps(brief.run_key, 100)

            if result.terminal == "submit":
                artifact = self.store.latest_artifact(brief.run_key)
                if artifact is None:
                    return self._finish(brief, "failed", state, error="submit without artifact")
                artifact.status = "submitted"
                self.store.save_artifact(artifact)
                return self._finish(brief, "completed", state, artifact=artifact)
            if result.terminal == "clarify":
                return self._finish(brief, "needs_owner", state)
            if result.terminal == "defer":
                return self._finish(brief, "deferred", state, error=result.error or "agent deferred")
            if not result.ok and result.error:
                # A tool failure is not automatically fatal: the model may recover.
                state.counters["invalid_decisions"] += 0

            self._save_checkpoint(brief, state)

    def _count_action(self, history: list[dict[str, Any]], action: str) -> int:
        return sum(1 for step in history if step.get("action") == action and step.get("status") == "ok")

    def _revise_phase_problem(
        self, history: list[dict[str, Any]], latest: ArtifactVersion
    ) -> str:
        """Prevent the revise-only loop that runs out of steps before rendering.

        A revision is useful once per review, but repeatedly rewriting an
        artifact with no rendered images cannot produce an approval.  The
        program, not the model, enforces the phase boundary; the model still
        decides the eventual wording and layout.
        """
        consecutive_revise = 0
        last_review_requires_images = False
        last_review_has_other_issues = False
        render_attempted_since_draft = False
        for step in reversed(history):
            action = str(step.get("action") or "")
            if action == "render_images" and step.get("status") in {"ok", "failed"}:
                render_attempted_since_draft = True
            if step.get("status") != "ok":
                continue
            if action == "revise_artifact":
                consecutive_revise += 1
                continue
            if action == "review_artifact":
                res = step.get("result") or {}
                review_data = res.get("review") if isinstance(res, dict) else {}
                issues = review_data.get("issues") if isinstance(review_data, dict) else []
                blob = json.dumps(res, ensure_ascii=False).lower()
                last_review_requires_images = "no rendered images" in blob
                if isinstance(issues, list):
                    last_review_has_other_issues = any(
                        "no rendered images" not in json.dumps(item, ensure_ascii=False).lower()
                        for item in issues
                    )
                break
            if action in {"render_images", "draft_artifact", "inspect_layout"}:
                break
        if (
            last_review_requires_images
            and not latest.images
            and not last_review_has_other_issues
            and not render_attempted_since_draft
        ):
            return "review requires rendered images; call render_images before revise_artifact"
        if consecutive_revise >= 1:
            return "revision already attempted; call review_artifact or render_images before another revise_artifact"
        return ""

    def _phase_hint(
        self, history: list[dict[str, Any]], latest: ArtifactVersion | None
    ) -> str:
        if latest is None:
            return "draft_artifact"
        if latest.images:
            return ""
        for step in reversed(history):
            if step.get("status") != "ok":
                continue
            if step.get("action") == "review_artifact":
                blob = json.dumps(step.get("result") or {}, ensure_ascii=False).lower()
                if "no rendered images" in blob:
                    return "render_images before another revise_artifact or review_artifact"
                break
            if step.get("action") == "revise_artifact":
                return "render_images or review_artifact before another revise_artifact"
        return ""

    def _artifact_view(self, artifact: ArtifactVersion | None, *, body_limit: int = 6000) -> dict[str, Any] | None:
        if artifact is None:
            return None
        data = artifact.as_dict()
        body = str(data.get("body") or "")
        data["body"] = body[:body_limit]
        data["body_truncated"] = len(body) > body_limit
        images = []
        for index, image in enumerate(artifact.images or [], 1):
            item = dict(image)
            item["asset_id"] = str(item.get("asset_id") or f"image:{index}")
            item.setdefault("ordinal", index)
            images.append(item)
        data["images"] = images
        data["claims"] = [
            {
                "claim_id": c.claim_id,
                "text": c.text,
                "status": c.status,
                "material_id": c.material_id,
                "quote": (c.quote or "")[:300],
            }
            for c in (artifact.claims or [])
        ]
        return data

    def _observation(self, brief: CreativeBrief, history: list[dict[str, Any]]) -> dict[str, Any]:
        compact = []
        for step in history[-8:]:
            compact.append(
                {
                    "seq": step.get("seq"),
                    "action": step.get("action"),
                    "args": step.get("args"),
                    "status": step.get("status"),
                    "result": step.get("result"),
                }
            )
        current = self.store.latest_artifact(brief.run_key)
        parent = getattr(self.context, "base_artifact", None)
        materials = []
        for material in getattr(self.context, "materials", []) or []:
            materials.append(
                {
                    "material_id": material.material_id,
                    "url": material.url,
                    "title": material.title,
                    "role": material.role,
                    "published_at": material.published_at,
                    "content_type": material.content_type,
                    "quote": (material.quote or "")[:500],
                }
            )
        return {
            "brief": brief.as_dict(),
            "tools": self.registry.schemas(),
            "recent_steps": compact,
            "current_artifact": self._artifact_view(current),
            "parent_artifact": self._artifact_view(parent) if parent is not None else None,
            # Kept for older prompts: never expose a parent artifact as if it
            # were a new submission of this run.
            "latest_artifact": self._artifact_view(current) if current is not None else None,
            "materials": materials,
            "phase_hint": self._phase_hint(history, current),
            "revision": {
                "instruction": brief.instruction,
                "must_keep": list(brief.must_keep or []),
                "may_change": list(brief.may_change or []),
            },
            "limits": {k: v for k, v in self.limits.items() if k != "enabled"},
            "instruction": (
                "Choose exactly one tool. Use only listed material IDs and asset IDs. "
                "For a revision, use parent_artifact as the base and preserve only "
                "the assets named in revision.must_keep; never submit the parent itself."
            ),
        }

    def _step(
        self,
        brief: CreativeBrief,
        state: _State,
        *,
        action: str,
        args: dict[str, Any],
        status: str,
        result: dict[str, Any],
        intent: str = "",
        expected: str = "",
        idem: str = "",
    ) -> int:
        state.counters["steps"] += 1
        return self.store.append_step(
            brief.run_key,
            action=action,
            args=args,
            intent_summary=intent,
            expected_result=expected,
            result=result,
            status=status,
            idempotency_key=idem,
        )

    def _save_checkpoint(self, brief: CreativeBrief, state: _State) -> None:
        elapsed = time.monotonic() - state.started_at
        self.store.update_run(
            brief.run_key,
            counters=state.counters,
            checkpoint={
                "last_signature": state.last_signature,
                "no_progress": state.no_progress,
                "elapsed_s": elapsed,
                "updated_at": to_iso(self.clock.now()),
            },
        )

    def _finish(
        self,
        brief: CreativeBrief,
        status: str,
        state: _State,
        *,
        artifact: ArtifactVersion | None = None,
        error: str = "",
    ) -> AgentResult:
        deferred_reason = ""
        if status == "deferred":
            if "model call" in error.lower():
                deferred_reason = "model_calls"
            elif "time" in error.lower():
                deferred_reason = "time"
            elif "budget" in error.lower():
                deferred_reason = "budget"
        self.store.update_run(
            brief.run_key,
            status=status,
            counters=state.counters,
            checkpoint={
                "last_signature": state.last_signature,
                "no_progress": state.no_progress,
                "elapsed_s": time.monotonic() - state.started_at,
                "error": error,
                "deferred_reason": deferred_reason,
                "model_limit_at_defer": int(self.config.get("creator_agent.max_model_calls", 14)),
                "updated_at": to_iso(self.clock.now()),
            },
        )
        if error:
            self.store.append_step(
                brief.run_key,
                action="run_end",
                args={},
                result={"error": error},
                status=status,
            )
        return AgentResult(
            run_key=brief.run_key,
            status=status,
            artifact=artifact,
            error=error,
            steps=state.counters["steps"],
        )


def _agent_system() -> str:
    return (
        "You are the decision layer of a bounded content-operations agent. "
        "Choose exactly one tool from the schemas. You may search, read, assess claims, "
        "draft, revise, render, inspect, review, ask the owner, or submit. "
        "Never invent evidence IDs or facts. Do not submit until the artifact is complete. "
        "Return JSON with action, args, intent_summary, expected_result, base_version. "
        "The program enforces all budgets and permissions."
    )

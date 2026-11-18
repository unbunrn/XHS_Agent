"""Controlled tools exposed to the publication decision model."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from ...timeutil import to_iso
from .diagnostics import redact_text, restore_from_manifest, verify_assets
from .policy import classify_error, plan_for_error
from .store import PublicationStore


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[["PublicationContext", dict[str, Any]], dict[str, Any]]


@dataclass
class PublicationContext:
    store: PublicationStore
    task: Any
    package: dict[str, Any]
    runtime: Any
    config: Any
    clock: Any
    allowed_actions: list[str] = field(default_factory=list)
    diagnose_only: bool = False


DIAGNOSIS_ACTIONS = {
    "inspect_task",
    "check_bridge_health",
    "check_login",
    "verify_assets",
    "inspect_failure",
    "check_publish_result",
    "notify_owner",
    "schedule_resume",
}


class PublicationTools:
    def __init__(self, runtime: Any):
        self.runtime = runtime
        self.specs: dict[str, ToolSpec] = {}
        for spec in self._build():
            self.specs[spec.name] = spec

    def names(self) -> list[str]:
        return list(self.specs)

    def schemas(self) -> list[dict[str, Any]]:
        return [{"name": s.name, "description": s.description, "schema": s.schema} for s in self.specs.values()]

    def execute(self, name: str, ctx: PublicationContext, args: dict[str, Any]) -> dict[str, Any]:
        if ctx.diagnose_only and name not in DIAGNOSIS_ACTIONS:
            return {
                "ok": False,
                "error": "diagnosis_action_not_allowed",
                "observation": {"action": name, "mode": "diagnosis"},
            }
        spec = self.specs.get(name)
        if spec is None:
            return {"ok": False, "error": "unknown_tool", "observation": {"error": "unknown tool"}}
        allowed = spec.schema.get("required", [])
        for key in allowed:
            if key not in args:
                return {"ok": False, "error": "missing_arg", "observation": {"missing": key}}
        unknown = set(args) - set(spec.schema.get("properties", {}))
        if unknown:
            return {"ok": False, "error": "unknown_arg", "observation": {"unknown": sorted(unknown)}}
        try:
            return spec.handler(ctx, args)
        except Exception as exc:  # tool failure is an observation, not a model crash
            return {"ok": False, "error": type(exc).__name__, "observation": {"error": str(exc)}}

    def _build(self) -> list[ToolSpec]:
        no_args = {"type": "object", "properties": {}, "required": []}
        return [
            ToolSpec("inspect_task", "Read task/package/history/attempts without sending", no_args, self._inspect_task),
            ToolSpec("check_bridge_health", "Read-only bridge health/version", no_args, self._bridge_health),
            ToolSpec("check_login", "Read-only login/account identity check", no_args, self._login),
            ToolSpec("verify_assets", "Verify approved image files and hashes", no_args, self._verify_assets),
            ToolSpec(
                "restore_approved_assets",
                "Restore program-owned exact-hash backups by approved asset id",
                {"type": "object", "properties": {"asset_ids": {"type": "array", "items": {"type": "string"}}}, "required": ["asset_ids"]},
                self._restore_assets,
            ),
            ToolSpec("inspect_failure", "Read structured last error/diagnostic", no_args, self._inspect_failure),
            ToolSpec(
                "publish_approved_artifact",
                "Submit the frozen approved package through the publish gate",
                {"type": "object", "properties": {"retry_basis": {"type": "string"}}, "required": []},
                self._publish,
            ),
            ToolSpec("check_publish_result", "Read-only reconciliation of the latest attempt", no_args, self._check_result),
            ToolSpec(
                "request_owner_action",
                "Ask the owner for login/confirmation/reapproval and wait",
                {
                    "type": "object",
                    "properties": {"question": {"type": "string"}, "kind": {"type": "string"}},
                    "required": ["question", "kind"],
                },
                self._request_owner,
            ),
            ToolSpec(
                "schedule_resume",
                "Persist a bounded next wake time and release the worker",
                {"type": "object", "properties": {"delay_seconds": {"type": "integer"}}, "required": ["delay_seconds"]},
                self._schedule_resume,
            ),
            ToolSpec(
                "notify_owner",
                "Queue a de-duplicated task status notification",
                {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                self._notify,
            ),
        ]

    def _inspect_task(self, ctx: PublicationContext, args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "observation": {"task": ctx.task.as_dict(), "package": {k: ctx.package.get(k) for k in ("approval_code", "draft_id", "draft_version", "title", "body_sha256", "images_sha256", "images", "scheduled_at", "expires_at")}}}

    def _bridge_health(self, ctx: PublicationContext, args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "observation": self.runtime.bridge_health()}

    def _login(self, ctx: PublicationContext, args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "observation": self.runtime.login_check()}

    def _verify_assets(self, ctx: PublicationContext, args: dict[str, Any]) -> dict[str, Any]:
        report = verify_assets(ctx.package)
        return {"ok": report["ok"], "observation": report}

    def _restore_assets(self, ctx: PublicationContext, args: dict[str, Any]) -> dict[str, Any]:
        report = restore_from_manifest(
            ctx.package,
            list(args.get("asset_ids") or []),
            target_roots=[str(ctx.config.resolve_path(str(ctx.config.get("render.output_dir", "var/drafts"))))],
            backup_root=str(ctx.config.resolve_path("var/backups/publication_assets")),
        )
        return {"ok": bool(report.get("ok")), "observation": report}

    def _inspect_failure(self, ctx: PublicationContext, args: dict[str, Any]) -> dict[str, Any]:
        report = self.runtime.inspect_failure(ctx.task.id)
        if report.get("error"):
            report["error"] = redact_text(str(report["error"]))
        return {"ok": True, "observation": report}

    def _publish(self, ctx: PublicationContext, args: dict[str, Any]) -> dict[str, Any]:
        if ctx.task.cancel_requested:
            return {"ok": False, "error": "cancelled", "observation": {"state": "cancelled"}}
        result = self.runtime.publish(ctx.task, ctx.package, retry_basis=str(args.get("retry_basis") or ""))
        return {"ok": bool(result.get("ok")), "observation": result, "terminal": result.get("terminal", False)}

    def _check_result(self, ctx: PublicationContext, args: dict[str, Any]) -> dict[str, Any]:
        if ctx.diagnose_only:
            # Diagnosis may preview platform evidence, but applying it would
            # change the approval/task lifecycle behind the owner's back.
            return {"ok": True, "observation": self.runtime.preview_result(ctx.task)}
        return {"ok": True, "observation": self.runtime.check_result(ctx.task)}

    def _request_owner(self, ctx: PublicationContext, args: dict[str, Any]) -> dict[str, Any]:
        question = redact_text(str(args["question"]), limit=1000)
        kind = str(args["kind"])[:80]
        req = ctx.store.add_owner_request(ctx.task.id, question=question, kind=kind)
        waiting_state = "waiting_login" if kind.lower() in {"login", "reauth", "qr"} else "waiting_owner"
        ctx.store.update(ctx.task.id, state=waiting_state, wait_reason=question)
        self.runtime.notify(ctx.task, question)
        return {"ok": True, "observation": {"owner_request_id": req, "state": waiting_state}, "terminal": True}

    def _schedule_resume(self, ctx: PublicationContext, args: dict[str, Any]) -> dict[str, Any]:
        delay = max(10, min(3600, int(args["delay_seconds"])))
        wake = ctx.clock.now() + timedelta(seconds=delay)
        if ctx.diagnose_only:
            # A diagnostic wake must never turn into a normal publish-capable
            # job.  Keep the durable task lifecycle untouched.
            ctx.runtime.schedule(ctx.task.id, wake, diagnose_only=True)
            return {
                "ok": True,
                "observation": {
                    "state": ctx.task.state,
                    "next_wake_at": to_iso(wake),
                    "mode": "diagnosis",
                },
                "terminal": True,
            }
        ctx.store.update(ctx.task.id, state="retry_wait", next_wake_at=to_iso(wake), wait_reason="retry scheduled")
        self.runtime.schedule(ctx.task.id, wake)
        return {"ok": True, "observation": {"state": "retry_wait", "next_wake_at": to_iso(wake)}, "terminal": True}

    def _notify(self, ctx: PublicationContext, args: dict[str, Any]) -> dict[str, Any]:
        # Never forward arbitrary model prose: a notification must reflect the
        # durable task state, not a model's claim that publishing succeeded.
        label = {
            "published": "已发布",
            "simulated": "模拟发布（未发布到平台）",
            "waiting_owner": "等待负责人处理",
            "waiting_login": "等待登录",
            "waiting_approval": "等待审批",
            "retry_wait": "等待重试",
            "cancelled": "已取消",
            "paused": "已暂停",
        }.get(ctx.task.state, ctx.task.state)
        reason = redact_text(ctx.task.wait_reason or "", limit=300)
        text = f"发布任务 {ctx.task.code} 当前状态：{label}。"
        if reason:
            text += f" {reason}"
        self.runtime.notify(ctx.task, text)
        return {"ok": True, "observation": {"notified": True}}

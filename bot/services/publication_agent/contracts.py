"""Stable contracts for the publication recovery phase."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ...adapters.types import BudgetExceeded


TASK_STATES = {
    "ready",
    "diagnosing",
    "submitting",
    "verifying",
    "retry_wait",
    "waiting_login",
    "waiting_owner",
    "waiting_approval",
    "paused",
    "published",
    "simulated",
    "cancelled",
}
TERMINAL_STATES = {"published", "simulated", "cancelled"}
DISPATCH_STATES = {"not_dispatched", "accepted_unconfirmed", "unknown", "published", "rejected"}


class PublicationBudgetExceeded(BudgetExceeded):
    """Raised before a publication model transport attempt is reserved."""


@dataclass(frozen=True)
class PublicationTask:
    id: int
    code: str
    account_id: str
    approval_id: int
    draft_id: int
    draft_version: int
    package_hash: str
    work_key: str | None
    generation: int
    state: str
    cancel_requested: bool = False
    creator_run_key: str | None = None
    next_wake_at: str | None = None
    wait_reason: str | None = None
    active_attempt_id: int | None = None
    last_error_class: str | None = None
    retry_count: int = 0
    lease_owner: str | None = None
    lease_until: str | None = None
    terminal_evidence_ref: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AgentDecision:
    action: str
    args: dict[str, Any] = field(default_factory=dict)
    intent_summary: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    expected_result: str = ""

    @classmethod
    def from_dict(cls, raw: Any, *, actions: set[str]) -> "AgentDecision":
        if not isinstance(raw, dict):
            raise ValueError("decision must be an object")
        action = str(raw.get("action") or "")
        if action not in actions:
            raise ValueError(f"unknown action: {action!r}")
        args = raw.get("args") or {}
        if not isinstance(args, dict):
            raise ValueError("args must be an object")
        refs = raw.get("evidence_refs") or []
        if not isinstance(refs, list) or not all(isinstance(x, str) for x in refs):
            raise ValueError("evidence_refs must be a list of strings")
        return cls(
            action=action,
            args=args,
            intent_summary=str(raw.get("intent_summary") or "")[:300],
            evidence_refs=refs[:20],
            expected_result=str(raw.get("expected_result") or "")[:300],
        )


@dataclass(frozen=True)
class TaskError:
    error_class: str
    stage: str
    detail: str
    retryable: bool = False
    evidence_refs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PublicationObservation:
    task: PublicationTask
    package: dict[str, Any]
    history: list[dict[str, Any]] = field(default_factory=list)
    failed_attempts: list[dict[str, Any]] = field(default_factory=list)
    active_attempt: dict[str, Any] | None = None
    owner_request: dict[str, Any] | None = None
    allowed_actions: list[str] = field(default_factory=list)
    budget: dict[str, Any] = field(default_factory=dict)
    last_error: TaskError | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "task": self.task.as_dict(),
            "package": self.package,
            "history": self.history,
            "failed_attempts": self.failed_attempts,
            "active_attempt": self.active_attempt,
            "owner_request": self.owner_request,
            "allowed_actions": self.allowed_actions,
            "budget": self.budget,
            "last_error": self.last_error.as_dict() if self.last_error else None,
        }


@dataclass(frozen=True)
class RecoveryPlan:
    retryable: bool
    reason: str
    next_wake_at: str | None = None
    requires_owner: bool = False
    requires_login: bool = False
    requires_reapproval: bool = False

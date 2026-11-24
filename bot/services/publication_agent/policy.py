"""Publication retry/cancel policy.  Pure functions, no model calls."""

from __future__ import annotations

from datetime import datetime, timedelta

from ...timeutil import parse_iso
from .contracts import RecoveryPlan, TaskError


RETRYABLE = {"not_dispatched", "login_expired", "asset_missing", "bridge_unavailable", "connection_before_send"}
NON_RETRYABLE = {"rejected", "approval_invalid", "cancelled", "owner_cancelled", "platform_rejected"}


def classify_error(raw: str, *, stage: str = "unknown", dispatch_state: str | None = None) -> TaskError:
    text = (raw or "").lower()
    if dispatch_state in {"unknown", "accepted_unconfirmed"} or any(x in text for x in ("timeout", "unknown", "in flight", "5xx", "500")):
        return TaskError("unknown_result", stage, raw, retryable=False)
    if "login" in text or "not logged" in text or "扫码" in text:
        return TaskError("login_expired", stage, raw, retryable=True)
    if "image" in text or "asset" in text or "hash" in text:
        return TaskError("asset_missing", stage, raw, retryable=True)
    if "connect" in text or "bridge" in text or "unavailable" in text:
        return TaskError("bridge_unavailable", stage, raw, retryable=True)
    if "reject" in text or "refus" in text or "拦截" in text:
        return TaskError("rejected", stage, raw, retryable=False)
    return TaskError("unknown_error", stage, raw, retryable=False)


def plan_for_error(error: TaskError, *, now: datetime, retry_count: int, max_retries: int = 3) -> RecoveryPlan:
    if error.error_class == "login_expired":
        return RecoveryPlan(True, "login required", requires_owner=True, requires_login=True)
    if error.error_class == "asset_missing":
        return RecoveryPlan(True, "assets need verification", requires_owner=True)
    if error.error_class == "unknown_result":
        # Never auto-resend an unknown POST.  The owner may authorize a
        # read-only resolution, but this plan alone never sends.
        return RecoveryPlan(False, "result unknown; reconcile before any retry", requires_owner=True)
    if error.error_class in NON_RETRYABLE:
        return RecoveryPlan(False, error.detail, requires_owner=True)
    if retry_count >= max_retries:
        return RecoveryPlan(False, "retry limit reached", requires_owner=True)
    delay = (30, 120, 600)[min(retry_count, 2)]
    return RecoveryPlan(True, f"retry after {delay}s", next_wake_at=(now + timedelta(seconds=delay)).isoformat())

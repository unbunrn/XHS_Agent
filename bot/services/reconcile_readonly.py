"""Conservative, read-only publication reconciliation.

This module never calls ``Publisher.publish`` and never retries a submission.
It asks an :class:`EvidenceProvider` (a read-only view of the owner's notes) for
candidate posts, then requires a strong, unique match before changing an
``unknown`` attempt into ``published``.

A missing result, a same-title post, an empty list, or a provider error is *not*
evidence of failure.  Those cases stay ``unknown`` and are surfaced to the
owner.  Only the manual ``reconcile`` command may declare "not published", and
only after a human checked the platform.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Protocol

from ..adapters.types import NotConfigured
from ..config import Config
from ..db import Database, json_dumps
from ..security import normalize_title_key, normalize_ws
from ..timeutil import Clock, parse_iso, to_iso
from . import approvals, publish
from .publication_results import record_result


class ReconcileError(RuntimeError):
    pass


_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9._-]{8,}\b"),
    re.compile(
        r"(?i)\b(token|secret|password|passwd|api[_-]?key|cookie|session)\b"
        r"\s*[:=]\s*['\"]?([^\s,;'\"]{3,})"
    ),
)


def _sanitize_text(text: Any, limit: int = 240) -> str:
    """Redact credential-like fragments before they reach notices or audit."""
    cleaned = str(text or "")
    for pattern in _SENSITIVE_PATTERNS:
        if pattern.groups >= 2:
            cleaned = pattern.sub(lambda m: f"{m.group(1)}=[REDACTED]", cleaned)
        elif pattern.groups == 1:
            cleaned = pattern.sub(lambda m: f"{m.group(1)} [REDACTED]", cleaned)
        else:
            cleaned = pattern.sub("[REDACTED]", cleaned)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > limit:
        cleaned = cleaned[: max(0, limit - 1)] + "…"
    return cleaned


def _sanitize_exception(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {_sanitize_text(exc)}"


@dataclass
class NoteEvidence:
    """A sanitized read-only candidate from the owner's own notes."""

    external_post_id: str
    external_url: str = ""
    title: str = ""
    body: str = ""
    published_at: str | None = None
    account_id: str | None = None
    raw: dict[str, Any] | None = None


class EvidenceProvider(Protocol):
    name: str

    def lookup(
        self,
        *,
        attempt: sqlite3.Row,
        approval: sqlite3.Row,
        draft: sqlite3.Row,
    ) -> list[NoteEvidence]: ...


class UnavailableEvidenceProvider:
    name = "unavailable"

    def lookup(self, *, attempt, approval, draft) -> list[NoteEvidence]:  # noqa: ARG002
        raise NotConfigured(
            "no documented read-only note-lookup API is configured; "
            "automatic reconciliation stays unknown and manual reconciliation is required"
        )


def _reserve_check(
    db: Database,
    clock: Clock,
    *,
    attempt_id: int,
    max_checks: int,
    min_interval_minutes: int,
) -> tuple[int | None, str | None, str | None]:
    """Reserve one check transactionally, enforcing count and interval limits.

    Reserving before the provider call prevents two concurrent workers from
    both passing the rate check and then racing to write contradictory results.
    The row starts as ``in_progress`` and is finalized after the read-only I/O.
    """
    now = to_iso(clock.now())
    with db.tx() as conn:
        attempt = conn.execute(
            "SELECT state FROM publish_attempts WHERE id=?", (int(attempt_id),)
        ).fetchone()
        if attempt is None:
            return None, "attempt_missing", None
        current_state = str(attempt["state"])
        if current_state != "unknown":
            return None, "attempt_changed", current_state
        count = int(
            conn.execute(
                "SELECT COUNT(*) FROM publish_reconcile_checks WHERE attempt_id=?",
                (int(attempt_id),),
            ).fetchone()[0]
            or 0
        )
        if count >= max_checks:
            return None, "reconcile_attempt_limit", current_state
        last = conn.execute(
            "SELECT checked_at FROM publish_reconcile_checks WHERE attempt_id=? "
            "ORDER BY id DESC LIMIT 1",
            (int(attempt_id),),
        ).fetchone()
        if last is not None and min_interval_minutes > 0:
            last_at = parse_iso(last["checked_at"])
            if last_at is not None and (clock.now() - last_at).total_seconds() < min_interval_minutes * 60:
                return None, "reconcile_interval_wait", current_state
        cur = conn.execute(
            "INSERT INTO publish_reconcile_checks(attempt_id, checked_at, outcome, reason_code, detail, evidence_json) "
            "VALUES (?, ?, 'in_progress', 'reserved', '', '{}')",
            (int(attempt_id), now),
        )
        return int(cur.lastrowid), None, current_state


def _finish_check(
    db: Database,
    *,
    check_id: int | None,
    outcome: str,
    reason_code: str,
    detail: str,
    evidence: dict[str, Any] | None = None,
) -> None:
    if check_id is None:
        return
    with db.tx() as conn:
        conn.execute(
            "UPDATE publish_reconcile_checks SET outcome=?, reason_code=?, detail=?, evidence_json=? "
            "WHERE id=? AND outcome='in_progress'",
            (outcome, reason_code, detail[:1000], json_dumps(evidence or {}), int(check_id)),
        )


def _changed_response(
    db: Database,
    approval: sqlite3.Row,
    *,
    attempt_id: int,
    checked: bool,
) -> dict[str, Any]:
    attempt = db.fetchone("SELECT * FROM publish_attempts WHERE id=?", (int(attempt_id),))
    attempt_state = str(attempt["state"]) if attempt is not None else "unknown"
    result = db.fetchone(
        "SELECT * FROM publish_results WHERE approval_id=? ORDER BY id DESC LIMIT 1",
        (int(approval["id"]),),
    )
    state = str(result["state"]) if result is not None else attempt_state
    return {
        "ok": state in {"published", "simulated"},
        "state": state,
        "reason_code": "attempt_state_changed",
        "detail": "对账期间发布尝试已被其他写入方确认；保留当前终态，未用旧结果覆盖。",
        "checked": checked,
        "result_id": int(result["id"]) if result is not None else None,
        "notification_id": (
            int(result["notification_id"])
            if result is not None and result["notification_id"] is not None
            else None
        ),
        "attempt_state": attempt_state,
        "transition_blocked": True,
    }


def _match_note(
    note: NoteEvidence,
    *,
    approval: sqlite3.Row,
    draft: sqlite3.Row,
    attempt: sqlite3.Row,
    window_minutes: int,
    min_body_similarity: float,
) -> tuple[bool, str, float]:
    """Return (is_match, reason, body_similarity).

    A title is never enough, and a nearby timestamp is not a substitute for
    content evidence.  To change an unknown attempt into "published" the
    provider must prove (1) the note belongs to the owner account and (2) the
    body is a strong match for the approved final body.  Time can narrow the
    window but cannot establish identity by itself.
    """
    if not note.account_id:
        return False, "candidate is missing owner account proof", 0.0
    if str(note.account_id) != str(approval["account_id"]):
        return False, "account mismatch", 0.0
    candidate_title = normalize_title_key(note.title or "")
    approved_title = normalize_title_key(str(draft["title"] or ""))
    if not candidate_title or candidate_title != approved_title:
        return False, "title mismatch", 0.0

    approved_body = normalize_ws(str(draft["body"] or ""))
    candidate_body = normalize_ws(note.body or "")
    if not candidate_body:
        return False, "candidate is missing body evidence", 0.0
    body_similarity = SequenceMatcher(None, approved_body, candidate_body).ratio()
    if body_similarity < float(min_body_similarity):
        return False, "body differs from the approved draft", body_similarity

    candidate_time = parse_iso(note.published_at)
    attempt_time = parse_iso(attempt["started_at"] or attempt["created_at"])
    if candidate_time is not None and attempt_time is not None:
        delta_seconds = abs((candidate_time - attempt_time).total_seconds())
        if delta_seconds > int(window_minutes) * 60:
            return False, "published time is outside the match window", body_similarity

    if not (note.external_post_id or note.external_url):
        return False, "matched note has no post id or URL receipt", body_similarity
    return True, "unique title/body/account match", body_similarity


def read_only_reconcile(
    db: Database,
    config: Config,
    clock: Clock,
    *,
    approval_code: str | None = None,
    attempt_id: int | None = None,
    provider: EvidenceProvider | None = None,
) -> dict[str, Any]:
    """Try to resolve one unknown attempt without submitting anything."""
    if approval_code is not None:
        approval = approvals.get_approval(db, code=approval_code)
        if approval is None:
            raise ReconcileError("approval not found")
        attempt = db.fetchone(
            "SELECT * FROM publish_attempts WHERE approval_id=? ORDER BY id DESC LIMIT 1",
            (int(approval["id"]),),
        )
    elif attempt_id is not None:
        attempt = db.fetchone("SELECT * FROM publish_attempts WHERE id=?", (int(attempt_id),))
        if attempt is None:
            raise ReconcileError("publish attempt not found")
        approval = approvals.get_approval(db, approval_id=int(attempt["approval_id"]))
    else:
        raise ReconcileError("approval_code or attempt_id is required")
    if attempt is None or approval is None:
        raise ReconcileError("no publish attempt to reconcile")
    if attempt["state"] == "publishing":
        # A publisher may still be finishing this exact attempt.  Read-only
        # reconciliation must not claim or resolve it while the request is in
        # flight; the normal publish completion or restart recovery owns it.
        raise ReconcileError(
            "attempt state is publishing; the active publish request must finish before read-only reconciliation"
        )
    if attempt["state"] != "unknown":
        raise ReconcileError(f"attempt state is {attempt['state']}, not unknown")

    max_checks = max(1, int(config.get("reconcile.max_checks_per_attempt", 5)))
    max_age_hours = max(1, int(config.get("reconcile.max_age_hours", 72)))
    min_interval_minutes = max(0, int(config.get("reconcile.min_interval_minutes", 10)))
    window_minutes = max(1, int(config.get("reconcile.match_window_minutes", 120)))
    min_body_similarity = float(config.get("reconcile.min_body_similarity", 0.65))

    created = parse_iso(attempt["created_at"])
    if created is not None and (clock.now() - created).total_seconds() > max_age_hours * 3600:
        return {
            "ok": False,
            "state": "unknown",
            "reason_code": "reconcile_window_expired",
            "detail": "对账时限已过；保持 unknown，需人工核对，绝不自动重发。",
            "checked": False,
        }

    draft = db.fetchone("SELECT * FROM drafts WHERE id=?", (approval["draft_id"],))
    if draft is None:
        raise ReconcileError("draft not found")

    check_id, reserve_reason, reserve_state = _reserve_check(
        db,
        clock,
        attempt_id=int(attempt["id"]),
        max_checks=max_checks,
        min_interval_minutes=min_interval_minutes,
    )
    if check_id is None:
        if reserve_reason == "attempt_changed":
            return _changed_response(db, approval, attempt_id=int(attempt["id"]), checked=False)
        if reserve_reason == "attempt_missing":
            raise ReconcileError("publish attempt disappeared during reconciliation")
        if reserve_reason == "reconcile_attempt_limit":
            detail = f"已达到最多 {max_checks} 次只读对账；保持 unknown，需人工核对。"
        elif reserve_reason == "reconcile_interval_wait":
            detail = f"距上次对账不足 {min_interval_minutes} 分钟；保持 unknown，等待后重试。"
        else:
            detail = "无法开始只读对账；保持 unknown。"
        return {
            "ok": False,
            "state": "unknown",
            "reason_code": reserve_reason or "reconcile_unavailable",
            "detail": detail,
            "checked": False,
        }

    def changed() -> dict[str, Any] | None:
        current = db.fetchone("SELECT state FROM publish_attempts WHERE id=?", (int(attempt["id"]),))
        if current is None or str(current["state"]) == "unknown":
            return None
        _finish_check(
            db,
            check_id=check_id,
            outcome=str(current["state"]),
            reason_code="attempt_state_changed",
            detail="对账期间发布尝试已被其他写入方确认；保留当前终态。",
        )
        return _changed_response(db, approval, attempt_id=int(attempt["id"]), checked=True)

    provider = provider or UnavailableEvidenceProvider()
    try:
        notes = provider.lookup(attempt=attempt, approval=approval, draft=draft)
    except NotConfigured as exc:
        raced = changed()
        if raced is not None:
            return raced
        detail = f"没有可用的只读笔记查询接口：{_sanitize_exception(exc)}；保持 unknown，需人工核对。"
        _finish_check(
            db, check_id=check_id, outcome="unknown", reason_code="lookup_unavailable", detail=detail
        )
        result = record_result(
            db,
            config,
            clock,
            approval=approval,
            state="unknown",
            stage="reconcile",
            attempt=attempt,
            reason_code="lookup_unavailable",
            detail=detail,
        )
        return {
            "ok": False,
            "state": result.get("state", "unknown"),
            "reason_code": "lookup_unavailable",
            "detail": detail,
            "checked": True,
            "result_id": result.get("result_id"),
            "notification_id": result.get("notification_id"),
        }
    except Exception as exc:  # noqa: BLE001 - provider outage is not proof of failure
        raced = changed()
        if raced is not None:
            return raced
        detail = f"只读对账查询失败：{_sanitize_exception(exc)}；保持 unknown。"
        _finish_check(
            db, check_id=check_id, outcome="unknown", reason_code="lookup_failed", detail=detail
        )
        result = record_result(
            db,
            config,
            clock,
            approval=approval,
            state="unknown",
            stage="reconcile",
            attempt=attempt,
            reason_code="lookup_failed",
            detail=detail,
        )
        return {
            "ok": False,
            "state": result.get("state", "unknown"),
            "reason_code": "lookup_failed",
            "detail": detail,
            "checked": True,
            "result_id": result.get("result_id"),
            "notification_id": result.get("notification_id"),
        }

    raced = changed()
    if raced is not None:
        return raced

    matches: list[tuple[NoteEvidence, float]] = []
    nearest_reason = "no candidate note matched title/body/account"
    for note in notes:
        matched, reason, similarity = _match_note(
            note,
            approval=approval,
            draft=draft,
            attempt=attempt,
            window_minutes=window_minutes,
            min_body_similarity=min_body_similarity,
        )
        if matched:
            matches.append((note, similarity))
        else:
            nearest_reason = reason

    if len(matches) == 1:
        note, similarity = matches[0]
        try:
            resolved = publish.reconcile_unknown(
                db,
                config,
                clock,
                approval_code=str(approval["code"]),
                resolution="confirmed_published",
                note=(
                    "只读对账唯一匹配：账号一致、标题一致且正文达到阈值"
                    f"（body_similarity={similarity:.3f}）。"
                ),
                external_post_id=note.external_post_id or None,
                external_url=note.external_url or None,
                actor="reconcile_readonly",
            )
        except publish.PublishGateError as exc:
            raced = changed()
            if raced is not None:
                return raced
            detail = f"匹配到笔记但无法落账：{exc}；保持 unknown。"
            _finish_check(
                db,
                check_id=check_id,
                outcome="unknown",
                reason_code="receipt_persist_failed",
                detail=detail,
                evidence={"post_id": note.external_post_id, "url": note.external_url},
            )
            result = record_result(
                db,
                config,
                clock,
                approval=approval,
                state="unknown",
                stage="reconcile",
                attempt=attempt,
                reason_code="receipt_persist_failed",
                detail=detail,
            )
            return {
                "ok": False,
                "state": result.get("state", "unknown"),
                "reason_code": "receipt_persist_failed",
                "detail": detail,
                "checked": True,
                "result_id": result.get("result_id"),
            }
        _finish_check(
            db,
            check_id=check_id,
            outcome="published",
            reason_code="unique_readonly_match",
            detail="唯一匹配的本人笔记，账号/正文达到阈值。",
            evidence={
                "post_id": note.external_post_id,
                "url": note.external_url,
                "title_key": normalize_title_key(note.title or ""),
                "body_similarity": round(similarity, 4),
                "published_at": note.published_at,
            },
        )
        return {
            "ok": True,
            "state": "published",
            "reason_code": "unique_readonly_match",
            "detail": resolved.get("detail", "reconciled"),
            "checked": True,
            "result_id": resolved.get("result_id"),
            "post_id": note.external_post_id,
            "url": note.external_url,
        }

    raced = changed()
    if raced is not None:
        return raced

    if len(matches) > 1:
        reason_code = "ambiguous_multiple_matches"
        detail = f"找到 {len(matches)} 篇相似笔记，无法唯一确认；保持 unknown，需人工核对。"
    else:
        reason_code = "no_unique_match"
        detail = f"{nearest_reason}；这不能证明未发布，保持 unknown，绝不自动重发。"
    _finish_check(
        db,
        check_id=check_id,
        outcome="unknown",
        reason_code=reason_code,
        detail=detail,
        evidence={"candidates": len(notes), "matches": len(matches), "window_minutes": window_minutes},
    )
    result = record_result(
        db,
        config,
        clock,
        approval=approval,
        state="unknown",
        stage="reconcile",
        attempt=attempt,
        reason_code=reason_code,
        detail=detail,
    )
    return {
        "ok": result.get("state") in {"published", "simulated"},
        "state": result.get("state", "unknown"),
        "reason_code": reason_code,
        "detail": detail,
        "checked": True,
        "result_id": result.get("result_id"),
        "transition_blocked": bool(result.get("transition_blocked")),
    }

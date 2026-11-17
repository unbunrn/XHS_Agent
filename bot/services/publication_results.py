"""Durable business results for publish decisions.

A job being consumed is not the same thing as a post being published.  This
module keeps one queryable result row per approval (or per publish attempt) and
queues an idempotent owner notification for every *state change*.  It is the
single place where success, definite failure, pre-submit blocking and unknown
outcomes are translated into owner-facing text and stored evidence.

No publisher is called from here.  In particular, a blocked gate result has no
publish attempt and must never be counted as a submission.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..audit import audit
from ..config import Config
from ..db import Database, json_dumps, json_loads
from ..timeutil import Clock, local_iso, to_iso
from . import notifications

RESULT_STATES = {"published", "simulated", "failed", "unknown", "blocked", "expired"}
# These states describe a final business fact.  A late, stale writer must never
# turn a confirmed result back into unknown/blocked.  ``unknown`` and the
# pre-submit states remain non-terminal because later evidence may resolve them.
TERMINAL_RESULT_STATES = {"published", "simulated", "failed"}

_STATE_LABEL = {
    "published": "已确认发布",
    "simulated": "仅本地模拟/导出（未发布）",
    "failed": "确定未发布",
    "unknown": "结果未知",
    "blocked": "发布前被拦截（未提交）",
    "expired": "审批已过期（未提交）",
}


def _next_step(state: str) -> str:
    if state == "published":
        return "无需操作；请以平台回执为准。"
    if state == "simulated":
        return "这是演示结果，不代表真实发布；真实发布需连接到已登录的发布桥并重新审批。"
    if state == "failed":
        return "不会自动重试；若要再发，必须重新生成草稿并重新审批。"
    if state == "unknown":
        return "不会自动重试；请只读核对平台，确认未发后再重新生成并审批，切勿直接重发。"
    if state == "expired":
        return "审批窗口已过；请重新生成草稿并取得新的审批。"
    return "请按说明修正后，重新生成草稿并取得新的审批。"


def _label(state: str) -> str:
    return _STATE_LABEL.get(state, state)


def result_label(state: str) -> str:
    """Public, read-only label for CLI/console display."""
    return _label(state)


def _dedupe_key(approval_id: int, attempt_id: int | None, stage: str, reason_code: str | None, state: str) -> str:
    if attempt_id is not None:
        return f"attempt:{int(attempt_id)}"
    # A gate block has no attempt.  Keying by reason avoids a retry loop that
    # spams the owner, while still allowing a *different* blocking reason to be
    # recorded separately.
    return f"gate:{int(approval_id)}:{stage}:{(reason_code or state)[:80]}"


def _message(
    target: sqlite3.Connection,
    *,
    approval: sqlite3.Row,
    attempt_id: int | None,
    state: str,
    detail: str,
    external_post_id: str | None,
    external_url: str | None,
    now: Any,
    timezone_name: str,
) -> str:
    draft = target.execute("SELECT title FROM drafts WHERE id=?", (approval["draft_id"],)).fetchone()
    title = str(draft["title"] if draft is not None else "")
    lines = [
        "【发布结果】",
        f"审批码：{approval['code']}",
        f"稿件：#{int(approval['draft_id'])} {title}",
        f"状态：{_label(state)}",
    ]
    if attempt_id is not None:
        lines.append(f"发布尝试：#{int(attempt_id)}")
    if detail:
        lines.append(f"说明：{detail}")
    if external_post_id or external_url:
        lines.append(f"平台回执：{external_post_id or '（无 post id）'}")
        lines.append(f"链接：{external_url or '（无）'}")
    lines.append(f"下一步：{_next_step(state)}")
    lines.append(f"记录时间：{local_iso(now, timezone_name)}")
    return "\n".join(lines)


def record_result(
    db: Database,
    config: Config,
    clock: Clock,
    *,
    approval: sqlite3.Row,
    state: str,
    detail: str = "",
    stage: str = "publisher",
    attempt: sqlite3.Row | None = None,
    reason_code: str | None = None,
    external_post_id: str | None = None,
    external_url: str | None = None,
    receipt: dict[str, Any] | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Insert/update the business result and queue one notice per state change.

    ``conn`` is supplied when the caller already owns the transaction (publish
    finish, restart recovery, manual reconciliation), so the result row and its
    notification commit together.  The operation is idempotent: re-recording the
    same attempt/state does not create a second notification.
    """
    if state not in RESULT_STATES:
        raise ValueError(f"unsupported publish result state: {state!r}")
    now = to_iso(clock.now())
    attempt_id = int(attempt["id"]) if attempt is not None else None
    dedupe = _dedupe_key(int(approval["id"]), attempt_id, stage, reason_code, state)
    notification_key = f"publish-result:{int(approval['id'])}:{dedupe}:{state}"

    def _previous_payload(row: sqlite3.Row) -> dict[str, Any]:
        existing_state = str(row["state"])
        return {
            "ok": existing_state in {"published", "simulated"},
            "state": existing_state,
            "stage": str(row["stage"]),
            "reason_code": row["reason_code"],
            "detail": row["detail"],
            "result_id": int(row["id"]),
            "notification_id": int(row["notification_id"]) if row["notification_id"] is not None else None,
            "notification_queued": False,
            "already_attempted": False,
            "transition_blocked": True,
        }

    def _work(target: sqlite3.Connection) -> dict[str, Any]:
        previous = target.execute("SELECT * FROM publish_results WHERE dedupe_key=?", (dedupe,)).fetchone()
        # A confirmed result is a business fact, not a last-writer-wins cache.
        # This is the final guard against a slow provider/reconcile callback
        # writing its stale "unknown" over a result that was already resolved by
        # a human or another worker.
        if previous is not None and str(previous["state"]) in TERMINAL_RESULT_STATES and str(previous["state"]) != state:
            return _previous_payload(previous)
        if attempt_id is not None:
            attempt_row = target.execute(
                "SELECT state FROM publish_attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            attempt_state = str(attempt_row["state"]) if attempt_row is not None else None
            if attempt_state in TERMINAL_RESULT_STATES and attempt_state != state:
                existing = target.execute(
                    "SELECT * FROM publish_results WHERE attempt_id=? ORDER BY id DESC LIMIT 1",
                    (attempt_id,),
                ).fetchone()
                if existing is not None:
                    return _previous_payload(existing)
                return {
                    "ok": attempt_state in {"published", "simulated"},
                    "state": attempt_state,
                    "stage": "publisher",
                    "reason_code": "attempt_state_changed",
                    "detail": "发布尝试已被其他写入方确认，拒绝用旧结果覆盖。",
                    "result_id": None,
                    "notification_id": None,
                    "notification_queued": False,
                    "already_attempted": False,
                    "transition_blocked": True,
                }
        # Preserve an existing receipt/url when a later re-record does not carry
        # one; a reconciliation note must not erase the real platform identity.
        previous_receipt = json_loads(previous["receipt_json"], {}) if previous is not None else {}
        target.execute(
            "INSERT INTO publish_results(approval_id, draft_id, attempt_id, dedupe_key, stage, state, "
            "reason_code, detail, external_post_id, external_url, receipt_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(dedupe_key) DO UPDATE SET stage=excluded.stage, state=excluded.state, "
            "reason_code=excluded.reason_code, detail=excluded.detail, "
            "external_post_id=COALESCE(excluded.external_post_id, publish_results.external_post_id), "
            "external_url=COALESCE(excluded.external_url, publish_results.external_url), "
            "receipt_json=excluded.receipt_json, updated_at=excluded.updated_at",
            (
                int(approval["id"]),
                int(approval["draft_id"]),
                attempt_id,
                dedupe,
                stage,
                state,
                reason_code,
                detail,
                external_post_id,
                external_url,
                json_dumps(receipt if receipt is not None else previous_receipt),
                now,
                now,
            ),
        )
        result = target.execute("SELECT * FROM publish_results WHERE dedupe_key=?", (dedupe,)).fetchone()
        assert result is not None
        changed = previous is None or str(previous["state"]) != state
        notification_id = previous["notification_id"] if previous is not None else None
        queued = False
        # Queue only on a real state change.  If an earlier attempt to enqueue
        # crashed before setting notification_id, fill the gap once.
        if changed or notification_id is None:
            notification_id = notifications.queue_outcome(
                db,
                config,
                clock,
                _message(
                    target,
                    approval=approval,
                    attempt_id=attempt_id,
                    state=state,
                    detail=detail,
                    external_post_id=external_post_id,
                    external_url=external_url,
                    now=clock.now(),
                    timezone_name=config.timezone_name,
                ),
                dedupe_key=notification_key,
                conn=target,
            )
            queued = True
            target.execute(
                "UPDATE publish_results SET notification_id=? WHERE id=?",
                (int(notification_id), int(result["id"])),
            )
        if changed:
            audit(
                target,
                "publish_gate",
                f"publish.result.{state}",
                entity="publish_result",
                entity_id=int(result["id"]),
                detail={
                    "approval_code": approval["code"],
                    "attempt_id": attempt_id,
                    "stage": stage,
                    "reason_code": reason_code,
                    "detail": detail,
                    "external_post_id": external_post_id,
                    "external_url": external_url,
                },
                at=now,
            )
        return {
            "ok": state in {"published", "simulated"},
            "state": state,
            "stage": stage,
            "reason_code": reason_code,
            "detail": detail,
            "result_id": int(result["id"]),
            "notification_id": int(notification_id) if notification_id is not None else None,
            "notification_queued": queued,
            "already_attempted": False,
        }

    if conn is not None:
        return _work(conn)
    with db.tx() as own:
        return _work(own)


def list_results(db: Database, *, limit: int = 20) -> list[sqlite3.Row]:
    return db.fetchall(
        "SELECT r.*, a.code AS approval_code, d.title AS draft_title "
        "FROM publish_results r JOIN approvals a ON a.id=r.approval_id "
        "JOIN drafts d ON d.id=r.draft_id ORDER BY r.id DESC LIMIT ?",
        (max(1, int(limit)),),
    )


def latest_for_approval(db: Database, approval_id: int) -> sqlite3.Row | None:
    return db.fetchone(
        "SELECT * FROM publish_results WHERE approval_id=? ORDER BY id DESC LIMIT 1",
        (int(approval_id),),
    )

"""Entry points: approval -> durable publication task -> wake."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from ...config import Config
from ...db import Database
from ...jobs import enqueue_job
from ...timeutil import Clock, parse_iso, to_iso
from .. import approvals
from .store import PublicationStore


def create_task_for_approval(
    db: Database,
    config: Config,
    clock: Clock,
    approval_id: int,
    *,
    creator_run_key: str | None = None,
) -> dict[str, Any]:
    approval = approvals.get_approval(db, approval_id=approval_id)
    if approval is None:
        raise ValueError("approval not found")
    if approval["status"] != "approved":
        raise ValueError(f"approval is {approval['status']}, not approved")
    ok, reason, images = approvals.verify_approval_artifact(db, approval)
    if not ok:
        raise ValueError(f"approval artifact invalid: {reason}")
    draft = db.fetchone("SELECT * FROM drafts WHERE id=?", (approval["draft_id"],))
    if draft is None:
        raise ValueError("draft not found")
    store = PublicationStore(db, clock)
    task = store.create_task(
        approval=approval,
        draft=draft,
        images=images or [],
        account_id=str(approval["account_id"]),
        creator_run_key=creator_run_key,
        asset_roots=[str(config.resolve_path(str(config.get("render.output_dir", "var/drafts"))))],
        asset_backup_root=str(config.resolve_path("var/backups/publication_assets")),
    )
    at = parse_iso(approval["scheduled_at"])
    job_id = enqueue_job(
        db,
        "publication_agent",
        f"publication:approval:{approval['id']}",
        payload={"task_id": task.id, "approval_id": int(approval["id"])},
        run_at=at,
        priority=7,
        clock=clock,
        max_attempts=3,
    )
    return {"task": task.as_dict(), "job_id": job_id}


def wake_task(
    db: Database, clock: Clock, task_id: int, *, reason: str,
    event_key: str | None = None, diagnose_only: bool = False,
    reconcile_only: bool = False, conn: sqlite3.Connection | None = None,
) -> int:
    # The inbound message/event id, not the unchanged task generation, is the
    # idempotency key: a replay of the same message must hit the same job while
    # a genuinely new owner message must be able to wake the task again.
    slot = str(event_key or reason)
    if reconcile_only:
        slot += ":reconcile"
    return enqueue_job(
        db,
        "publication_agent",
        f"publication:wake:{task_id}:{slot}",
        payload={
            "task_id": int(task_id),
            "reason": reason,
            "diagnose_only": bool(diagnose_only),
            "reconcile_only": bool(reconcile_only),
        },
        clock=clock,
        priority=7,
        max_attempts=3,
        conn=conn,
    )


def _unknown_reconcile_key(task_id: int, attempt_id: int) -> str:
    return f"publication:wake:{task_id}:unknown-attempt:{attempt_id}:reconcile"


def recover_unknown_reconciliations(
    db: Database, clock: Clock, *, limit: int = 50,
) -> int:
    """Recover the narrow crash window left by older/aborted processes.

    New unknown results enqueue this job in the same transaction as the task
    update.  This startup pass covers a process that died before that fix, or
    before any job existed, without inventing a second reconciliation state.
    """
    rows = db.fetchall(
        "SELECT t.id AS task_id, a.id AS attempt_id "
        "FROM publication_tasks t "
        "JOIN publish_attempts a ON a.id = ("
        "  SELECT a2.id FROM publish_attempts a2 "
        "  WHERE a2.publication_task_id=t.id ORDER BY a2.id DESC LIMIT 1"
        ") "
        "WHERE t.state='waiting_owner' AND a.state='unknown' "
        "AND (SELECT COUNT(*) FROM publication_agent_steps s "
        "     WHERE s.task_id=t.id AND s.action='reconcile_wait') < 3 "
        "ORDER BY t.id LIMIT ?",
        (int(limit),),
    )
    queued = 0
    now = to_iso(clock.now())
    for row in rows:
        task_id = int(row["task_id"])
        attempt_id = int(row["attempt_id"])
        key = _unknown_reconcile_key(task_id, attempt_id)
        # A valid query may be the immediate seed job or a delayed backoff job
        # (different dedupe key).  Either one means startup must do nothing.
        active = db.fetchone(
            "SELECT id FROM jobs WHERE kind='publication_agent' "
            "AND state IN ('pending','running') "
            "AND COALESCE(json_extract(payload_json, '$.reconcile_only'), 0)=1 "
            "AND CAST(json_extract(payload_json, '$.task_id') AS INTEGER)=? "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        if active is not None:
            continue
        job = db.fetchone("SELECT id, state FROM jobs WHERE dedupe_key=?", (key,))
        if job is not None:
            with db.tx() as conn:
                cursor = conn.execute(
                    "UPDATE jobs SET state='pending', attempts=0, next_run_at=?, "
                    "lease_owner=NULL, lease_until=NULL, last_error=NULL, updated_at=? "
                    "WHERE id=? AND state IN ('done','failed')",
                    (now, now, int(job["id"])),
                )
                if cursor.rowcount:
                    queued += 1
            continue
        wake_unknown_reconciliation(db, clock, task_id, attempt_id,
                                    event_key=None)
        queued += 1
    return queued


def wake_unknown_reconciliation(
    db: Database, clock: Clock, task_id: int, attempt_id: int, *,
    event_key: str | None = None, conn: sqlite3.Connection | None = None,
) -> int:
    """Queue the one reconciliation chain for an unknown send.

    Automatic first observation uses a stable per-attempt key, so a restart or
    a second call cannot create another chain.  An explicit owner repair reuses
    that chain while it is pending/running; after it has finished, the owner's
    new event id may start a fresh explicit check.
    """
    stable_slot = f"unknown-attempt:{int(attempt_id)}"
    if conn is not None or event_key is None:
        return wake_task(
            db, clock, task_id,
            reason="unknown-reconcile",
            event_key=stable_slot,
            reconcile_only=True,
            conn=conn,
        )
    # The first automatic pass may already have handed off to the bounded
    # backoff job (keyed by its due time), so an owner "repair" must reuse any
    # pending/running reconcile job for this task, not just the stable seed key.
    existing = db.fetchone(
        "SELECT id FROM jobs WHERE kind='publication_agent' "
        "AND state IN ('pending','running') "
        "AND COALESCE(json_extract(payload_json, '$.reconcile_only'), 0)=1 "
        "AND CAST(json_extract(payload_json, '$.task_id') AS INTEGER)=? "
        "ORDER BY id DESC LIMIT 1",
        (int(task_id),),
    )
    if existing is not None:
        return int(existing["id"])
    return wake_task(
        db, clock, task_id,
        reason="unknown-reconcile",
        event_key=event_key,
        reconcile_only=True,
    )


_COMMAND = re.compile(
    r"^(发布状态|检查原因|修复并重试|已登录|取消发布|拒绝发布|暂停发布|继续发布|确认未发布并重试)"
    r"\s*(XH-[A-Z0-9]{4,12})?$",
    re.IGNORECASE,
)


def handle_command(
    db: Database,
    config: Config,
    clock: Clock,
    text: str,
    *,
    event_key: str | None = None,
) -> dict[str, Any]:
    """Handle an owner command without calling a model or the browser.

    The callback only authenticates/dedupes/persists/wakes; all diagnosis and
    submission happens in the worker. Unknown results are never treated as
    “safe to retry”.
    """
    m = _COMMAND.match(str(text or "").strip())
    if m is None:
        return {"ok": False, "outcome": "unknown_publication_command"}
    action = m.group(1)
    code = (m.group(2) or "").upper()
    if not code:
        return {"ok": False, "outcome": "publication_command_needs_code"}
    approval = approvals.get_approval(db, code=code)
    if approval is None:
        return {"ok": False, "outcome": "unknown_approval", "code": code}
    store = PublicationStore(db, clock)
    task = store.by_approval(int(approval["id"]))
    if task is None:
        return {"ok": False, "outcome": "no_publication_task", "code": code}
    if action in {"发布状态", "检查原因"}:
        job_id = None
        if action == "检查原因":
            job_id = wake_task(db, clock, task.id, reason="diagnose", event_key=event_key, diagnose_only=True)
        attempts = db.fetchall(
            "SELECT id,attempt_no,state,error,external_post_id,created_at,finished_at "
            "FROM publish_attempts WHERE publication_task_id=? ORDER BY id DESC LIMIT 5",
            (task.id,),
        )
        return {
            "ok": True,
            "outcome": "publication_status",
            "code": code,
            "task": task.as_dict(),
            "attempts": [dict(a) for a in attempts],
            "job_id": job_id,
        }
    if action in {"取消发布", "拒绝发布"}:
        if task.state in {"published", "cancelled"}:
            return {"ok": True, "outcome": "already_terminal", "code": code, "state": task.state}
        latest_attempt = db.fetchone(
            "SELECT state FROM publish_attempts WHERE publication_task_id=? ORDER BY id DESC LIMIT 1",
            (task.id,),
        )
        unresolved = latest_attempt is not None and str(latest_attempt["state"]) in {"unknown", "publishing"}
        if task.active_attempt_id is not None or task.state in {"submitting", "verifying"} or unresolved:
            store.update(task.id, cancel_requested=True, wait_reason="owner requested cancellation; in-flight result must be reconciled")
            job_id = wake_task(
                db, clock, task.id, reason="cancel-reconcile",
                event_key=event_key, diagnose_only=True,
            )
            return {
                "ok": True, "outcome": "cancel_requested_in_flight", "code": code,
                "state": task.state, "job_id": job_id,
            }
        store.update(task.id, state="cancelled", cancel_requested=True, wait_reason="owner cancelled")
        return {"ok": True, "outcome": "cancelled", "code": code}
    if action == "暂停发布":
        if task.state in {"published", "cancelled"}:
            return {"ok": True, "outcome": "already_terminal", "code": code, "state": task.state}
        store.update(task.id, state="paused", wait_reason="owner paused")
        return {"ok": True, "outcome": "paused", "code": code}
    if action == "继续发布":
        if task.state in {"published", "cancelled"}:
            return {"ok": True, "outcome": "already_terminal", "code": code, "state": task.state}
        store.update(task.id, state="ready", wait_reason="owner resumed")
        store.batch_start(task.id, force=True)
        job_id = wake_task(db, clock, task.id, reason="resume", event_key=event_key)
        return {"ok": True, "outcome": "resumed", "code": code, "job_id": job_id}
    if action == "已登录":
        if task.state in {"published", "cancelled"}:
            return {"ok": True, "outcome": "already_terminal", "code": code, "state": task.state}
        store.update(task.id, state="ready", wait_reason="owner reports login; worker must verify")
        store.batch_start(task.id, force=True)
        job_id = wake_task(db, clock, task.id, reason="login", event_key=event_key)
        return {"ok": True, "outcome": "login_check_queued", "code": code, "job_id": job_id}
    if action == "修复并重试":
        if task.state in {"published", "cancelled"}:
            return {"ok": True, "outcome": "already_terminal", "code": code, "state": task.state}
        latest_attempt = db.fetchone(
            "SELECT id, state FROM publish_attempts WHERE publication_task_id=? ORDER BY id DESC LIMIT 1",
            (task.id,),
        )
        unresolved = (
            task.state == "waiting_owner"
            and task.last_error_class == "unknown"
        ) or (
            latest_attempt is not None
            and str(latest_attempt["state"]) in {"unknown", "publishing"}
        )
        if unresolved:
            # "Repair" asks us to investigate, not to declare the previous send
            # had no effect.  Reuse the automatic reconciliation chain while it
            # is active; only "确认未发布并重试" may authorise a new submission.
            if latest_attempt is not None:
                job_id = wake_unknown_reconciliation(
                    db, clock, task.id, int(latest_attempt["id"]),
                    event_key=event_key,
                )
            else:
                job_id = wake_task(
                    db, clock, task.id, reason="unknown-reconcile",
                    event_key=event_key, reconcile_only=True,
                )
            return {
                "ok": True,
                "outcome": "unknown_requires_reconciliation",
                "code": code,
                "state": task.state,
                "job_id": job_id,
            }
        store.update(task.id, state="ready", wait_reason="owner requested repair/retry")
        store.batch_start(task.id, force=True)
        job_id = wake_task(db, clock, task.id, reason="retry", event_key=event_key)
        return {"ok": True, "outcome": "retry_queued", "code": code, "job_id": job_id}
    if action == "确认未发布并重试":
        if task.state in {"published", "cancelled"}:
            return {"ok": True, "outcome": "already_terminal", "code": code, "state": task.state}
        if task.state == "submitting" or task.state == "verifying" or task.active_attempt_id is not None:
            return {"ok": False, "outcome": "cannot_confirm_while_in_flight", "code": code}
        latest = db.fetchone(
            "SELECT id,state FROM publish_attempts WHERE publication_task_id=? ORDER BY id DESC LIMIT 1",
            (task.id,),
        )
        if latest is None or str(latest["state"]) != "unknown":
            return {"ok": False, "outcome": "no_unknown_attempt_to_confirm", "code": code}
        now = to_iso(clock.now())
        with db.tx() as conn:
            changed = conn.execute(
                "UPDATE publish_attempts SET state='failed', "
                "error=COALESCE(error, 'owner confirmed not published'), "
                "reconcile_note=COALESCE(reconcile_note, 'owner confirmed not published'), "
                "finished_at=COALESCE(finished_at, ?), updated_at=? "
                "WHERE id=? AND state='unknown'",
                (now, now, int(latest["id"])),
            )
            if changed.rowcount != 1:
                return {"ok": False, "outcome": "attempt_changed_before_confirmation", "code": code}
            moved = conn.execute(
                "UPDATE publication_tasks SET state='ready', wait_reason=?, active_attempt_id=NULL, "
                "retry_count=retry_count+1, generation=generation+1, updated_at=? "
                "WHERE id=? AND generation=?",
                ("owner confirmed not published", now, task.id, int(task.generation)),
            )
            if moved.rowcount != 1:
                return {"ok": False, "outcome": "task_changed_before_confirmation", "code": code}
        job_id = wake_task(db, clock, task.id, reason="confirmed_not_published", event_key=event_key)
        return {"ok": True, "outcome": "retry_queued", "code": code, "job_id": job_id}
    return {"ok": False, "outcome": "unsupported_command", "code": code}

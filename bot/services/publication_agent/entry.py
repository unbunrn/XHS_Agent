"""Entry points: approval -> durable publication task -> wake."""

from __future__ import annotations

import re
import sqlite3
from datetime import timedelta, timezone
from typing import Any

from ...config import Config
from ...db import Database
from ...jobs import enqueue_job
from ...timeutil import Clock, parse_iso, to_iso
from .. import approvals
from .diagnostics import format_attempt_status, latest_attempt
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

_STATE_LABELS = {
    "ready": "已排队，等待执行",
    "diagnosing": "正在只读检查",
    "submitting": "正在提交，可能已发出",
    "verifying": "正在核验发布结果",
    "retry_wait": "等待自动重试",
    "waiting_login": "等待扫码登录",
    "waiting_owner": "已暂停，等待你处理",
    "waiting_approval": "等待审批",
    "paused": "已暂停",
    "published": "已发布",
    "simulated": "模拟发布（测试环境，未真实上架）",
    "cancelled": "已取消",
}


def _local_time(value: Any) -> str:
    parsed = parse_iso(str(value)) if value else None
    if parsed is None:
        return "未知"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")


def _latest_attempt(db: Database, task_id: int) -> dict[str, Any] | None:
    return latest_attempt(db, task_id)


def _attempt_line(attempt: dict[str, Any] | None) -> str:
    return format_attempt_status(attempt, include_attempt_id=False)


def _wait_reason_text(reason: str | None) -> str:
    raw = str(reason or "").strip()
    if not raw:
        return ""
    if raw == "publication agent step limit reached":
        return "本轮检查步数已用完，任务已停下等待你决定"
    if raw.startswith("publication model budget exhausted") or raw.startswith("model budget:"):
        return "本轮模型调用预算已用完或不可用"
    if raw.startswith("invalid model decision"):
        return "模型连续给出无效动作，任务已停下"
    if raw.startswith("no progress:"):
        return "连续只读检查没有新发现，任务已停下等待你决定"
    if raw == "owner requested repair/retry":
        return "已收到修复请求"
    if raw.startswith("owner requested cancellation") or raw.startswith("cancellation pending"):
        return "已记录取消请求；在途发送结果仍需核对，不会把在途发送说成已取消"

    if raw == "owner paused":
        return "你已暂停这个任务"
    if "unknown" in raw.lower():
        return "发送结果未知，需要先核对平台"
    return raw


def _next_step(task_state: str, attempt: dict[str, Any] | None, code: str) -> str:
    if task_state == "published":
        return "无需操作；可在小红书查看这篇笔记。"
    if task_state == "simulated":
        return "这是测试记录，没有真实发布；如需真实发布，请重新走审批。"
    if task_state == "cancelled":
        return "如仍需发布，请重新提交作品审批。"
    if task_state == "paused":
        return f"发送“继续发布 {code}”恢复。"
    if task_state == "waiting_login":
        return f"完成扫码后发送“已登录 {code}”。"
    if attempt is not None and str(attempt.get("state") or "") in {"unknown", "publishing"}:
        return f"先发送“检查原因 {code}”只读核对；确认前不要重发。"
    if task_state in {"submitting", "verifying"}:
        return f"发送“发布状态 {code}”稍后再看；不要重发。"
    if task_state == "retry_wait":
        return f"系统会在计划时间自动重试；也可发送“检查原因 {code}”先核对。"
    if task_state in {"ready", "diagnosing"}:
        return f"稍后发送“发布状态 {code}”查看进展。"
    return (
        f"可发送“修复并重试 {code}”继续；"
        f"如果已经在小红书确认没有这篇，再发送“确认未发布并重试 {code}”。"
    )


def _status_text(db: Database, result: dict[str, Any]) -> str:
    task = result.get("task") or {}
    task_id = int(task.get("id") or 0)
    code = str(result.get("code") or task.get("approval_code") or "")
    row = db.fetchone(
        "SELECT t.updated_at, d.title FROM publication_tasks t "
        "LEFT JOIN drafts d ON d.id=t.draft_id WHERE t.id=?",
        (task_id,),
    )
    task_state = str(task.get("state") or "")
    attempt = _latest_attempt(db, task_id)
    lines = [f"发布状态：{code}"]
    title = str(row["title"] or "").strip() if row is not None else ""
    if title:
        lines.append(f"作品：{title[:40]}")
    lines.append(f"当前状态：{_STATE_LABELS.get(task_state, task_state or '未知')}")
    lines.append(_attempt_line(attempt))
    note_id = str((attempt or {}).get("external_post_id") or "").strip()
    if note_id:
        lines.append(f"平台笔记 ID：{note_id}")
    reason = _wait_reason_text(task.get("wait_reason"))
    if reason and task_state not in {"published", "simulated", "cancelled"}:
        lines.append(f"等待原因：{reason}")
    updated = row["updated_at"] if row is not None else ""
    lines.append(f"最后更新：{_local_time(updated)}")
    next_step = _next_step(task_state, attempt, code)
    lines.append(f"下一步：{next_step}")
    if task_state == "published":
        lines.append("这条任务不会再次提交；新作品仍需单独批准。")
    else:
        lines.append("在得到你明确授权前，不会绕过审批或自动重发。")
    return "\n".join(lines)


def format_command_reply(
    db: Database, result: dict[str, Any], *, command_text: str = ""
) -> str:
    """Render an owner-facing Chinese receipt for a publication command.

    This is deliberately deterministic: the model never writes command replies.
    It is the single place that explains whether a send actually happened.
    """
    m = _COMMAND.match(str(command_text or "").strip())
    action = m.group(1) if m is not None else ""
    outcome = str(result.get("outcome") or "")
    code = str(result.get("code") or "")
    prefix = f"{code}：" if code else ""
    if outcome == "publication_status":
        if action == "检查原因":
            if result.get("diagnosis_skipped"):
                return "无需继续检查，任务已经结束：\n" + _status_text(db, result)
            return (
                f"已收到检查请求（{code}）。正在只读检查连接、登录、图片和发送记录，"
                "不会发布或重发。检查完成后会再发结果。"
            )
        return _status_text(db, result)
    if outcome == "unknown_requires_reconciliation":
        return (
            f"{prefix}已收到。上次发送结果未知，现在先只读核对平台，不会直接重发。"
            "确认前请不要把“小红书里没有”当成未发布。"
        )
    if outcome == "retry_queued":
        if action == "确认未发布并重试":
            return (
                f"{prefix}已按你的确认，把上次未知发送记为未发布，并安排重新提交。"
                "只会提交原来批准的那一版。"
            )
        row = db.fetchone(
            "SELECT t.id FROM publication_tasks t "
            "JOIN approvals a ON a.id=t.approval_id WHERE a.code=?",
            (code,),
        )
        attempt = _latest_attempt(db, int(row["id"])) if row is not None else None
        state = str((attempt or {}).get("state") or "")
        if state in {"unknown", "publishing"}:
            return (
                f"{prefix}已收到修复请求。上次发送结果仍未确认，会先只读核对平台；"
                "确认前不会直接重发。"
            )
        if attempt is None:
            return (
                f"{prefix}已接受修复并重试。这篇此前没有发送记录，会从原批准版本继续；"
                "仍会重新核验账号和图片，不会绕过审批。"
            )
        return (
            f"{prefix}已接受修复并重试。上次发送没有成功，会从原批准版本继续；"
            "仍会重新核验账号和图片，不会重复提交旧结果。"
        )
    if outcome == "cancel_requested_in_flight":
        return (
            f"{prefix}已记录取消请求。当前发送可能仍在进行，需要先核对平台结果；"
            "不会把在途发送说成已取消。"
        )
    if outcome == "cancelled":
        return f"{prefix}已取消这篇的后续发布；已发到平台的帖子不会自动撤回。"
    if outcome == "paused":
        return f"{prefix}已暂停。发送“继续发布 {code}”可恢复。"
    if outcome == "resumed":
        return f"{prefix}已恢复，正在安排继续；仍需通过原来的批准和账号校验。"
    if outcome == "login_check_queued":
        return f"{prefix}已收到登录完成，后台会重新核验账号后再继续；不会跳过校验。"
    if outcome == "already_terminal":
        state = str(result.get("state") or "")
        label = _STATE_LABELS.get(state, state or "已结束")
        return f"{prefix}这篇已经处于“{label}”，本次没有做改动。"
    if outcome == "cannot_confirm_while_in_flight":
        return f"{prefix}当前仍在提交或核验，不能确认“未发布”；请稍后查状态，不要重发。"
    if outcome == "no_unknown_attempt_to_confirm":
        return f"{prefix}没有待确认的未知发送，因此没有重试。"
    if outcome == "unknown_approval":
        return f"{prefix}找不到这个审批码，请核对飞书审批消息里的 XH 编号。"
    if outcome == "no_publication_task":
        return f"{prefix}这个审批还没有对应的发布任务；请重新提交审批或发送“帮助”查看命令。"
    if outcome == "publication_command_needs_code":
        return "这条发布命令缺少 XH 审批码。请从审批消息里带上编号，例如“发布状态 XH-XXXXXX”。"
    if outcome in {"attempt_changed_before_confirmation", "task_changed_before_confirmation"}:
        return (
            f"{prefix}这次确认没有执行：任务或发送状态在确认前已经变化。"
            f"请先发送“发布状态 {code}”核对，不要直接重发。"
        )
    if outcome in {"unsupported_command", "unknown_publication_command"}:
        return "这条发布命令没有执行。请发送“帮助”查看可用命令和正确格式。"
    return (
        f"{prefix}命令已处理（{outcome or '未知结果'}）。"
        f"发送“发布状态 {code}”可查看当前进度。" if code else
        "命令没有执行成功。发送“帮助”查看可用命令。"
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
        diagnosis_skipped = False
        if action == "检查原因":
            if task.state in {"published", "cancelled", "simulated"}:
                # There is nothing left to diagnose.  Answer from the durable
                # record instead of queueing a job that would exit immediately
                # and never send the promised follow-up.
                diagnosis_skipped = True
            else:
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
            "diagnosis_skipped": diagnosis_skipped,
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

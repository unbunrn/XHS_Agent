"""Application entry points for Creator Agent V2.

This module connects the model-driven loop to the existing durable job queue and
the existing notification outbox.  It deliberately does not call a live model,
Feishu, or Xiaohongshu by itself; callers inject the runner/runtime.  The demo
tests use the same functions with fake adapters.
"""

from __future__ import annotations

import json
import re
from hashlib import sha256
from typing import Any, Callable

from bot.db import Database, json_dumps
from bot.jobs import enqueue_job
from bot.services.notifications import queue_outcome
from bot.timeutil import to_iso

from .contracts import ArtifactVersion, CreativeBrief
from .runner import AgentResult, AgentRunner


def run_key_for(account_id: str, message_id: str) -> str:
    return "ca-" + sha256(f"{account_id}|{message_id}".encode()).hexdigest()[:20]


_COMMAND_PREFIXES = (
    "V2 选题",
    "V2 写一篇",
    "V2 修改",
    "V2修改",
    "V2",
    "选题",
    "写一篇",
    "修改",
    "改一下",
)


def strip_command_prefix(text: str) -> str:
    """Remove one known command prefix, case-insensitively.

    Feishu clients can lowercase the leading ASCII token (``v2``), so a
    case-sensitive ``startswith`` would leak the command into the brief topic.
    Longest-first ordering prevents ``v2`` from consuming ``v2 选题``.
    """
    raw = str(text or "").strip()
    lowered = raw.lower()
    for prefix in sorted(_COMMAND_PREFIXES, key=len, reverse=True):
        if lowered.startswith(prefix.lower()):
            return raw[len(prefix) :].strip(" ：:，,")
    return raw


def brief_from_text(
    *,
    account_id: str,
    message_id: str,
    text: str,
    origin: str = "manual",
    generation: int = 1,
    base_artifact_version: int | None = None,
    must_keep: list[str] | None = None,
    may_change: list[str] | None = None,
) -> CreativeBrief:
    raw = strip_command_prefix(text)
    if not raw:
        raise ValueError("empty creator brief")
    return CreativeBrief(
        run_key=run_key_for(account_id, message_id),
        account_id=account_id,
        origin=origin,  # type: ignore[arg-type]
        topic=raw[:500],
        instruction=raw[:2000],
        generation=int(generation),
        base_artifact_version=base_artifact_version,
        must_keep=list(must_keep or []),
        may_change=list(may_change or []),
    )


def enqueue_brief(
    db: Database,
    clock,
    brief: CreativeBrief,
    *,
    priority: int = 0,
) -> int:
    return enqueue_job(
        db,
        "creator_agent_run",
        f"creator-run:{brief.run_key}:g{brief.generation}",
        payload={"brief": brief.as_dict()},
        priority=priority,
        max_attempts=2,
        clock=clock,
    )


def accept_inbound(
    db: Database,
    clock,
    *,
    account_id: str,
    message_id: str,
    text: str,
    sender_id: str = "owner",
    chat_type: str = "p2p",
    origin: str = "manual",
    generation: int = 1,
    base_artifact_version: int | None = None,
) -> dict[str, Any]:
    """Deduplicate an owner message and enqueue one bounded agent run."""
    existing = db.fetchone("SELECT * FROM inbound_messages WHERE message_id=?", (message_id,))
    if existing is not None:
        return {"ok": True, "duplicate": True, "outcome": existing["outcome"], "message_id": message_id}
    brief = brief_from_text(
        account_id=account_id,
        message_id=message_id,
        text=text,
        origin=origin,
        generation=generation,
        base_artifact_version=base_artifact_version,
    )
    now = to_iso(clock.now())
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO inbound_messages(message_id, channel, sender_id, chat_id, chat_type, text, "
            "received_at, outcome, detail) VALUES (?, 'feishu', ?, NULL, ?, ?, ?, 'accepted', ?)",
            (message_id, sender_id, chat_type, text, now, brief.run_key),
        )
        job_id = enqueue_job(
            db,
            "creator_agent_run",
            f"creator-run:{brief.run_key}:g{brief.generation}",
            payload={"brief": brief.as_dict()},
            clock=clock,
            conn=conn,
        )
    return {"ok": True, "duplicate": False, "brief": brief.as_dict(), "job_id": job_id}


def _revision_target(
    db: Database, account_id: str, approval_code: str | None = None
) -> dict[str, Any] | None:
    """Latest V2 submission in this account that can still be revised.

    An explicit approval code narrows the target; otherwise the latest
    submitted artifact in the same account is used.  A target from another
    account is never selected.
    """
    sql = (
        "SELECT ca.run_key, ca.version, ca.draft_id, r.generation, "
        "r.brief_json AS brief_json, a.account_id AS account_id, "
        "a.id AS approval_id, a.code AS approval_code, a.status AS approval_status "
        "FROM creator_agent_artifacts ca "
        "JOIN creator_agent_runs r ON r.run_key=ca.run_key "
        "JOIN drafts d ON d.id=ca.draft_id "
        "JOIN approvals a ON a.draft_id=d.id "
        "WHERE ca.status='submitted' AND a.account_id=? "
        "AND a.status IN ('pending','approved','revision_pending')"
    )
    params: list[Any] = [account_id]
    if approval_code:
        sql += " AND a.code=?"
        params.append(approval_code)
    sql += " ORDER BY ca.rowid DESC LIMIT 1"
    row = db.fetchone(sql, tuple(params))
    return dict(row) if row is not None else None


def accept_revision(
    db: Database,
    clock,
    *,
    account_id: str,
    message_id: str,
    text: str,
    sender_id: str,
    chat_type: str,
) -> dict[str, Any]:
    """Bind a V2 revision to the latest submitted artifact and freeze its approval."""
    raw = strip_command_prefix(text)
    if not raw:
        return {"ok": False, "outcome": "revision_needs_instruction"}
    import re

    explicit = re.search(r"(?i)\bXH-[A-Z0-9]{4,12}\b", raw)
    target = _revision_target(
        db, account_id, explicit.group(0).upper() if explicit else None
    )
    if target is None:
        return {"ok": False, "outcome": "revision_no_target"}
    ordinal_words = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    must_keep: list[str] = []
    for match in re.finditer(r"(?:保留|原样保留|不改).{0,8}?第([一二三四五六七八九十\d]+)张", raw):
        token = match.group(1)
        ordinal = int(token) if token.isdigit() else ordinal_words.get(token)
        if ordinal:
            value = f"image:{ordinal}"
            if value not in must_keep:
                must_keep.append(value)
    try:
        parent_brief = json.loads(str(target.get("brief_json") or "{}"))
    except (TypeError, ValueError):
        parent_brief = {}
    parent_topic = str(parent_brief.get("topic") or target["run_key"])
    brief = CreativeBrief(
        run_key=run_key_for(account_id, message_id),
        account_id=account_id,
        origin="revision",
        topic=parent_topic,
        instruction=raw[:2000],
        generation=int(target["generation"]) + 1,
        base_artifact_version=int(target["version"]),
        parent_run_key=str(target["run_key"]),
        must_keep=must_keep,
    )
    now = to_iso(clock.now())
    with db.tx() as conn:
        existing = conn.execute(
            "SELECT * FROM inbound_messages WHERE message_id=?", (message_id,)
        ).fetchone()
        if existing is not None:
            return {
                "ok": True, "duplicate": True, "outcome": existing["outcome"],
                "message_id": message_id,
            }
        conn.execute(
            "UPDATE approvals SET status='revision_pending', decided_at=?, decided_by=?, "
            "decision_comment=? WHERE id=? AND status IN ('pending','approved')",
            (now, sender_id, f"V2 revision requested: {raw[:500]}", target["approval_id"]),
        )
        conn.execute(
            "UPDATE drafts SET status='revision_pending', updated_at=? WHERE id=? "
            "AND status IN ('drafted','review_pending','approved')",
            (now, target["draft_id"]),
        )
        conn.execute(
            "UPDATE events SET status='revision_pending' WHERE id="
            "(SELECT event_id FROM drafts WHERE id=?) AND status NOT IN ('published','simulated','rejected')",
            (target["draft_id"],),
        )
        conn.execute(
            "UPDATE jobs SET state='cancelled', lease_owner=NULL, lease_until=NULL, "
            "last_error=?, updated_at=? WHERE kind='publish' AND dedupe_key=? AND state='pending'",
            ("approval moved to revision_pending", now,
             f"publish:approval:{target['approval_id']}"),
        )
        conn.execute(
            "INSERT INTO inbound_messages(message_id, channel, sender_id, chat_id, chat_type, text, "
            "received_at, outcome, detail) VALUES (?, 'feishu', ?, NULL, ?, ?, ?, 'accepted', ?)",
            (message_id, sender_id, chat_type, text, now, brief.run_key),
        )
        job_id = enqueue_job(
            db, "creator_agent_run", f"creator-run:{brief.run_key}:g{brief.generation}",
            payload={"brief": brief.as_dict()}, clock=clock, conn=conn,
        )
    return {
        "ok": True, "duplicate": False, "outcome": "creator_revision_queued",
        "brief": brief.as_dict(), "job_id": job_id,
        "base_run_key": target["run_key"], "base_version": target["version"],
        "invalidated_approval": target["approval_code"],
    }


def load_brief_from_job(job) -> CreativeBrief:
    payload = json.loads(job["payload_json"] or "{}")
    raw = payload.get("brief") or {}
    return CreativeBrief(**raw)


def queue_artifact_outbox(
    db: Database,
    clock,
    *,
    result: AgentResult,
    notification_kind: str = "creator_artifact_ready",
) -> int | None:
    """Write one idempotent outbox row for a submitted artifact.

    The row is not an approval: the owner still has to approve through the
    existing approval boundary.  It is the handoff/notification record for the
    agent artifact and deliberately carries no recipient ID.
    """
    if result.status != "completed" or result.artifact is None:
        return None
    artifact = result.artifact
    # A live submit already created a real pending approval; do not enqueue a
    # second fake "artifact ready" notification in that case.
    if artifact.draft_id is not None:
        return None
    dedupe = f"creator-artifact:{artifact.run_key}:v{artifact.version}"
    payload = {
        "run_key": artifact.run_key,
        "version": int(artifact.version),
        "title": artifact.title,
        "body": artifact.body,
        "images": artifact.images,
        "status": artifact.status,
        "note": "agent artifact ready; owner approval is required before publish",
    }
    now = to_iso(clock.now())
    with db.tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO notifications(kind, dedupe_key, channel, payload_json, state, attempts, created_at) "
            "VALUES (?, ?, 'fake', ?, 'pending', 0, ?)",
            (notification_kind, dedupe, json_dumps(payload), now),
        )
        row = conn.execute("SELECT id FROM notifications WHERE dedupe_key=?", (dedupe,)).fetchone()
    return int(row["id"]) if row is not None else None


def run_job(
    db: Database,
    config,
    clock,
    job,
    *,
    runner_factory: Callable[[CreativeBrief], AgentRunner],
) -> AgentResult:
    brief = load_brief_from_job(job)
    runner = runner_factory(brief)
    result = runner.run(brief)
    queue_artifact_outbox(db, clock, result=result)
    if result.status != "completed":
        # A terminal/failed run must produce one owner-visible outcome; silence
        # looks like the bot ignored the message.  A later successful resume can
        # still create its normal approval card.
        label = {
            "failed": "V2 创作未完成",
            "deferred": "V2 创作已暂停",
            "needs_owner": "V2 创作需要你补充信息",
        }.get(result.status, f"V2 创作状态：{result.status}")
        queue_outcome(
            db,
            config,
            clock,
            (
                f"{label}（run {result.run_key}，{result.steps} 步）。\n"
                f"原因：{(result.error or '未返回错误详情')[:300]}\n"
                "没有生成审批，也没有发布。"
            ),
            dedupe_key=f"creator-outcome:{result.run_key}:{result.status}",
        )
    return result


def scheduled_tick(
    db: Database,
    clock,
    *,
    account_id: str,
    discover: Callable[[], list[dict[str, Any]]],
    max_topics: int = 3,
) -> list[int]:
    """Enqueue at most ``max_topics`` new scheduled runs.

    The caller supplies discovery (RSS/AIHot/etc.).  Dedupe is based on the
    topic text + source URL, so a scheduler restart cannot duplicate a run.
    """
    ids: list[int] = []
    for item in discover()[: max(0, int(max_topics))]:
        topic = str(item.get("topic") or item.get("title") or "").strip()
        if not topic:
            continue
        source = str(item.get("source_url") or item.get("url") or "").strip()
        summary = str(item.get("summary") or "").strip()
        mid = "sched-" + sha256(f"{account_id}|{topic}|{source}".encode()).hexdigest()[:20]
        instruction = f"发现选题：{topic}"
        if source:
            instruction += f"\n线索链接：{source}"
        if summary:
            instruction += f"\n线索摘要：{summary[:800]}"
        brief = CreativeBrief(
            run_key=mid,
            account_id=account_id,
            origin="scheduled",
            topic=topic[:500],
            instruction=instruction[:2000],
            source_url=source,
        )
        ids.append(enqueue_brief(db, clock, brief))
    return ids

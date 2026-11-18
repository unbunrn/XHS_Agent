"""The publish gate.

Only this module may call a publisher.  The sequence is deliberately strict:

1. re-check global pause, approval status/expiry/schedule, account state, source
   state, daily publish budget, approval fingerprint and the current body/image
   bytes;
2. transactionally claim the approval by inserting a UNIQUE
   ``publish_attempts.dedupe_key`` and moving the approval to ``publishing``;
3. call the publisher *outside* the transaction;
4. record exactly one terminal state.

A crash between (2) and (4) is resolved as ``publish_unknown`` at restart and is
never resent.  ``published`` is only written when the adapter returns a real
platform post id or URL.
"""

# =============================================================================
# 中文导读：发布闸门——全项目唯一允许“按发布按钮”的地方
# =============================================================================
# 打个比方：前面几个模块只是把菜做好、摆盘、请老板签字；
# 这个模块才是真正把菜端出门的那道门。所有检查都在这里再做一遍，
# 因为只要有一项漏掉，就可能把没批准的内容发到公开平台。
#
# 发布前依次核对（任何一项不通过就 blocked）：
#   1. 全局暂停开关有没有关掉一切；
#   2. 审批是否 approved、是否到了计划时间、是否还没过期；
#   3. 草稿版本、正文哈希、图片哈希是否仍与审批时完全一致；
#   4. 事件是否还有可用来源、来源是否被禁用；
#   5. live 模式还要求账号 enabled 且 verified；
#   6. 今天是否超过每日发布上限；
#   7. 这个审批是不是已经有过发布尝试。
#
# 然后是“先占坑、再出门”：
#   在一个数据库事务里插入 publish_attempts（dedupe_key 唯一）并把
#   审批改成 publishing。这样即使两个进程同时抢，也只有一个能成功；
#   抢到坑位之后才去调用发布器，而且调用发生在事务外面
#   （网络请求可能很慢，不能一直占着数据库写锁）。
#
# 四种结果（与 adapters/publishers.py 保持一致）：
#   published  有平台回执，真的发出去了
#   simulated  只是本地模拟导出
#   failed     确定没发出去
#   unknown    可能发出去了；绝不自动重试，重启后变成 publish_unknown，
#              只能由人用 reconcile_unknown 去平台核对后手动收尾。
#
# 为什么 unknown 不能重试？
#   重试可能造成同一篇内容发两遍。平台不会帮你判重，
#   粉丝会看到重复内容，账号还可能被限流。宁可停下来等人工确认。
# =============================================================================

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ..adapters.publishers import PublishOutcome, PublishRequest, Publisher, build_publisher
from ..adapters.types import NotConfigured, PublishFailed, PublishUnknown
from ..audit import audit
from ..config import Config
from ..db import Database, json_dumps, json_loads
from ..security import images_digest, sha256_file
from ..timeutil import Clock, local_day_bounds_utc, parse_iso, to_iso
from . import approvals
from .publication_results import latest_for_approval, record_result


class PublishGateError(RuntimeError):
    # 发布闸门自身的错误：审批找不到、没有发布尝试可对账、
    # 人工对账参数不合法等。调用方看到它应当停下并报告，而不是重试。
    pass


def _attempts_today(
    db: Database, config: Config, clock: Clock, *, conn: sqlite3.Connection | None = None, manual: bool | None = None
) -> int:
    # 统计“本自然日”已经创建了多少次发布尝试。
    # 注意用的是配置时区下的自然日（比如北京时间 0 点到 24 点），
    # 不是 UTC 日，否则用户在晚上会看到限额在奇怪的时间点重置。
    # local_day_bounds_utc 把本地日转换成 UTC 的起止时间，再和库里的
    # ISO 时间字符串比较。
    start, end = local_day_bounds_utc(clock.now(), config.timezone_name)
    # Only an explicitly reconciled non-publication releases the daily slot.
    # Ordinary failures and unknown/in-flight attempts still count conservatively.
    # Count works, not retries: a safe retry of the same frozen package must
    # not consume another daily slot. Schema 10 has no task id, so fall back to
    # the legacy attempt count there. Legacy attempts without a task id also
    # count once per approval.
    rows = (
        conn.execute("PRAGMA table_info(publish_attempts)").fetchall()
        if conn is not None else
        db.fetchall("PRAGMA table_info(publish_attempts)")
    )
    # sqlite3.Row supports mapping access; a raw sqlite3.Connection returns
    # tuples.  Keep this helper usable from both the Runtime and the legacy
    # direct-connection quota tests.
    columns = {
        str(row["name"]) if hasattr(row, "keys") else str(row[1])
        for row in rows
    }
    if "publication_task_id" in columns:
        counted = (
            "COUNT(DISTINCT CASE WHEN p.publication_task_id IS NOT NULL "
            "THEN 'task:' || p.publication_task_id ELSE 'approval:' || p.approval_id END)"
        )
    else:
        counted = "COUNT(*)"
    sql = (
        "SELECT " + counted + " FROM publish_attempts p WHERE p.created_at >= ? AND p.created_at < ? "
        "AND NOT (p.state='failed' AND EXISTS (SELECT 1 FROM approvals a "
        "WHERE a.id=p.approval_id AND a.status='reconciled_not_published'))"
    )
    if manual is not None:
        membership = "EXISTS" if manual else "NOT EXISTS"
        sql += (" AND " + membership + " (SELECT 1 FROM drafts d JOIN topic_requests t "
                "ON t.event_id=d.event_id WHERE d.id=p.draft_id)")
    params = (to_iso(start), to_iso(end))
    if conn is not None:
        row = conn.execute(sql, params).fetchone()
        return int(row[0] if row is not None else 0)
    return int(db.scalar(sql, params) or 0)


def _existing_attempt(
    db: Database, approval_id: int, *, conn: sqlite3.Connection | None = None
) -> sqlite3.Row | None:
    # 一个审批最多对应一次发布尝试。只要查到记录，就说明已经试过了。
    # 这是“绝不重发”在第一道入口的检查；真正的并发保护在 claim_publish
    # 的 UNIQUE 约束里。
    sql = "SELECT * FROM publish_attempts WHERE approval_id=?"
    if conn is not None:
        return conn.execute(sql, (int(approval_id),)).fetchone()
    return db.fetchone(sql, (int(approval_id),))


def _account_check(config: Config, publisher: Publisher) -> tuple[bool, str]:
    # 账号检查。mock 模式允许在账号还没绑定时先演练；
    # live 模式必须 enabled=true 且 status="verified"，缺一不可。
    # 这样即使配置里误开了 live，也不会往一个没验证过的账号发内容。
    account = config.account()
    enabled = bool(account.get("enabled", False))
    status = str(account.get("status", "unverified"))
    if publisher.mode == "live":
        if not enabled:
            return False, "account is not enabled in config"
        if status != "verified":
            return False, f"account status is {status!r}, not verified"
    # Mock mode is allowed to simulate a work even before the account is bound,
    # but it never claims the account is connected.
    return True, "ok"


def _source_check(
    db: Database,
    draft: sqlite3.Row,
    publisher: Publisher,
    *,
    conn: sqlite3.Connection | None = None,
) -> tuple[bool, str]:
    # 来源检查：草稿关联的选题（事件）必须仍然有效，而且至少有一条
    # “未重复、来自已启用来源”的证据。为什么要现查而不是信草稿创建时的结果？
    # 因为从写稿到审批再到发布之间，用户可能已经禁用了某个来源，
    # 或者把事件标成 rejected/expired。发布前必须以数据库当前状态为准。
    event = (
        conn.execute("SELECT * FROM events WHERE id=?", (draft["event_id"],)).fetchone()
        if conn is not None
        else db.fetchone("SELECT * FROM events WHERE id=?", (draft["event_id"],))
    )
    if event is None:
        return False, "event no longer exists"
    if event["status"] in {"rejected", "expired", "published", "simulated"}:
        return False, f"event status is {event['status']}"
    if event["reject_reason"] and event["status"] == "rejected":
        return False, f"event was rejected: {event['reject_reason']}"
    # 只有“原始条目没被判定为重复 + 来源仍启用”的证据才算数。
    # 一条都没剩，就不能发。
    usable_sql = (
        "SELECT COUNT(*) FROM evidence ev JOIN raw_items raw ON raw.id=ev.raw_item_id "
        "JOIN sources s ON s.id=raw.source_id "
        "WHERE ev.event_id=? AND raw.duplicate_of IS NULL AND s.enabled=1"
    )
    total_sql = "SELECT COUNT(*) FROM evidence WHERE event_id=?"
    if conn is not None:
        usable_row = conn.execute(usable_sql, (event["id"],)).fetchone()
        usable = int(usable_row[0] if usable_row is not None else 0)
    else:
        usable = int(db.scalar(usable_sql, (event["id"],)) or 0)
    if usable < 1:
        return False, "no enabled source evidence remains for this event"
    # 再查一次“总证据数”，与上面的“可用证据数”分开：
    # 两者都为 0 时给出不同提示，方便用户判断是没采到还是来源被禁用。
    if conn is not None:
        total_row = conn.execute(total_sql, (event["id"],)).fetchone()
        total = int(total_row[0] if total_row is not None else 0)
    else:
        total = int(db.scalar(total_sql, (event["id"],)) or 0)
    if total < 1:
        return False, "event has no evidence"

    # Defence in depth: even if a caller bypasses the config-level demo guard
    # (for example by passing a custom live publisher), a live request must
    # never contain fixture evidence or fake/demo-generated copy.
    # 下面这段是“纵深防御”：即使调用方绕过配置层演示保护、
    # 自己塞了一个 live 发布器，也不能让 fixture/demo 来源或假模型写的稿
    # 真的发出去。配置检查和代码检查各做一遍，漏一个还有另一个。
    if publisher.mode == "live":
        kinds_sql = (
            "SELECT DISTINCT s.kind FROM evidence ev "
            "JOIN raw_items raw ON raw.id=ev.raw_item_id "
            "JOIN sources s ON s.id=raw.source_id "
            "WHERE ev.event_id=? AND raw.duplicate_of IS NULL AND s.enabled=1"
        )
        if conn is not None:
            kind_rows = conn.execute(kinds_sql, (event["id"],)).fetchall()
        else:
            kind_rows = db.fetchall(kinds_sql, (event["id"],))
        kinds = [str(row["kind"]).lower() for row in kind_rows]
        if any(kind in {"fixture", "demo"} for kind in kinds):
            return False, "live publish refuses fixture/demo source evidence"
        # 检查草稿里记录的生成器。fake/demo 生成的内容只能演练，
        # 不能进入真实发布；model 以 "fake" 开头也一样拒绝。
        body = json_loads(draft["body_json"], {}) or {}
        generator = str(body.get("generator", "")).lower()
        model = str(body.get("model", "")).lower()
        if generator in {"fake", "demo"} or model.startswith("fake"):
            return False, "live publish refuses a fake/demo generated draft"
    return True, "ok"


def _mode_freshness_check(db: Database, config: Config, clock: Clock, draft: sqlite3.Row, conn=None) -> tuple[bool, str]:
    """Re-check the persisted research mode at publish time.

    NEWS is still a 72h product at approval time; a long approval delay must not
    turn old material into news.  ANALYSIS may intentionally use old material,
    but the draft and its originating request must agree.  Unknown dates are not
    rewritten to "today"; analysis may proceed only with its honest date label.
    """
    mode = str(draft["research_mode"] or "news")
    if mode not in {"news", "analysis"}:
        return False, f"unknown research mode: {mode}"
    request = None
    if draft["topic_request_id"] is not None:
        query = "SELECT * FROM topic_requests WHERE id=?"
        args = (int(draft["topic_request_id"]),)
    else:
        query = "SELECT * FROM topic_requests WHERE event_id=? ORDER BY id DESC LIMIT 1"
        args = (int(draft["event_id"]),)
    request = (conn.execute(query, args).fetchone() if conn is not None else db.fetchone(query, args))
    if request is not None and str(request["research_mode"] or "news") != mode:
        return False, "draft research mode differs from its topic request"
    if mode == "analysis":
        return True, "ok"
    event = (conn.execute("SELECT * FROM events WHERE id=?", (draft["event_id"],)).fetchone()
             if conn is not None else db.fetchone("SELECT * FROM events WHERE id=?", (draft["event_id"],)))
    if event is None:
        return False, "event no longer exists"
    latest = parse_iso(event["latest_published_at"]) if event["latest_published_at"] else None
    if latest is not None:
        age_hours = (clock.now() - latest).total_seconds() / 3600
        if age_hours > float(config.get("limits.max_age_hours", 72)):
            return False, f"news material is {age_hours:.1f}h old; limit is {config.get('limits.max_age_hours', 72)}h"
    return True, "ok"


def _artifact_check(
    db: Database,
    approval: sqlite3.Row,
    draft: sqlite3.Row,
    *,
    conn: sqlite3.Connection | None = None,
    allow_reopen_draft: bool = False,
) -> tuple[bool, str, list[dict[str, Any]]]:
    # “验货”：批准的是哪一版，现在发的就必须是哪一版。
    # 先让 approvals.verify_approval_artifact 把审批指纹、正文哈希重算一遍，
    # 再补查草稿版本、草稿状态、图片清单和每个图片文件的真实字节哈希。
    # 任何一项变了，都说明“此物非彼物”，必须拦下。
    ok, reason, images = approvals.verify_approval_artifact(db, approval, conn=conn)
    if not ok:
        return False, reason, images
    # 草稿可能被改过并生成了新版本，而审批还停留在旧版本。
    if int(draft["version"]) != int(approval["draft_version"]):
        return False, "draft version differs from approval", images
    if str(draft["status"]) != "approved" and not (
        allow_reopen_draft and str(draft["status"]) in {"failed", "publish_unknown"}
    ):
        return False, f"draft status is {draft['status']}, not approved", images
    if not draft["images_sha256"]:
        return False, "draft has no image digest", images
    if images_digest(images) != approval["images_sha256"]:
        return False, "image digest does not match approval", images
    # 逐张重新读取文件算 sha256，而不是只相信数据库里存的哈希。
    # 这样即使有人手动替换了 PNG，也会在这里被发现。
    for image in images:
        if not str(image.get("path")):
            return False, "image path missing", images
        try:
            actual = sha256_file(image["path"])
        except OSError:
            return False, "approved image file is missing", images
        if actual != image["sha256"]:
            return False, "approved image bytes changed", images
    return True, "ok", images


def _due_reason(approval: sqlite3.Row, clock: Clock, *, allow_reopen: bool = False) -> tuple[bool, str]:
    # 判断“现在该不该发”：必须 approved、时间戳可解析、还没过期、
    # 且已到计划时间。scheduled_at 是“最早可以发”的时间，
    # expires_at 是“最晚必须发”的时间，两者之间才是窗口。
    if approval["status"] != "approved" and not (
        allow_reopen and approval["status"] in {"failed", "publish_unknown"}
    ):
        return False, f"approval status is {approval['status']}"
    now = clock.now()
    expires = parse_iso(approval["expires_at"])
    scheduled = parse_iso(approval["scheduled_at"])
    if expires is None or scheduled is None:
        return False, "approval has invalid timestamps"
    # 过期和“还没到点”是两回事：前者需要让用户重新生成审批，
    # 后者只需要等一会儿。
    if expires <= now:
        return False, "approval expired"
    if scheduled > now:
        return False, "not yet scheduled"
    return True, "ok"


def _frozen_package_check(task: sqlite3.Row, draft: sqlite3.Row, approval: sqlite3.Row, images: list[dict[str, Any]]) -> tuple[bool, str]:
    package = json_loads(task["approved_package_json"], {}) or {}
    expected_tags = list((json_loads(draft["body_json"], {}) or {}).get("tags") or [])
    if str(package.get("title") or "") != str(draft["title"]):
        return False, "frozen package title no longer matches draft"
    if str(package.get("body_sha256") or "") != str(approval["body_sha256"]):
        return False, "frozen package body hash changed"
    if int(package.get("draft_version") or 0) != int(draft["version"]):
        return False, "frozen package draft version changed"
    if list(package.get("tags") or []) != expected_tags:
        return False, "frozen package tags changed"
    if str(package.get("visibility") or "public") != "public":
        return False, "frozen package visibility is not public"
    frozen_images = [
        (int(i.get("ordinal")), str(i.get("path") or ""), str(i.get("sha256") or ""))
        for i in package.get("images") or []
    ]
    actual_images = [
        (int(i["ordinal"]), str(i["path"]), str(i["sha256"]))
        for i in sorted(images, key=lambda x: int(x["ordinal"]))
    ]
    if frozen_images != actual_images:
        return False, "frozen package images changed or out of order"
    if str(package.get("body_sha256") or "") != str(approval["body_sha256"]):
        return False, "frozen package approval hash changed"
    return True, "ok"


def _validate_publish_conn(
    db: Database,
    conn: sqlite3.Connection,
    config: Config,
    clock: Clock,
    approval: sqlite3.Row,
    publisher: Publisher,
    *,
    task_id: int | None = None,
    attempt_no: int | None = None,
    allow_reopen: bool = False,
    task_generation: int | None = None,
    package_hash: str | None = None,
    lease_owner: str | None = None,
) -> tuple[bool, str, sqlite3.Row | None, list[dict[str, Any]]]:
    """All publish preconditions, evaluated through one database connection.

    ``claim_publish`` calls this with its write transaction connection, so the
    quota, expiry, artifact and source checks cannot be satisfied by stale data
    read before the claim.
    """
    if bool(config.get("app.pause_all", False)):
        return False, "global pause is on", None, []
    due, reason = _due_reason(
        approval, clock, allow_reopen=bool(task_id is not None and allow_reopen)
    )
    if not due:
        return False, reason, None, []
    draft = conn.execute("SELECT * FROM drafts WHERE id=?", (approval["draft_id"],)).fetchone()
    if draft is None:
        return False, "draft no longer exists", None, []
    ok, reason, images = _artifact_check(
        db, approval, draft, conn=conn,
        allow_reopen_draft=bool(task_id is not None and allow_reopen),
    )
    if not ok:
        return False, reason, draft, images
    ok, reason = _mode_freshness_check(db, config, clock, draft, conn=conn)
    if not ok:
        return False, reason, draft, images
    ok, reason = _source_check(db, draft, publisher, conn=conn)
    if not ok:
        return False, reason, draft, images
    configured_account = str(config.account().get("id") or "")
    if configured_account and str(approval["account_id"]) != configured_account:
        return False, "approval belongs to a different account", draft, images
    ok, reason = _account_check(config, publisher)
    if not ok:
        return False, reason, draft, images
    # 每日限额按“尝试次数”算，而不是成功次数：即使失败也占名额，
    # 防止程序在出问题时不停重试、把账号刷爆。
    manual = conn.execute("SELECT 1 FROM topic_requests WHERE event_id=? LIMIT 1", (draft["event_id"],)).fetchone() is not None
    quota_key = "limits.max_manual_publish_per_day" if manual else "limits.max_publish_per_day"
    if _attempts_today(db, config, clock, conn=conn, manual=manual) >= int(
        config.get(quota_key, 1)
    ):
        return False, "daily publish limit reached", draft, images
    if task_id is None:
        if _existing_attempt(db, int(approval["id"]), conn=conn) is not None:
            return False, "a publish attempt already exists for this approval", draft, images
    else:
        task = conn.execute(
            "SELECT state, cancel_requested, generation, approved_package_hash, "
            "active_attempt_id, lease_owner, approved_package_json FROM publication_tasks WHERE id=?",
            (int(task_id),),
        ).fetchone()
        if task is None:
            return False, "publication task does not exist", draft, images
        if task["state"] not in {"ready", "diagnosing", "retry_wait"}:
            return False, "publication task is not in an executable state", draft, images
        if int(task["cancel_requested"] or 0):
            return False, "publication task cancellation requested", draft, images
        if task_generation is not None and int(task["generation"]) != int(task_generation):
            return False, "publication task generation changed", draft, images
        if package_hash is not None and str(task["approved_package_hash"]) != str(package_hash):
            return False, "publication task package changed", draft, images
        if lease_owner is not None and str(task["lease_owner"] or "") != str(lease_owner):
            return False, "publication task lease changed", draft, images
        ok, reason = _frozen_package_check(task, draft, approval, images)
        if not ok:
            return False, reason, draft, images
        if attempt_no is not None:
            cap = int(config.get("limits.max_publish_attempts_per_task", 3))
            prior = int(conn.execute(
                "SELECT COUNT(*) FROM publish_attempts WHERE publication_task_id=?",
                (int(task_id),),
            ).fetchone()[0])
            if prior >= cap:
                return False, "publication task attempt limit reached", draft, images
        if task["active_attempt_id"] is not None:
            return False, "publication task already has an active attempt", draft, images
        unresolved = conn.execute(
            "SELECT 1 FROM publish_attempts WHERE publication_task_id=? "
            "AND state IN ('publishing','unknown') LIMIT 1",
            (int(task_id),),
        ).fetchone()
        if unresolved is not None:
            return False, "publication task has an unresolved attempt", draft, images
    return True, "ok", draft, images


def validate_publish(
    db: Database, config: Config, clock: Clock, approval: sqlite3.Row, publisher: Publisher,
    *, task_id: int | None = None, attempt_no: int | None = None, allow_reopen: bool = False,
    task_generation: int | None = None, package_hash: str | None = None,
    lease_owner: str | None = None,
) -> tuple[bool, str, sqlite3.Row | None, list[dict[str, Any]]]:
    # 只读检查，不改数据库。返回 (是否通过, 原因, 草稿行, 图片清单)。
    # 顺序有讲究：先查最便宜的全局开关，再查时间，再查内容，
    # 最后查账号和限额——越贵的检查越靠后，失败时也更容易解释。
    # 注意：这里通过不代表一定能发，claim_publish 还会在事务里再核对一次。
    with db.read() as conn:
        return _validate_publish_conn(
            db, conn, config, clock, approval, publisher,
            task_id=task_id, attempt_no=attempt_no, allow_reopen=allow_reopen,
            task_generation=task_generation, package_hash=package_hash,
            lease_owner=lease_owner,
        )


@dataclass
class PublishClaim:
    """The exact artifact that won the transactional claim.

    The caller must build its publish request from these rows, never from an
    earlier validation snapshot.
    """

    attempt: sqlite3.Row
    approval: sqlite3.Row
    draft: sqlite3.Row
    images: list[dict[str, Any]]


def claim_publish(
    db: Database,
    config: Config,
    clock: Clock,
    approval_id: int,
    publisher: Publisher,
    *,
    task_id: int | None = None,
    attempt_no: int | None = None,
    allow_reopen: bool = False,
    retry_basis: str = "",
    task_generation: int | None = None,
    package_hash: str | None = None,
    lease_owner: str | None = None,
) -> tuple[PublishClaim | None, str]:
    """Transactionally validate and move approved -> publishing.

    Every database condition that applies to a publish (pause, approval/draft
    state, expiry/schedule, body/image hashes, sources, account, daily quota and
    prior attempts) is re-evaluated through the same ``BEGIN IMMEDIATE``
    connection before the attempt row is inserted.  This prevents two distinct
    approvals from both passing a stale "one publish today" check.

    This serialises the claim inside one SQLite database.  It is not a
    distributed exactly-once guarantee across machines or databases.
    """
    # 中文：这是“占坑”步骤。整个函数在一个写事务里完成：
    #   - 重新读审批和草稿的最新状态（不能拿旧快照做决定）；
    #   - 在同一个事务里重算全部发布条件（含每日配额、过期、正文/图片、
    #     来源、账号、已有尝试），而不是相信事务外的一次预检；
    #   - 插入 publish_attempts，dedupe_key = approval:<id>；
    #   - 把审批/草稿/事件都改成 publishing。
    # dedupe_key 上有 UNIQUE 约束，所以两个进程同时抢时，
    # 只有一个能插入成功，另一个会收到 IntegrityError，被下面捕获。
    # 这个保证只覆盖同一个 SQLite 数据库，不是跨机器/跨库的分布式
    # exactly-once；网络发布仍可能产生 unknown。
    if isinstance(approval_id, sqlite3.Row):  # backward-compatible call style
        approval_id = int(approval_id["id"])
    approval_id = int(approval_id)
    now = to_iso(clock.now())
    try:
        with db.tx() as conn:
            # 事务内重新读一遍。外面拿到的 approval 可能已经过期，
            # 比如另一个进程刚刚处理过，或者用户刚撤回。
            fresh = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if fresh is None:
                return None, "approval changed before claim"
            allowed_statuses = {"approved"}
            if task_id is not None and allow_reopen:
                allowed_statuses |= {"failed", "publish_unknown"}
            if fresh["status"] not in allowed_statuses:
                return None, "approval changed before claim"
            # 所有 DB 条件都在同一个写事务连接上重新检查。
            ok, reason, current_draft, images = _validate_publish_conn(
                db, conn, config, clock, fresh, publisher,
                task_id=task_id, attempt_no=attempt_no, allow_reopen=allow_reopen,
                task_generation=task_generation, package_hash=package_hash,
                lease_owner=lease_owner,
            )
            if not ok or current_draft is None:
                return None, reason or "draft changed before claim"
            # Re-check the binding inside the same write transaction.
            expected = approvals.approval_fingerprint(
                platform=fresh["platform"],
                account_id=fresh["account_id"],
                draft_id=fresh["draft_id"],
                draft_version=fresh["draft_version"],
                body_sha256=fresh["body_sha256"],
                images_sha256=fresh["images_sha256"],
                scheduled_at=fresh["scheduled_at"],
                expires_at=fresh["expires_at"],
            )
            if expected != fresh["fingerprint"]:
                return None, "approval fingerprint mismatch at claim"
            if task_id is None:
                dedupe_key = f"approval:{int(fresh['id'])}"
            else:
                if attempt_no is None:
                    attempt_no = int(conn.execute(
                        "SELECT COALESCE(MAX(attempt_no),0)+1 FROM publish_attempts "
                        "WHERE publication_task_id=?", (int(task_id),)
                    ).fetchone()[0])
                dedupe_key = f"publication:{int(task_id)}:{int(attempt_no)}"
            if task_id is None:
                # Compatibility: an un-migrated live DB (schema 10) has no
                # publication_task_id/attempt_no/dispatch_state/retry_basis
                # columns yet. The old path must keep working until the
                # operator explicitly applies migration 11.
                cur = conn.execute(
                    "INSERT INTO publish_attempts(approval_id, draft_id, dedupe_key, platform, account_id, "
                    "mode, state, created_at, started_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'publishing', ?, ?, ?)",
                    (
                        fresh["id"],
                        fresh["draft_id"],
                        dedupe_key,
                        fresh["platform"],
                        fresh["account_id"],
                        publisher.mode,
                        now,
                        now,
                        now,
                    ),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO publish_attempts(approval_id, draft_id, dedupe_key, platform, account_id, "
                    "mode, state, created_at, started_at, updated_at, publication_task_id, attempt_no, "
                    "dispatch_state, retry_basis) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'publishing', ?, ?, ?, ?, ?, 'not_dispatched', ?)",
                    (
                        fresh["id"],
                        fresh["draft_id"],
                        dedupe_key,
                        fresh["platform"],
                        fresh["account_id"],
                        publisher.mode,
                        now,
                        now,
                        now,
                        task_id,
                        attempt_no,
                        retry_basis,
                    ),
                )
            attempt_id = int(cur.lastrowid)
            moved = conn.execute(
                "UPDATE approvals SET status='publishing' WHERE id=? AND status IN ('approved','failed','publish_unknown')",
                (fresh["id"],),
            )
            if moved.rowcount != 1:
                raise sqlite3.IntegrityError("approval changed while claiming")
            moved = conn.execute(
                "UPDATE drafts SET status='publishing', updated_at=? WHERE id=? AND status IN ('approved','failed','publish_unknown')",
                (now, fresh["draft_id"]),
            )
            if moved.rowcount != 1:
                raise sqlite3.IntegrityError("draft changed while claiming")
            conn.execute(
                "UPDATE events SET status='publishing' WHERE id=?",
                (current_draft["event_id"],),
            )
            if task_id is not None:
                conn.execute(
                    "UPDATE publication_tasks SET active_attempt_id=?, state='submitting', updated_at=? "
                    "WHERE id=? AND active_attempt_id IS NULL",
                    (attempt_id, now, int(task_id)),
                )
            # 审计记录也写在同一事务里：要么“占坑 + 审计”一起成功，
            # 要么一起回滚，不会出现有坑位却没记录的情况。
            audit(
                conn,
                "publish_gate",
                "publish.claimed",
                entity="publish_attempt",
                entity_id=attempt_id,
                detail={
                    "approval_id": fresh["id"],
                    "approval_code": fresh["code"],
                    "draft_id": fresh["draft_id"],
                    "mode": publisher.mode,
                    "dedupe_key": dedupe_key,
                },
                at=now,
            )
            attempt = conn.execute(
                "SELECT * FROM publish_attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            return PublishClaim(
                attempt=attempt,
                approval=fresh,
                draft=current_draft,
                images=images,
            ), "ok"
    except sqlite3.IntegrityError as exc:
        # 唯一约束冲突 = 另一个进程已经抢到了。这不是错误，
        # 而是“本来就不该有第二次发布”的正常结果。
        return None, f"publish already claimed: {exc}"


def _reason_code(reason: str) -> str:
    code = re.sub(r"[^a-z0-9]+", "_", str(reason or "").lower()).strip("_")
    return (code or "blocked")[:80]


def _gate_detail(reason: str, reason_code: str | None = None) -> str:
    """Translate a machine gate reason into an owner-facing Chinese sentence.

    The original reason stays available in ``reason_code``/audit for debugging;
    the result row and Feishu notice must not show raw English internals.
    """
    raw = str(reason or "").strip()
    lowered = raw.lower()
    if reason_code == "publisher_not_configured":
        return "发布适配器未配置：" + raw[:160]
    if reason_code == "approval_expired" or lowered == "approval expired":
        return "审批已过期；需要重新生成草稿并取得新的审批。"
    fixed = {
        "global pause is on": "全局暂停已开启，未提交发布。",
        "approval is not approved": "审批尚未批准，未提交发布。",
        "approval has invalid timestamps": "审批时间字段无效，未提交发布。",
        "not yet scheduled": "尚未到计划发布时间。",
        "approval changed before claim": "提交前审批状态发生变化，未提交发布。",
        "draft no longer exists": "草稿已不存在，未提交发布。",
        "draft has no image digest": "草稿缺少图片指纹，未提交发布。",
        "approved image file is missing": "审批时的图片文件缺失，未提交发布。",
        "approved image bytes changed": "审批后图片内容被改动，未提交发布。",
        "image digest does not match approval": "图片指纹与审批不一致，未提交发布。",
        "draft version differs from approval": "草稿版本与审批版本不一致，未提交发布。",
        "live publish refuses fixture/demo source evidence": "真实发布拒绝 fixture/demo 来源证据，未提交发布。",
        "live publish refuses a fake/demo generated draft": "真实发布拒绝假模型/演示稿，未提交发布。",
        "no enabled source evidence remains for this event": "该事件已无可用来源证据，未提交发布。",
        "event has no evidence": "该事件没有证据，未提交发布。",
        "daily publish limit reached": "今日发布额度已用尽，未提交发布。",
        "a publish attempt already exists for this approval": "该审批已有发布尝试，不会重复提交。",
        "approval fingerprint mismatch at claim": "审批指纹在提交时不一致，未提交发布。",
        "approval changed while claiming": "抢占发布时审批状态发生变化，未提交发布。",
        "draft changed while claiming": "抢占发布时草稿状态发生变化，未提交发布。",
    }
    if lowered in fixed:
        return fixed[lowered]
    if lowered.startswith("approval status is "):
        return f"审批状态为 {lowered.removeprefix('approval status is ')}，尚未批准。"
    if lowered.startswith("draft status is "):
        return f"草稿状态为 {lowered.removeprefix('draft status is ')}，不是已批准状态。"
    if lowered.startswith("account status is "):
        return f"账号状态为 {lowered.removeprefix('account status is ')}，尚未核验。"
    if lowered.startswith("event status is "):
        return f"事件状态为 {lowered.removeprefix('event status is ')}，已不能发布。"
    if lowered.startswith("publish already claimed"):
        return "该审批已被其他进程抢占发布，本次未重复提交。"
    return "发布前检查未通过：" + raw[:160]


def _record_gate_result(
    db: Database,
    config: Config,
    clock: Clock,
    approval: sqlite3.Row,
    *,
    state: str,
    reason: str,
    reason_code: str | None = None,
) -> dict[str, Any] | None:
    """Persist a pre-submit block/expiry as a business result.

    This intentionally does not create a publish_attempt: nothing was submitted,
    so counting it as an attempt would corrupt the quota and could look like a
    real post.  The result row is the durable answer to "what happened?".
    """
    try:
        return record_result(
            db,
            config,
            clock,
            approval=approval,
            state=state,
            stage="gate",
            reason_code=reason_code or _reason_code(reason),
            detail=_gate_detail(reason, reason_code),
        )
    except Exception:  # noqa: BLE001 - notification/result failure must not hide the gate result
        return None


def record_blocked_approval(
    db: Database,
    config: Config,
    clock: Clock,
    approval_id: int,
    *,
    reason: str,
    reason_code: str = "publisher_not_configured",
) -> dict[str, Any]:
    """Durably record a pre-submit block when no publisher was constructed.

    Used by the worker's ``publisher is None`` path.  It must not create a
    publish attempt (nothing was submitted) and must queue the same idempotent
    owner notice as every other gate block.
    """
    approval = approvals.get_approval(db, approval_id=approval_id)
    if approval is None:
        raise PublishGateError("approval not found")
    existing = _existing_attempt(db, int(approval["id"]))
    if existing is not None:
        business = latest_for_approval(db, int(approval["id"]))
        return {
            "ok": False,
            "state": "already_attempted",
            "business_state": business["state"] if business is not None else None,
            "result_id": int(business["id"]) if business is not None else None,
            "notification_id": business["notification_id"] if business is not None else None,
            "notification_queued": False,
            "detail": "this approval already has a publish attempt",
        }
    gate = _record_gate_result(
        db,
        config,
        clock,
        approval,
        state="blocked",
        reason=reason,
        reason_code=reason_code,
    )
    detail = _gate_detail(reason, reason_code)
    return {
        "ok": False,
        "state": "blocked",
        "business_state": gate["state"] if gate is not None else "blocked",
        "detail": detail,
        "result_id": gate.get("result_id") if gate is not None else None,
        "notification_id": gate.get("notification_id") if gate is not None else None,
        "notification_queued": bool(gate.get("notification_queued")) if gate is not None else False,
        "approval_code": approval["code"],
    }


def _finish(
    db: Database,
    config: Config,
    clock: Clock,
    attempt: sqlite3.Row,
    approval: sqlite3.Row,
    outcome: PublishOutcome,
) -> dict[str, Any]:
    # 把发布器的返回结果落库，并且只落“一个”最终状态。
    # 关键原则：不轻信适配器。适配器说 published 但没给回执，
    # 或者给了一个不认识的 state，一律降级成 unknown。
    # 因为在发布这件事上，“不确定”比“错误地认定成功”安全得多。
    now = to_iso(clock.now())
    state = outcome.state
    external_id = outcome.external_post_id
    external_url = outcome.external_url
    if state == "published" and not (external_id or external_url):
        state = "unknown"
        outcome.detail = "adapter claimed published without a post id/URL; treated as unknown"
    # 只接受四种状态。未知状态不猜测，直接当 unknown 处理。
    if state not in {"published", "simulated", "failed", "unknown"}:
        state = "unknown"
        outcome.detail = f"unrecognised adapter state {state!r}; treated as unknown"
    # 同一个发布结果，要同步到审批、草稿、事件三张表，
    # 用字典做一一映射，避免到处写 if/else 写漏。
    # 注意 unknown 对应的状态名是 publish_unknown，不是 unknown，
    # 让数据库里一眼能看出“这是需要人工处理的”。
    approval_status = {
        "published": "published",
        "simulated": "simulated",
        "failed": "failed",
        "unknown": "publish_unknown",
    }[state]
    draft_status = {
        "published": "published",
        "simulated": "simulated",
        "failed": "failed",
        "unknown": "publish_unknown",
    }[state]
    event_status = draft_status
    # 落库也是一个事务：attempt、approval、draft、event、audit
    # 要么一起更新，要么一起不动，不会出现“尝试成功但草稿还是 publishing”。
    with db.tx() as conn:
        # Compare-and-set: only the writer that still owns a *publishing*
        # attempt may record the publisher outcome.  If a human (or another
        # read-only reconciler) already resolved this attempt while the network
        # call was in flight, keep that terminal fact instead of overwriting it.
        cur = conn.execute(
            "UPDATE publish_attempts SET state=?, external_post_id=?, external_url=?, receipt_json=?, "
            "error=?, finished_at=?, updated_at=? WHERE id=? AND state='publishing'",
            (
                state,
                external_id,
                external_url,
                json_dumps(outcome.receipt or {}),
                None if state != "failed" else outcome.detail,
                now,
                now,
                attempt["id"],
            ),
        )
        if cur.rowcount != 1:
            current = conn.execute(
                "SELECT * FROM publish_attempts WHERE id=?", (attempt["id"],)
            ).fetchone()
            if current is None:
                raise PublishGateError("publish attempt disappeared while finishing")
            business = conn.execute(
                "SELECT * FROM publish_results WHERE approval_id=? ORDER BY id DESC LIMIT 1",
                (approval["id"],),
            ).fetchone()
            current_state = str(current["state"])
            return {
                "ok": current_state in {"published", "simulated"},
                "state": current_state,
                "detail": "发布尝试已被其他写入方确认；保留现有终态，未用旧回调覆盖。",
                "external_post_id": current["external_post_id"],
                "external_url": current["external_url"],
                "simulated": current_state == "simulated",
                "attempt_id": int(current["id"]),
                "approval_code": approval["code"],
                "result_id": int(business["id"]) if business is not None else None,
                "notification_id": (
                    int(business["notification_id"])
                    if business is not None and business["notification_id"] is not None
                    else None
                ),
                "notification_queued": False,
                "transition_blocked": True,
            }
        conn.execute("UPDATE approvals SET status=? WHERE id=?", (approval_status, approval["id"]))
        conn.execute("UPDATE drafts SET status=?, updated_at=? WHERE id=?", (draft_status, now, approval["draft_id"]))
        conn.execute(
            "UPDATE events SET status=? WHERE id=(SELECT event_id FROM drafts WHERE id=?)",
            (event_status, approval["draft_id"]),
        )
        # Older (schema 10) attempts do not have publication_task_id at all.
        # Reading it by name would raise KeyError on an old row.
        task_id = attempt["publication_task_id"] if "publication_task_id" in attempt.keys() else None
        if task_id is not None:
            task_state = (
                "published" if state == "published" else
                "simulated" if state == "simulated" else
                "waiting_owner" if state == "unknown" else
                "retry_wait"
            )
            conn.execute(
                "UPDATE publication_tasks SET active_attempt_id=NULL, state=?, updated_at=? "
                "WHERE id=? AND active_attempt_id=?",
                (task_state, now, task_id, attempt["id"]),
            )
        if "failure_class" in attempt.keys():
            conn.execute(
                "UPDATE publish_attempts SET failure_class=?, retryable=? WHERE id=?",
                (outcome.failure_class or None, 1 if outcome.retryable else 0, attempt["id"]),
            )
        audit(
            conn,
            "publish_gate",
            f"publish.{state}",
            entity="publish_attempt",
            entity_id=attempt["id"],
            detail={
                "approval_code": approval["code"],
                "external_post_id": external_id,
                "external_url": external_url,
                "detail": outcome.detail,
                "simulated": state == "simulated",
            },
            at=now,
        )
        result = record_result(
            db,
            config,
            clock,
            approval=approval,
            state=state,
            stage="publisher",
            attempt=attempt,
            detail=str(outcome.detail or ""),
            external_post_id=external_id,
            external_url=external_url,
            receipt=outcome.receipt,
            conn=conn,
        )
    return {
        "ok": state in {"published", "simulated"},
        "state": state,
        "detail": outcome.detail,
        "external_post_id": external_id,
        "external_url": external_url,
        "simulated": state == "simulated",
        "failure_class": outcome.failure_class,
        "retryable": bool(outcome.retryable),
        "attempt_id": int(attempt["id"]),
        "approval_code": approval["code"],
        "result_id": result.get("result_id"),
        "notification_id": result.get("notification_id"),
        "notification_queued": result.get("notification_queued", False),
    }


def publish_approval(
    db: Database,
    config: Config,
    clock: Clock,
    approval_id: int,
    *,
    publisher: Publisher | None = None,
    task_id: int | None = None,
    attempt_no: int | None = None,
    allow_reopen: bool = False,
    retry_basis: str = "",
    task_generation: int | None = None,
    package_hash: str | None = None,
    lease_owner: str | None = None,
    before_send: Any | None = None,
) -> dict[str, Any]:
    # 一次完整发布的入口：检查 -> 占坑 -> 调用发布器 -> 记账。
    # publisher 参数允许测试注入假发布器；正常运行时传 None，
    # 由 build_publisher 根据配置创建。
    approval = approvals.get_approval(db, approval_id=approval_id)
    if approval is None:
        raise PublishGateError("approval not found")
    # 已经尝试过就直接返回，不做任何网络调用。
    # 这是“绝不重发”最外层、也最容易看懂的一道检查。
    if task_id is None and _existing_attempt(db, int(approval["id"])) is not None:
        result = {
            "ok": False,
            "state": "already_attempted",
            "detail": "this approval already has a publish attempt",
            "notification_queued": False,
        }
        business = latest_for_approval(db, int(approval["id"]))
        if business is not None:
            # Return the durable business result too, so callers do not have to
            # ask “did the earlier attempt actually publish?” a second time.
            result.update(
                {
                    "business_state": business["state"],
                    "result_id": int(business["id"]),
                    "notification_id": business["notification_id"],
                }
            )
        return result

    def _attach(payload: dict[str, Any], gate: dict[str, Any] | None) -> dict[str, Any]:
        if gate is not None:
            payload.update(
                {
                    "business_state": gate["state"],
                    "result_id": gate.get("result_id"),
                    "notification_id": gate.get("notification_id"),
                    "detail": gate.get("detail") or payload.get("detail", ""),
                }
            )
        return payload

    # 创建发布器本身就可能失败（比如 live 开关没全开、token 没配）。
    # 这种情况按 blocked 返回，不抛异常，因为它是可预期的配置问题。
    try:
        publisher = publisher or build_publisher(config, clock)
    except NotConfigured as exc:
        gate = _record_gate_result(
            db,
            config,
            clock,
            approval,
            state="blocked",
            reason=str(exc),
            reason_code="publisher_not_configured",
        )
        return _attach({"ok": False, "state": "blocked", "detail": str(exc)}, gate)
    ok, reason, draft, images = validate_publish(
        db, config, clock, approval, publisher,
        task_id=task_id, attempt_no=attempt_no, allow_reopen=allow_reopen,
        task_generation=task_generation, package_hash=package_hash,
        lease_owner=lease_owner,
    )
    if not ok or draft is None:
        expired = reason == "approval expired"
        if expired:
            approvals.expire_approvals(db, clock)
        gate = _record_gate_result(
            db,
            config,
            clock,
            approval,
            state="expired" if expired else "blocked",
            reason=reason,
            reason_code="approval_expired" if expired else None,
        )
        return _attach(
            {"ok": False, "state": "expired" if expired else "blocked", "detail": reason},
            gate,
        )
    claim, claim_reason = claim_publish(
        db, config, clock, int(approval["id"]), publisher,
        task_id=task_id, attempt_no=attempt_no, allow_reopen=allow_reopen,
        retry_basis=retry_basis, task_generation=task_generation,
        package_hash=package_hash, lease_owner=lease_owner,
    )
    if claim is None:
        expired = claim_reason == "approval expired"
        if expired:
            approvals.expire_approvals(db, clock)
        gate = _record_gate_result(
            db,
            config,
            clock,
            approval,
            state="expired" if expired else "blocked",
            reason=claim_reason,
            reason_code="approval_expired" if expired else None,
        )
        return _attach(
            {"ok": False, "state": "not_claimed" if not expired else "expired", "detail": claim_reason},
            gate,
        )
    attempt = claim.attempt
    # 组装发布请求时，所有字段都取自事务内重新验证过的那一版，
    # 而不是事务外的旧快照；这样“预检通过后内容被改”不会把旧内容发出去。
    fresh_approval = claim.approval
    fresh_draft = claim.draft
    request = PublishRequest(
        draft_id=int(fresh_draft["id"]),
        draft_version=int(fresh_draft["version"]),
        title=str(fresh_draft["title"]),
        body=str(fresh_draft["body"]),
        body_sha256=str(fresh_approval["body_sha256"]),
        images=claim.images,
        platform=str(fresh_approval["platform"]),
        account_id=str(fresh_approval["account_id"]),
        approval_code=str(fresh_approval["code"]),
        scheduled_at=str(fresh_approval["scheduled_at"]),
        tags=list((json_loads(fresh_draft["body_json"], {}) or {}).get("tags") or []),
    )
    # 记录发送前的账号/平台基线。回调失败不能阻止发送（否则会制造
    # 一个“没发却占着 attempt”的状态），但会留下 read_ok=false 的证据，
    # 后续重启核对只能保持 unknown。
    if before_send is not None:
        try:
            before_send(claim)
        except Exception:
            pass
    # 到这里坑位已经占好，无论发布器怎么炸，都必须给出一个确定的结果，
    # 绝不能让记录永远停在 publishing。
    # 异常映射规则：
    #   PublishFailed  -> failed（确定没发出去）
    #   PublishUnknown -> unknown（可能发出去了）
    #   NotConfigured  -> failed（配置问题，请求根本没发）
    #   其他任何异常  -> unknown（最保守：无法判断就不假装失败）
    try:
        if task_id is not None:
            with db.tx() as conn:
                conn.execute(
                    "UPDATE publish_attempts SET dispatch_state='dispatched', updated_at=? "
                    "WHERE id=? AND dispatch_state='not_dispatched'",
                    (to_iso(clock.now()), attempt["id"]),
                )
        outcome = publisher.publish(request)
    except PublishFailed as exc:
        failure_class = str(exc.code or "rejected")
        # Only an explicit pre-send/connect failure may be retried.  Login,
        # asset and platform-rejection failures require owner/repair action.
        retryable = bool(exc.retryable and failure_class in {
            "not_dispatched", "connection_before_send", "bridge_unavailable"
        })
        outcome = PublishOutcome(
            state="failed", detail=f"publisher failed: {exc}",
            failure_class=failure_class, retryable=retryable,
        )
    except PublishUnknown as exc:
        outcome = PublishOutcome(
            state="unknown", detail=f"publish outcome unknown: {exc}",
            failure_class="unknown_result", retryable=False,
        )
    except NotConfigured as exc:
        outcome = PublishOutcome(
            state="failed", detail=f"publisher not configured: {exc}",
            failure_class="config", retryable=False,
        )
    except Exception as exc:  # noqa: BLE001 - never assume a network exception means success
        outcome = PublishOutcome(
            state="unknown", detail=f"publisher raised {type(exc).__name__}: {exc}; not retried",
            failure_class="unknown_result", retryable=False,
        )
    return _finish(db, config, clock, attempt, fresh_approval, outcome)


def publish_due(
    db: Database,
    config: Config,
    clock: Clock,
    *,
    publisher: Publisher | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    # 批量处理“到点且未过期”的审批，每次最多 limit 条。
    # 单条失败不能影响其他条：循环里捕获异常，把错误作为结果返回。
    now = to_iso(clock.now())
    rows = db.fetchall(
        "SELECT * FROM approvals WHERE status='approved' AND scheduled_at <= ? AND expires_at > ? "
        "ORDER BY scheduled_at LIMIT ?",
        (now, now, int(limit)),
    )
    results = []
    for approval in rows:
        # 逐条独立处理。某一条抛异常（比如数据库锁冲突），
        # 只记录这条失败，继续处理后面的。
        try:
            results.append(publish_approval(db, config, clock, int(approval["id"]), publisher=publisher))
        except Exception as exc:  # noqa: BLE001 - one failure must not stop others
            results.append(
                {"ok": False, "state": "error", "detail": f"{type(exc).__name__}: {exc}", "approval_code": approval["code"]}
            )
    return results


def recover_interrupted_publishes(db: Database, clock: Clock, config: Config | None = None) -> int:
    """Any attempt left in ``publishing`` may have reached the platform.

    We cannot know, so it becomes ``publish_unknown`` and is never resent.  This
    also covers a process restart while ``approvals.status='publishing'``.
    """
    # 中文：进程崩溃恢复。如果数据库里还留着 publishing，说明上次
    # 调用发布器之后、写结果之前程序死了。请求可能已经到了平台，
    # 也可能没有——无法判断，所以统一改成 unknown，绝不重发。
    # 这就是“宁可人工核对，也不重复发帖”的代价与选择。
    now = to_iso(clock.now())
    count = 0
    with db.tx() as conn:
        attempts = conn.execute("SELECT * FROM publish_attempts WHERE state='publishing'").fetchall()
        for attempt in attempts:
            conn.execute(
                "UPDATE publish_attempts SET state='unknown', reconcile_note=?, finished_at=?, updated_at=? "
                "WHERE id=? AND state='publishing'",
                (
                    "process restarted while publishing; the request may have reached the platform",
                    now,
                    now,
                    attempt["id"],
                ),
            )
            conn.execute(
                "UPDATE approvals SET status='publish_unknown' WHERE id=? AND status='publishing'",
                (attempt["approval_id"],),
            )
            conn.execute(
                "UPDATE drafts SET status='publish_unknown', updated_at=? WHERE id=?",
                (now, attempt["draft_id"]),
            )
            conn.execute(
                "UPDATE events SET status='publish_unknown' "
                "WHERE id=(SELECT event_id FROM drafts WHERE id=?)",
                (attempt["draft_id"],),
            )
            audit(
                conn,
                "publish_gate",
                "publish.recovered_unknown",
                entity="publish_attempt",
                entity_id=attempt["id"],
                detail={"approval_id": attempt["approval_id"]},
                at=now,
            )
            if config is not None:
                approval = conn.execute(
                    "SELECT * FROM approvals WHERE id=?", (attempt["approval_id"],)
                ).fetchone()
                if approval is not None:
                    record_result(
                        db,
                        config,
                        clock,
                        approval=approval,
                        state="unknown",
                        stage="recovery",
                        attempt=attempt,
                        reason_code="restart_while_publishing",
                        detail=(
                            "进程在发布请求之后、写结果之前重启；"
                            "请求可能已到平台，已按 unknown 处理且绝不自动重发。"
                        ),
                        conn=conn,
                    )
            count += 1
        # Defensive: an approval could be marked publishing without an attempt.
        # 中文：防御性兜底。理论上审批变成 publishing 时一定同时插入尝试，
        # 但万一历史数据或手工操作造成“只有审批没有尝试”，
        # 也不能让它永远卡住，同样标成 unknown。
        rows = conn.execute(
            "SELECT * FROM approvals WHERE status='publishing' "
            "AND id NOT IN (SELECT approval_id FROM publish_attempts)"
        ).fetchall()
        for row in rows:
            conn.execute("UPDATE approvals SET status='publish_unknown' WHERE id=?", (row["id"],))
            conn.execute("UPDATE drafts SET status='publish_unknown', updated_at=? WHERE id=?", (now, row["draft_id"]))
            if config is not None:
                record_result(
                    db,
                    config,
                    clock,
                    approval=row,
                    state="unknown",
                    stage="recovery",
                    reason_code="publishing_without_attempt",
                    detail="发现审批停在 publishing 但没有发布尝试；按 unknown 处理且绝不自动重发。",
                    conn=conn,
                )
            count += 1
    return count


def reconcile_unknown(
    db: Database,
    config: Config,
    clock: Clock,
    *,
    approval_code: str,
    resolution: str,
    note: str,
    external_post_id: str | None = None,
    external_url: str | None = None,
    actor: str = "operator",
) -> dict[str, Any]:
    """Manually resolve a publish_unknown.  Never triggers a resend."""
    # 中文：人工对账。程序已经无法判断这篇到底发没发，只能由人去平台看，
    # 然后告诉程序三种结论之一：
    #   confirmed_published      确实发了（必须提供真实 post id 或 URL）
    #   confirmed_not_published  确实没发（之后要重新走草稿+审批才能再发）
    #   still_unknown            还没查清（只记备注，状态保持 unknown）
    # 注意：这个函数永远不调用发布器，永远不会重发。
    approval = approvals.get_approval(db, code=approval_code)
    if approval is None:
        raise PublishGateError("approval not found")
    attempt = db.fetchone(
        "SELECT * FROM publish_attempts WHERE approval_id=? ORDER BY id DESC LIMIT 1", (approval["id"],)
    )
    if attempt is None:
        raise PublishGateError("no publish attempt to reconcile")
    if attempt["state"] not in {"unknown", "publishing"}:
        raise PublishGateError(f"attempt state is {attempt['state']}, not unknown")
    now = to_iso(clock.now())

    def _cas_attempt(conn: sqlite3.Connection, sql: str, params: tuple) -> None:
        cur = conn.execute(sql, params)
        if cur.rowcount != 1:
            raise PublishGateError(
                "attempt state changed before reconciliation; the current terminal result was kept"
            )

    # 结论一：确认已发布。必须有回执，否则不接受——
    # 否则“人工确认”就变成了绕过审核的发布后门。
    if resolution == "confirmed_published":
        if not (external_post_id or external_url):
            raise PublishGateError("confirmed_published requires a real post id or URL")
        with db.tx() as conn:
            _cas_attempt(
                conn,
                "UPDATE publish_attempts SET state='published', external_post_id=?, external_url=?, "
                "reconcile_note=?, finished_at=?, updated_at=? "
                "WHERE id=? AND state IN ('unknown','publishing')",
                (external_post_id, external_url, note, now, now, attempt["id"]),
            )
            conn.execute("UPDATE approvals SET status='published', decision_comment=? WHERE id=?", (note, approval["id"]))
            conn.execute(
                "UPDATE drafts SET status='published', updated_at=? WHERE id=?", (now, approval["draft_id"])
            )
            conn.execute(
                "UPDATE events SET status='published' WHERE id=(SELECT event_id FROM drafts WHERE id=?)",
                (approval["draft_id"],),
            )
            audit(
                conn,
                actor,
                "publish.reconciled_published",
                entity="publish_attempt",
                entity_id=attempt["id"],
                detail={"note": note, "external_url": external_url},
                at=now,
            )
            result = record_result(
                db,
                config,
                clock,
                approval=approval,
                state="published",
                stage="reconcile",
                attempt=attempt,
                reason_code="manual_confirmed_published",
                detail=note,
                external_post_id=external_post_id,
                external_url=external_url,
                receipt={"reconciled": True, "note": note},
                conn=conn,
            )
        return {
            "ok": True,
            "state": "published",
            "detail": "reconciled with a real receipt",
            "result_id": result.get("result_id"),
            "notification_id": result.get("notification_id"),
        }
    # 结论二：确认没发。把草稿标成 approval_invalidated：
    # 旧审批不能再用了，想重发必须重新生成内容、重新审批。
    if resolution == "confirmed_not_published":
        with db.tx() as conn:
            _cas_attempt(
                conn,
                "UPDATE publish_attempts SET state='failed', error=?, reconcile_note=?, finished_at=?, updated_at=? "
                "WHERE id=? AND state IN ('unknown','publishing')",
                ("confirmed not published during manual reconciliation", note, now, now, attempt["id"]),
            )
            conn.execute("UPDATE approvals SET status='reconciled_not_published' WHERE id=?", (approval["id"],))
            conn.execute(
                "UPDATE drafts SET status='approval_invalidated', updated_at=? WHERE id=?",
                (now, approval["draft_id"]),
            )
            conn.execute(
                "UPDATE events SET status='reconciled_not_published' "
                "WHERE id=(SELECT event_id FROM drafts WHERE id=?)",
                (approval["draft_id"],),
            )
            audit(
                conn,
                actor,
                "publish.reconciled_not_published",
                entity="publish_attempt",
                entity_id=attempt["id"],
                detail={"note": note},
                at=now,
            )
            result = record_result(
                db,
                config,
                clock,
                approval=approval,
                state="failed",
                stage="reconcile",
                attempt=attempt,
                reason_code="reconciled_not_published",
                detail=f"人工确认未发布：{note}",
                receipt={"reconciled": True, "published": False, "note": note},
                conn=conn,
            )
        return {
            "ok": True,
            "state": "not_published",
            "detail": "not published; a new approval/draft version is required before any retry",
            "result_id": result.get("result_id"),
            "notification_id": result.get("notification_id"),
        }
    # 结论三：还没查清。只更新备注，其他状态一律不动，
    # 继续保持“不可自动重试”。
    if resolution == "still_unknown":
        with db.tx() as conn:
            _cas_attempt(
                conn,
                "UPDATE publish_attempts SET reconcile_note=?, updated_at=? "
                "WHERE id=? AND state IN ('unknown','publishing')",
                (note, now, attempt["id"]),
            )
            audit(
                conn,
                actor,
                "publish.reconcile_pending",
                entity="publish_attempt",
                entity_id=attempt["id"],
                detail={"note": note},
                at=now,
            )
            result = record_result(
                db,
                config,
                clock,
                approval=approval,
                state="unknown",
                stage="reconcile",
                attempt=attempt,
                reason_code="manual_still_unknown",
                detail=note,
                conn=conn,
            )
        return {
            "ok": True,
            "state": "unknown",
            "detail": "still unknown; no resend performed",
            "result_id": result.get("result_id"),
            "notification_id": result.get("notification_id"),
        }
    raise PublishGateError("resolution must be confirmed_published, confirmed_not_published, or still_unknown")


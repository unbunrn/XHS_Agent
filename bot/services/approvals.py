"""Approval lifecycle.

An approval is a *binding* over the exact final artifact:

    platform + account + draft id/version + body sha256 + image content sha256
    + scheduled publish time + expiry

Any change to text, images, account, platform or schedule makes the approval
invalid.  The callback path is idempotent by message id; approving twice never
creates a second publish attempt.
"""

# =============================================================================
# 中文导读：审批单是整个系统的“合同”
# =============================================================================
# 用户批准的不是“一篇稿子”这个概念，而是一份精确绑定的快照：
#
#   平台 + 账号 + 草稿 ID/版本 + 正文哈希 + 图片哈希
#   + 计划发布时间 + 审批过期时间
#
# 上面任何一项变化，审批就作废。这就是为什么“修订”必须重新审批，
# 也是为什么审批后重新渲染图片会让审批失效——这是设计，不是 bug。
#
# 三条安全规则：
#   1. 只接受 owner 本人、私聊（p2p）里的命令；
#   2. 同一条消息 ID 只处理一次（网络回调经常重复推送）；
#   3. 批准时用 UPDATE ... WHERE status='pending' 做“比较并交换”，
#      两个并发回调只有一个能成功，另一个得到 race_lost。
#
# 生活化类比：
#   这是一张“验货单”。单子上写明了货号、重量、照片指纹。
#   交货时必须逐项核对；任何一项对不上，仓库就不放行。
# =============================================================================

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from ..audit import audit
from ..config import Config
from ..db import Database, json_dumps, json_loads
from ..ids import approval_code
from ..security import images_digest, sha256_file, sha256_text
from ..timeutil import Clock, parse_iso, to_iso
from . import notifications

APPROVE_WORDS = {"批准", "通过", "同意", "approve", "ok"}
# 三个命令词集合，中英文都支持。比较时会统一转小写。
REJECT_WORDS = {"拒绝", "驳回", "reject", "no"}
REVISE_WORDS = {"修改", "改一下", "revise", "edit"}

# Longest-first so "改一下" is preferred over a shorter accidental prefix.
_ACTION_WORDS = sorted(
    [(word, "approve") for word in APPROVE_WORDS]
    + [(word, "reject") for word in REJECT_WORDS]
    + [(word, "revise") for word in REVISE_WORDS],
    key=lambda item: len(item[0]),
    reverse=True,
)


class ApprovalError(RuntimeError):
    # 审批创建/校验失败的统称。权限或完整性问题都不应该被自动重试。
    pass


def configured_owner(config: Config) -> str:
    # owner 的 open_id 优先从 secrets 读，其次从普通配置读。
    # 如果为空，handle_inbound_message 会拒绝所有命令。
    return (
        config.secrets.lark_owner_open_id
        or str(config.get("notify.lark.owner_open_id", "") or "")
    ).strip()


def approval_fingerprint(
    *,
    platform: str,
    account_id: str,
    draft_id: int,
    draft_version: int,
    body_sha256: str,
    images_sha256: str,
    scheduled_at: str,
    expires_at: str,
) -> str:
    # 指纹 = 把八个绑定字段按固定顺序拼起来再算 SHA-256。
    # 顺序和分隔符必须稳定；任何字段变化都会产生完全不同的指纹。
    payload = "\n".join(
        [
            platform,
            account_id,
            str(int(draft_id)),
            str(int(draft_version)),
            body_sha256,
            images_sha256,
            scheduled_at,
            expires_at,
        ]
    )
    return sha256_text(payload)


def make_approval(
    db: Database,
    config: Config,
    clock: Clock,
    *,
    draft: sqlite3.Row | dict[str, Any],
    images: list[dict[str, Any]],
    scheduled_at: str,
    expires_at: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> sqlite3.Row:
    """Create a pending approval bound to the current draft bytes."""
    # 注意：正文哈希和图片哈希都是“现在重新算”的，不是相信数据库里的旧值。
    # 这样审批单一定对应当前真实内容。
    draft_id = int(draft["id"])
    version = int(draft["version"])
    platform = str(draft["platform"])
    account_id = str(draft["account_id"])
    body_sha = sha256_text(str(draft["body"]))
    images_sha = images_digest(images)
    now = clock.now()
    expires = expires_at or to_iso(now + timedelta(hours=float(config.get("limits.approval_ttl_hours", 24))))
    if parse_iso(expires) is None:
        raise ApprovalError("invalid approval expiry")
    if parse_iso(scheduled_at) is None:
        raise ApprovalError("invalid scheduled_at")
    fingerprint = approval_fingerprint(
        platform=platform,
        account_id=account_id,
        draft_id=draft_id,
        draft_version=version,
        body_sha256=body_sha,
        images_sha256=images_sha,
        scheduled_at=scheduled_at,
        expires_at=expires,
    )

    def _insert(conn: sqlite3.Connection) -> sqlite3.Row:
        # 待审批数量上限。注意这个检查和插入在同一个事务里，
        # 否则并发创建可能同时通过检查，超过上限。
        if conn.execute(
            "SELECT COUNT(*) FROM approvals WHERE status='pending'"
        ).fetchone()[0] >= int(config.get("limits.max_pending_approvals", 3)):
            raise ApprovalError("pending approval limit reached")
        code = None
        # 审批码随机生成，可能撞车；最多重试 12 次。
        # 12 次都撞上说明随机源有问题，宁可报错也不无限循环。
        for _ in range(12):
            candidate = approval_code()
            if conn.execute("SELECT 1 FROM approvals WHERE code=?", (candidate,)).fetchone() is None:
                code = candidate
                break
        if code is None:
            raise ApprovalError("could not allocate a unique approval code")
        try:
            cur = conn.execute(
                "INSERT INTO approvals(code, platform, account_id, draft_id, draft_version, "
                "body_sha256, images_sha256, fingerprint, scheduled_at, expires_at, status, "
                "channel, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    code,
                    platform,
                    account_id,
                    draft_id,
                    version,
                    body_sha,
                    images_sha,
                    fingerprint,
                    scheduled_at,
                    expires,
                    config.get("notify.provider", "fake"),
                    to_iso(now),
                ),
            )
        except sqlite3.IntegrityError as exc:
            # 两种唯一约束可能触发：审批码重复，或同一草稿已有 pending 审批
            # （ux_approvals_one_pending）。后者是防重复审批的关键闸门。
            raise ApprovalError(f"an approval for this draft already exists: {exc}") from exc
        return conn.execute("SELECT * FROM approvals WHERE id=?", (cur.lastrowid,)).fetchone()

    if conn is not None:
        # 调用方已经有事务时，直接加入它的事务，保证“建草稿 + 建审批”
        # 要么一起成功，要么一起回滚。
        return _insert(conn)
    with db.tx() as own:
        return _insert(own)


def get_approval(db: Database, *, code: str | None = None, approval_id: int | None = None) -> sqlite3.Row | None:
    # 支持按审批码或按数据库 ID 查询。审批码统一转大写去空格，
    # 用户手输大小写不一致也能找到。
    if code:
        return db.fetchone("SELECT * FROM approvals WHERE code=?", (code.strip().upper(),))
    if approval_id is not None:
        return db.fetchone("SELECT * FROM approvals WHERE id=?", (int(approval_id),))
    return None


def list_pending(db: Database, limit: int = 20) -> list[sqlite3.Row]:
    # 同时带出草稿当前的正文/图片哈希，控制台可以对比“审批绑定的”
    # 和“数据库现在的”是否一致，提前发现内容漂移。
    return db.fetchall(
        "SELECT a.*, d.title, d.body_sha256 AS current_body_sha256, d.images_sha256 AS current_images_sha256 "
        "FROM approvals a JOIN drafts d ON d.id=a.draft_id "
        "WHERE a.status='pending' ORDER BY a.created_at LIMIT ?",
        (int(limit),),
    )


def _refresh_artifact_hashes(
    db: Database, draft_id: int, *, conn: sqlite3.Connection | None = None
) -> tuple[str, str, list[dict[str, Any]], list[str]]:
    if conn is not None:
        draft = conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        rows = conn.execute(
            "SELECT ordinal, path, sha256 FROM draft_images WHERE draft_id=? ORDER BY ordinal",
            (draft_id,),
        ).fetchall()
    else:
        draft = db.fetchone("SELECT * FROM drafts WHERE id=?", (draft_id,))
        rows = db.fetchall(
            "SELECT ordinal, path, sha256 FROM draft_images WHERE draft_id=? ORDER BY ordinal",
            (draft_id,),
        )
    if draft is None:
        return "", "", [], ["draft missing"]
    problems: list[str] = []
    # 重新计算正文哈希（不是读数据库里的旧值）。
    body_sha = sha256_text(str(draft["body"]))
    images: list[dict[str, Any]] = []
    for row in rows:
        try:
            # 关键：直接读磁盘文件重新哈希，而不是相信 draft_images.sha256。
            # 这样即使有人手动替换了 PNG，也能被发现。
            actual = sha256_file(row["path"])
        except OSError:
            problems.append(f"image missing: {row['path']}")
            continue
        if actual != row["sha256"]:
            problems.append(f"image changed: {row['ordinal']}")
        images.append({"ordinal": int(row["ordinal"]), "sha256": actual, "path": row["path"]})
    if not images:
        problems.append("no images")
    return body_sha, images_digest(images), images, problems


def verify_approval_artifact(
    db: Database,
    approval: sqlite3.Row | dict[str, Any],
    *,
    conn: sqlite3.Connection | None = None,
) -> tuple[bool, str, list[dict[str, Any]]]:
    """Check that DB bytes/files still match what the approval bound."""
    # 逐项核对，任何一项不符都返回 (False, 原因, 图片列表)。
    # 检查顺序从便宜到昂贵：先比数据库字段，最后才重新读图片算哈希。
    # conn 供发布事务复用同一个写事务连接；只读调用可省略。
    if conn is not None:
        draft = conn.execute(
            "SELECT * FROM drafts WHERE id=?", (approval["draft_id"],)
        ).fetchone()
    else:
        draft = db.fetchone("SELECT * FROM drafts WHERE id=?", (approval["draft_id"],))
    if draft is None:
        return False, "draft no longer exists", []
    if int(draft["version"]) != int(approval["draft_version"]):
        return False, "draft version changed after approval", []
    if str(draft["platform"]) != str(approval["platform"]) or str(draft["account_id"]) != str(
        approval["account_id"]
    ):
        return False, "platform/account changed after approval", []
    if str(draft["body_sha256"]) != str(approval["body_sha256"]):
        return False, "stored body hash changed after approval", []
    if str(draft["scheduled_at"]) != str(approval["scheduled_at"]):
        return False, "scheduled publish time changed after approval", []
    if str(draft["expires_at"]) != str(approval["expires_at"]):
        return False, "approval expiry changed after approval", []
    body_sha, images_sha, images, problems = _refresh_artifact_hashes(
        db, int(draft["id"]), conn=conn
    )
    if problems:
        return False, "; ".join(problems), images
    if body_sha != approval["body_sha256"]:
        return False, "body content changed after approval", images
    if images_sha != approval["images_sha256"]:
        return False, "image content changed after approval", images
    expected = approval_fingerprint(
        # 用审批单自己保存的字段重新算一次指纹，再和存储的指纹比较。
        # 这能发现数据库行被手工篡改、但字段之间不再自洽的情况。
        platform=approval["platform"],
        account_id=approval["account_id"],
        draft_id=approval["draft_id"],
        draft_version=approval["draft_version"],
        body_sha256=approval["body_sha256"],
        images_sha256=approval["images_sha256"],
        scheduled_at=approval["scheduled_at"],
        expires_at=approval["expires_at"],
    )
    if expected != approval["fingerprint"]:
        return False, "approval fingerprint mismatch", images
    return True, "ok", images


def expire_approvals(db: Database, clock: Clock) -> int:
    # 把过期的 pending/approved 审批标为 expired，并同步草稿和事件状态。
    # 为什么 approved 也过期？因为计划发布时间已经过了，
    # 再拿旧批准去发就违背了“用户批准的是那个时间点”。
    now = to_iso(clock.now())
    expired = 0
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT * FROM approvals WHERE status IN ('pending','approved') AND expires_at <= ?",
            (now,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE approvals SET status='expired', decided_at=?, decision_comment='expired without decision' "
                "WHERE id=? AND status IN ('pending','approved')",
                (now, row["id"]),
            )
            conn.execute(
                "UPDATE drafts SET status='expired', updated_at=? WHERE id=? AND status IN "
                "('drafted','review_pending','approved')",
                (now, row["draft_id"]),
            )
            conn.execute(
                "UPDATE events SET status='expired' WHERE id=("
                "SELECT event_id FROM drafts WHERE id=?) AND status NOT IN ('published','simulated')",
                (row["draft_id"],),
            )
            audit(
                conn,
                "system",
                "approval.expired",
                entity="approval",
                entity_id=row["id"],
                detail={"code": row["code"], "draft_id": row["draft_id"]},
                at=now,
            )
            expired += 1
    return expired


def _parse_command(text: str) -> tuple[str | None, str, str]:
    # 解析聊天命令，格式：<动作> <审批码> [修改意见]
    # 例如：“批准 XH-AB12CD”、“修改 XH-AB12CD 标题再短一点”。
    # 先去掉开头可能出现的 @机器人 提及，但不解析消息里的其他内容。
    cleaned = (text or "").strip()
    # Strip leading @mentions common in chat, without interpreting content.
    cleaned = re.sub(r"^(@\S+\s+)+", "", cleaned).strip()
    parts = cleaned.split(maxsplit=2)
    word = parts[0].lower()
    action = next((act for w, act in _ACTION_WORDS if w.lower() == word), None)
    if (
        len(parts) >= 2
        and action is not None
        and re.fullmatch(r"(?i)XH-[A-Z0-9]{4,12}", parts[1].strip())
    ):
        code = parts[1].strip().upper()
        comment = parts[2].strip() if len(parts) > 2 else ""
        return action, code, comment
    # Accept the common no-space form “批准XH-AB12CD” without weakening
    # approval binding: only known action words immediately followed by a
    # single XH code are accepted.  Repeated action words are tolerated
    # (“批准批准XH-...”): a duplicated prefix is a typo, not a topic.
    rest = cleaned
    repeated_action = None
    while True:
        matched = next(
            ((word, act) for word, act in _ACTION_WORDS if rest.lower().startswith(word.lower())),
            None,
        )
        if matched is None:
            break
        word, act = matched
        if repeated_action is None:
            repeated_action = act
        rest = rest[len(word):].lstrip()
    if repeated_action is not None:
        match = re.match(r"(?i)^(XH-[A-Z0-9]{4,12})(?:\s+(.*))?$", rest)
        if match:
            return repeated_action, match.group(1).upper(), (match.group(2) or "").strip()
    if len(parts) >= 2:
        return None, parts[1].strip().upper(), parts[2].strip() if len(parts) > 2 else ""
    return None, "", ""


COMMAND_HELP = 'AI 运营助手 · 使用帮助\n【写稿】\n选题 <主题或链接>：可同时说明受众、角度和风格。\n修改 <具体意见>：修改最近一篇可修改作品；多篇同时处理时请先核对对象。\n\n【审批】\n批准 XH-XXXXXX：同意真实发布对应版本；先看完整正文和图片。\n拒绝 XH-XXXXXX：拒绝这份审批。\n请用机器人发给你的实际 XH 审批码替换示例编号。\n\n【发布与故障处理】\n发布状态 XH-XXXXXX：查看进度。\n检查原因 XH-XXXXXX：只检查，不触发发布。\n修复并重试 XH-XXXXXX：修复可处理的问题；结果未知时先查原发送，不直接重发。\n已登录 XH-XXXXXX：你完成扫码后，继续这篇任务；机器人会重新核验账号。编号标识作品，不是登录凭据。\n暂停发布 XH-XXXXXX / 继续发布 XH-XXXXXX：暂停或恢复任务。\n取消发布 XH-XXXXXX：停止后续提交；不能撤回已发帖，在途发送仍需查结果。\n确认未发布并重试 XH-XXXXXX：仅在你核实平台确实没有发布后使用。\n\n扫码是恢复账号登录，带编号是指定继续哪一篇。先按机器人提示完成登录，再发“已登录 编号”，这句话本身不会完成登录。\n例：选题 AI 智能体记忆，用生活例子解释，少用术语。\n例：修改 开头改成我的观点，保留第二张图。\n随时发送 帮助、菜单 或 help 查看本说明。'

_PUBLICATION_COMMAND_RE = re.compile(
    r"^(发布状态|检查原因|修复并重试|已登录|取消发布|拒绝发布|暂停发布|继续发布|确认未发布并重试)"
    r"\s*(XH-[A-Z0-9]{4,12})$",
    re.IGNORECASE,
)


def _revision_failure_text(outcome: str, accepted: dict) -> str:
    if outcome == "revision_ambiguous":
        rows = accepted.get("candidates") or []
        codes = "、".join(
            str(row.get("code") or "") for row in rows if row.get("code")
        )
        return (
            f"找到多篇可以修改的作品：{codes}。请回复"
            "“修改 XH-编号 + 具体意见”，我只会改你指定的那一篇。"
        )
    if outcome == "revision_no_target":
        code = str(accepted.get("explicit_code") or "")
        if code:
            return (
                f"没有找到可修改的 {code}。它可能已经发布，或不属于当前账号；"
                "已发布作品不能就地修改，也不会自动重发。请发送“发布状态 "
                f"{code}”核对。"
            )
        return (
            "没有找到可以修改的已送审作品。已发布作品不能就地修改；"
            "请先发送“选题 …”生成新稿，或发送“发布状态 XH-编号”核对。"
        )
    if outcome == "revision_in_flight":
        code = str(accepted.get("approval_code") or "")
        return (
            f"{code} 正在发布或已有在途发送，不能就地换稿。"
            "为避免旧批准覆盖新内容，我没有修改；请先发送“发布状态 "
            f"{code}”确认结果。"
        )
    if outcome == "revision_already_running":
        code = str(accepted.get("approval_code") or "")
        return (
            f"{code} 已有一轮修改正在进行。为避免两个版本互相覆盖，"
            "这次没有另起修改；请等这一轮结果后再发新意见。"
        )
    if outcome == "revision_needs_instruction":
        return "请说明具体要改什么，例如“修改 XH-编号 标题短一点，正文保留”。"
    if outcome == "revision_conflict":
        return "这篇作品的状态刚刚变化，我没有修改。请先发送“发布状态 XH-编号”核对。"
    return "这次修改没有执行，请发送“发布状态 XH-编号”核对后再试。"


def _replay_publication_reply(
    db: Database, config: Config, clock: Clock, message_id: str, existing: Any
) -> None:
    """Best-effort receipt for a publication command that was already accepted.

    Feishu retries callbacks.  The first delivery may have committed the inbound
    row and then crashed before queueing the reply.  A duplicate must not
    re-execute the command, but it should still leave the owner with one visible
    receipt.  The notification dedupe key is what makes this at-most-once.
    """
    if not bool(config.get("publication_agent.enabled", False)):
        return
    text = str(existing["text"] or "").strip()
    m = _PUBLICATION_COMMAND_RE.match(text)
    if m is None:
        return
    outcome = str(existing["outcome"] or "")
    if outcome.startswith("rejected_") or outcome in {
        "help", "missing_message_id", "rejected_no_owner", "rejected_sender",
    }:
        return
    if outcome == "processing":
        # A callback may be retried while the first delivery is still working.
        # Do not tell the owner it crashed during that window; the first call
        # will queue the real receipt.  Only an old processing row is evidence
        # that the process died between accepting the command and replying.
        accepted = parse_iso(existing["received_at"])
        if accepted is not None and (clock.now() - accepted).total_seconds() < 120:
            return
    dedupe_key = f"inbound:{message_id}"
    if db.fetchone("SELECT id FROM notifications WHERE dedupe_key=?", (dedupe_key,)) is not None:
        return
    detail = json_loads(existing["detail"], {}) or {}
    result = detail.get("publication") if isinstance(detail, dict) else None
    reply = ""
    if isinstance(result, dict) and outcome != "processing":
        from .publication_agent.entry import format_command_reply
        try:
            reply = format_command_reply(db, result, command_text=text)
        except Exception:
            reply = ""
    if not reply:
        action, code = m.group(1), m.group(2).upper()
        if outcome == "processing":
            reply = (
                f"已收到“{action} {code}”，但上次处理中断了，没有生成回执。"
                f"请重新发送一次“{action} {code}”。"
            )
        else:
            reply = f"已处理“{action} {code}”（{outcome}），但没有找到可复用的回执内容。"
    notifications.queue_outcome(db, config, clock, reply, dedupe_key=dedupe_key)


def handle_inbound_message(
    db: Database,
    config: Config,
    clock: Clock,
    *,
    message_id: str,
    sender_id: str,
    chat_id: str,
    chat_type: str,
    text: str,
    channel: str = "lark",
    llm=None,
) -> dict[str, Any]:
    """Handle one chat message.  Replays by message_id are no-ops."""
    # 幂等的第一步：先看 message_id 是否已经处理过。
    # 真正的“占位”在下面按路径完成：审批路径先写 processing；
    # 选题路径把 inbound 行、request、job、ack 放进同一个事务。
    now = to_iso(clock.now())
    message_id = (message_id or "").strip()
    if not message_id:
        return {"ok": False, "outcome": "missing_message_id", "detail": "message id is required"}
    with db.read() as conn:
        existing = conn.execute(
            "SELECT * FROM inbound_messages WHERE message_id=?", (message_id,)
        ).fetchone()
    if existing is not None:
        _replay_publication_reply(db, config, clock, message_id, existing)
        return {
            "ok": False,
            "duplicate": True,
            "outcome": existing["outcome"],
            "detail": existing["detail"] or "duplicate callback ignored",
        }

    owner = configured_owner(config)
    # 权限检查三条：必须配置 owner；发送者必须正好是 owner；
    # 选题/帮助/状态路径无论审批配置是否放宽，都强制要求 p2p。
    if not owner:
        return _record_rejected(
            db,
            now,
            message_id,
            channel,
            sender_id,
            chat_id,
            chat_type,
            text,
            "rejected_no_owner",
            "owner open_id is not configured; refusing all commands",
        )
    if sender_id != owner:
        return _record_rejected(
            db,
            now,
            message_id,
            channel,
            sender_id,
            chat_id,
            chat_type,
            text,
            "rejected_sender",
            "sender is not the bound owner; command ignored",
        )

    require_p2p = bool(config.get("notify.lark.require_p2p", True))
    is_p2p = (chat_type or "").lower() in {"p2p", "private"}

    pub_text = (text or "").strip()
    if pub_text.lower() in {"帮助", "菜单", "help", "/help"}:
        if not is_p2p:
            return _record_rejected(
                db, now, message_id, channel, sender_id, chat_id, chat_type,
                text, "rejected_chat_type", "帮助仅支持本人私聊",
            )
        with db.tx() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO inbound_messages"
                "(message_id,channel,sender_id,chat_id,chat_type,text,received_at,outcome,detail) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (message_id, channel, sender_id, chat_id, chat_type,
                 text, now, "help", "command menu"),
            )
            if not cursor.rowcount:
                return {"ok": False, "duplicate": True, "outcome": "help"}
            notifications.queue_outcome(
                db, config, clock, COMMAND_HELP,
                dedupe_key=f"help:{message_id}", conn=conn,
            )
        return {"ok": True, "outcome": "help"}

    if bool(config.get("publication_agent.enabled", False)) and re.match(
        r"^(发布状态|检查原因|修复并重试|已登录|取消发布|拒绝发布|暂停发布|继续发布|确认未发布并重试)\s*XH-",
        pub_text,
        re.IGNORECASE,
    ):
        if not is_p2p:
            return _record_rejected(
                db, now, message_id, channel, sender_id, chat_id, chat_type, text,
                "rejected_chat_type", "publication commands are only accepted in a p2p chat",
            )
        from .publication_agent.entry import format_command_reply, handle_command

        with db.tx() as conn:
            conn.execute(
                "INSERT INTO inbound_messages(message_id, channel, sender_id, chat_id, chat_type, text, "
                "received_at, outcome, detail) VALUES (?, ?, ?, ?, ?, ?, ?, 'processing', NULL)",
                (message_id, channel, sender_id, chat_id, chat_type, text[:4000], now),
            )
        result = handle_command(db, config, clock, pub_text, event_key=message_id)
        outcome = str(result.get("outcome") or "publication_command")
        detail = str(result.get("detail") or result.get("state") or "")
        try:
            reply = format_command_reply(db, result, command_text=pub_text)
        except Exception:
            # The command may already have changed state.  Never leave the owner
            # with silence just because rendering failed.
            reply = (
                f"命令已处理（{outcome}），但生成回执时出错了。"
                f"请发送“发布状态 {result.get('code') or ''}”重新查看。"
            )
        # State update and the visible receipt are committed together.  This closes
        # the window where a crash after accepting the command leaves no reply.
        with db.tx() as conn:
            conn.execute(
                "UPDATE inbound_messages SET outcome=?, detail=? WHERE message_id=?",
                (outcome, json_dumps({"detail": detail, "publication": result}), message_id),
            )
            notifications.queue_outcome(
                db, config, clock, reply,
                dedupe_key=f"inbound:{message_id}", conn=conn,
            )
        return {
            "ok": bool(result.get("ok")),
            "outcome": outcome,
            "message_id": message_id,
            "detail": result,
        }

    # Creator Agent V2 is the default new-topic path when enabled.  The
    # legacy ``选题``/``写一篇`` creation path is intentionally not reachable
    # any more in that mode; it would otherwise keep two divergent pipelines
    # for the same owner command.  The explicit ``V2`` prefix remains accepted
    # as an alias for old messages.  V2 itself creates one durable job and a
    # real pending approval later; it never publishes here.
    v2_text = (text or "").strip()
    plain_topic = v2_text.startswith("选题") or v2_text.startswith("写一篇")
    v2_command = (
        v2_text.upper() == "V2"
        or v2_text.upper().startswith("V2 ")
        or plain_topic
    )
    # Every native modification goes through the Creator revision path, with
    # or without an explicit XH code.  The old ``修改 XH-...`` route created a
    # separate legacy draft with a different evidence set.
    v2_revision = (
        v2_text.lower().startswith("v2 修改")
        or v2_text.lower().startswith("v2修改")
        or v2_text.startswith("修改")
    )
    if bool(config.get("creator_agent.enabled", False)) and (v2_command or v2_revision):
        if not is_p2p:
            return _record_rejected(
                db, now, message_id, channel, sender_id, chat_id, chat_type, text,
                "rejected_chat_type", "Creator Agent commands are only accepted in a p2p chat",
            )
        from .creator_agent.entry import accept_inbound, accept_revision

        try:
            if v2_revision:
                accepted = accept_revision(
                    db,
                    clock,
                    account_id=str(config.account().get("id", "xhs-main")),
                    message_id=message_id,
                    text=text,
                    sender_id=sender_id,
                    chat_type=chat_type,
                    config=config,
                )
            else:
                accepted = accept_inbound(
                    db,
                    clock,
                    account_id=str(config.account().get("id", "xhs-main")),
                    message_id=message_id,
                    text=text,
                    sender_id=sender_id,
                    chat_type=chat_type,
                    origin="manual",
                )
        except Exception as exc:  # noqa: BLE001 - malformed user command
            return _record_rejected(
                db, now, message_id, channel, sender_id, chat_id, chat_type, text,
                "creator_brief_error", f"creator brief rejected: {type(exc).__name__}: {exc}",
            )
        if not accepted.get("ok", True):
            outcome = str(accepted.get("outcome") or "creator_revision_rejected")
            if v2_revision:
                reply = _revision_failure_text(outcome, accepted)
                notifications.queue_outcome(
                    db,
                    config,
                    clock,
                    reply,
                    dedupe_key=f"inbound:{message_id}",
                )
            return {
                "ok": False,
                "outcome": outcome,
                "message_id": message_id,
                "detail": accepted,
            }
        return {
            "ok": True,
            "outcome": str(accepted.get("outcome") or "creator_agent_queued"),
            "duplicate": bool(accepted.get("duplicate")),
            "message_id": message_id,
            "detail": accepted,
        }

    action, code, comment = _parse_command(text)
    if action is None:
        # The fixed topic pipeline was retired.  Keep a durable record and an
        # owner-visible hint instead of silently dropping unknown commands.
        if not is_p2p:
            return _record_rejected(
                db,
                now,
                message_id,
                channel,
                sender_id,
                chat_id,
                chat_type,
                text,
                "rejected_chat_type",
                "Creator Agent commands are only accepted in a p2p chat",
            )
        detail = (
            "未识别该命令。发送 帮助 查看完整用法。也可以直接发送：选题 <主题/链接>；"
            "修改 <意见>；批准 XH-XXXXXX；拒绝 XH-XXXXXX。"
        )
        result = _record_rejected(
            db,
            now,
            message_id,
            channel,
            sender_id,
            chat_id,
            chat_type,
            text,
            "legacy_command_retired",
            detail,
        )
        notifications.queue_outcome(
            db,
            config,
            clock,
            detail,
            dedupe_key=f"legacy-command:{message_id}",
        )
        return result

    if require_p2p and not is_p2p:
        return _record_rejected(
            db,
            now,
            message_id,
            channel,
            sender_id,
            chat_id,
            chat_type,
            text,
            "rejected_chat_type",
            "approval commands are only accepted in a p2p chat",
        )

    # Approval/revision path: claim the message id before any decision work.
    with db.tx() as conn:
        existing = conn.execute(
            "SELECT * FROM inbound_messages WHERE message_id=?", (message_id,)
        ).fetchone()
        if existing is not None:
            return {
                "ok": False,
                "duplicate": True,
                "outcome": existing["outcome"],
                "detail": existing["detail"] or "duplicate callback ignored",
            }
        conn.execute(
            "INSERT INTO inbound_messages(message_id, channel, sender_id, chat_id, chat_type, text, "
            "received_at, outcome, detail) VALUES (?, ?, ?, ?, ?, ?, ?, 'processing', NULL)",
            (message_id, channel, sender_id, chat_id, chat_type, text[:4000], now),
        )

    def finish(
        outcome: str,
        detail: str,
        *,
        extra: dict | None = None,
        notify_owner: bool = False,
    ) -> dict[str, Any]:
        result = _finish_message(db, now, message_id, outcome, detail, extra=extra)
        if notify_owner:
            # Only the bound owner in a p2p chat may receive outcome text.
            notifications.queue_outcome(
                db,
                config,
                clock,
                _outcome_text(outcome, detail, extra or {}),
                dedupe_key=f"inbound:{message_id}",
            )
        return result

    if not re.fullmatch(r"XH-[A-Z0-9]{6}", code):
        # 严格校验审批码格式，挡住乱输和注入尝试。
        return finish("invalid_code", "approval code format is invalid", notify_owner=True)

    approval = db.fetchone("SELECT * FROM approvals WHERE code=?", (code,))
    if approval is None:
        return finish("unknown_code", "no approval with that code", notify_owner=True)
    if approval["status"] not in {"pending", "approved"}:
        return finish(
            f"rejected_{approval['status']}",
            f"approval is already {approval['status']}",
            extra={"code": code},
            notify_owner=True,
        )
    expires = parse_iso(approval["expires_at"])
    if expires is not None and expires <= clock.now():
        expire_approvals(db, clock)
        return finish("expired", "approval has expired", extra={"code": code}, notify_owner=True)

    # A phone (Feishu) approval is only effective after the final text AND every
    # image were confirmed delivered.  Text arriving is not enough: the owner
    # must have seen the exact images bound to the approval.  The authenticated
    # local console (and the local CLI) remain separate deliberate paths.
    if action == "approve" and channel == "lark":
        notice = db.fetchone(
            "SELECT state, attempts, error FROM notifications WHERE dedupe_key=?",
            (f"approval:{approval['id']}",),
        )
        if notice is None or notice["state"] != "sent":
            state = notice["state"] if notice is not None else "missing"
            detail = (
                f"approval notification is not fully delivered (state={state}); "
                "images may be missing. Retry delivery and send a new approval message after it completes."
            )
            if notice is not None and notice["error"]:
                detail += f" Last delivery error: {notice['error']}"
            return finish(
                "delivery_incomplete", detail, extra={"code": code}, notify_owner=True
            )

    ok, reason, images = verify_approval_artifact(db, approval)
    if not ok:
        # 内容已经和审批时不一致：审批作废，草稿标 approval_invalidated。
        # 不自动重新审批，必须用户重新看过新内容。
        with db.tx() as conn:
            conn.execute(
                "UPDATE approvals SET status='invalidated', decided_at=?, decided_by=?, decision_comment=? "
                "WHERE id=? AND status='pending'",
                (now, sender_id, reason, approval["id"]),
            )
            conn.execute(
                "UPDATE drafts SET status='approval_invalidated', updated_at=? WHERE id=?",
                (now, approval["draft_id"]),
            )
            audit(
                conn,
                "approval",
                "approval.invalidated",
                entity="approval",
                entity_id=approval["id"],
                detail={"reason": reason, "message_id": message_id},
                at=now,
            )
        return finish("invalidated", reason, extra={"code": code}, notify_owner=True)

    if action == "approve":
        if approval["status"] == "approved":
            # 重复批准是幂等的：不报错，也不再排队发布。
            return finish(
                "already_approved",
                "approval was already recorded",
                extra={"code": code},
                notify_owner=True,
            )
        with db.tx() as conn:
            cur = conn.execute(
                "UPDATE approvals SET status='approved', decided_at=?, decided_by=?, decision_comment=? "
                "WHERE id=? AND status='pending'",
                (now, sender_id, comment or "approved by owner", approval["id"]),
            )
            if cur.rowcount != 1:
                # 并发回调竞争：另一个请求已经先改了状态。
                # 我们这边不重试、不重复排队，直接报告 race_lost。
                return finish(
                    "race_lost",
                    "another callback already changed this approval",
                    extra={"code": code},
                    notify_owner=True,
                )
            conn.execute(
                "UPDATE drafts SET status='approved', updated_at=? WHERE id=? AND status='review_pending'",
                (now, approval["draft_id"]),
            )
            conn.execute(
                "UPDATE events SET status='approved' WHERE id=(SELECT event_id FROM drafts WHERE id=?)",
                (approval["draft_id"],),
            )
            audit(
                conn,
                sender_id,
                "approval.approved",
                entity="approval",
                entity_id=approval["id"],
                detail={"code": code, "draft_id": approval["draft_id"], "message_id": message_id},
                at=now,
            )
        # Queue exactly one publish job per approval.  The publish gate also
        # has a UNIQUE dedupe key, so this is defence in depth.
        # 每个审批最多排一个发布任务；发布闸门里还有唯一键兜底，
        # 即使这里重复调用也不会产生两次发布。
        from ..jobs import enqueue_job

        if bool(config.get("publication_agent.enabled", False)):
            from .publication_agent.entry import create_task_for_approval

            try:
                task_result = create_task_for_approval(
                    db, config, clock, int(approval["id"])
                )
            except Exception as exc:  # noqa: BLE001 - approval stays approved; owner sees error
                return finish(
                    "approved",
                    f"approved {code}; publication task creation failed: {type(exc).__name__}: {exc}",
                    extra={"code": code},
                    notify_owner=True,
                )
            return finish(
                "approved",
                f"approved {code}; publication task {task_result['task']['code']} queued",
                extra={"code": code, "task": task_result["task"]},
                notify_owner=True,
            )

        enqueue_job(
            db,
            "publish",
            f"publish:approval:{approval['id']}",
            payload={"approval_id": int(approval["id"])},
            run_at=parse_iso(approval["scheduled_at"]),
            clock=clock,
            priority=5,
        )
        return finish(
            "approved",
            f"approved {code}; queued for the publish gate",
            extra={"code": code},
            notify_owner=True,
        )

    if action == "reject":
        # 拒绝同样用 WHERE status='pending' 防并发。
        # 草稿和事件都标 rejected，并记录拒绝理由，供账号记忆学习。
        with db.tx() as conn:
            cur = conn.execute(
                "UPDATE approvals SET status='rejected', decided_at=?, decided_by=?, decision_comment=? "
                "WHERE id=? AND status='pending'",
                (now, sender_id, comment or "rejected by owner", approval["id"]),
            )
            if cur.rowcount != 1:
                return finish(
                    "race_lost",
                    "approval already decided",
                    extra={"code": code},
                    notify_owner=True,
                )
            conn.execute(
                "UPDATE drafts SET status='rejected', updated_at=? WHERE id=?",
                (now, approval["draft_id"]),
            )
            conn.execute(
                "UPDATE events SET status='rejected', reject_reason=? "
                "WHERE id=(SELECT event_id FROM drafts WHERE id=?)",
                (comment or "rejected by owner", approval["draft_id"]),
            )
            audit(
                conn,
                sender_id,
                "approval.rejected",
                entity="approval",
                entity_id=approval["id"],
                detail={"code": code, "comment": comment, "message_id": message_id},
                at=now,
            )
        return finish(
            "rejected",
            f"rejected {code}; nothing will be published",
            extra={"code": code},
            notify_owner=True,
        )

    # Revision is durable background work.  The callback must return promptly,
    # so it never calls the model; the worker's "revise" handler does that.
    if not comment:
        return finish(
            "revision_needs_instruction",
            "修改命令需要附上修改意见",
            extra={"code": code},
            notify_owner=True,
        )
    from ..jobs import enqueue_job

    # Freeze the old version and enqueue the revision in one transaction.  A
    # revision request must never leave a still-publishable approval behind:
    # the old work becomes revision_pending before any model call is queued.
    # Publishing already in flight cannot be recalled, so refuse in that case.
    refusal: tuple[str, str] | None = None
    job_id: int | None = None
    with db.tx() as conn:
        fresh = conn.execute(
            "SELECT * FROM approvals WHERE id=?", (approval["id"],)
        ).fetchone()
        if fresh is None:
            refusal = ("unknown_code", "no approval with that code")
        elif fresh["status"] not in {"pending", "approved"}:
            refusal = (
                "revision_refused",
                f"approval is {fresh['status']}; revision is only possible before publishing starts",
            )
        else:
            cur = conn.execute(
                "UPDATE approvals SET status='revision_pending', decided_at=?, decided_by=?, "
                "decision_comment=? WHERE id=? AND status IN ('pending','approved')",
                (now, sender_id, f"revision requested: {comment}", approval["id"]),
            )
            if cur.rowcount != 1:
                refusal = ("race_lost", "another callback already changed this approval")
            else:
                conn.execute(
                    "UPDATE drafts SET status='revision_pending', updated_at=? WHERE id=? "
                    "AND status IN ('drafted','review_pending','approved')",
                    (now, approval["draft_id"]),
                )
                conn.execute(
                    "UPDATE events SET status='revision_pending' WHERE id="
                    "(SELECT event_id FROM drafts WHERE id=?) "
                    "AND status NOT IN ('published','simulated','rejected')",
                    (approval["draft_id"],),
                )
                # The approval state already blocks the gate; cancel a queued
                # publish job as well so it does not wake up for a frozen work.
                conn.execute(
                    "UPDATE jobs SET state='cancelled', lease_owner=NULL, lease_until=NULL, "
                    "last_error=?, updated_at=? WHERE kind='publish' AND dedupe_key=? "
                    "AND state='pending'",
                    (
                        "approval moved to revision_pending",
                        now,
                        f"publish:approval:{approval['id']}",
                    ),
                )
                job_id = enqueue_job(
                    db,
                    "revise",
                    f"revise:approval:{approval['id']}",
                    payload={
                        "approval_id": int(approval["id"]),
                        "instruction": comment,
                        "actor": sender_id,
                        "message_id": message_id,
                    },
                    clock=clock,
                    priority=4,
                    max_attempts=1,
                    conn=conn,
                )
                audit(
                    conn,
                    sender_id,
                    "approval.revision_pending",
                    entity="approval",
                    entity_id=approval["id"],
                    detail={
                        "code": code,
                        "draft_id": approval["draft_id"],
                        "job_id": job_id,
                        "message_id": message_id,
                    },
                    at=now,
                )
    if refusal is not None:
        outcome, detail = refusal
        return finish(outcome, detail, extra={"code": code}, notify_owner=True)
    return finish(
        "revision_queued",
        f"修改请求已排队（job #{job_id}）；新版本生成后会重新请求批准",
        extra={"code": code, "job_id": job_id},
        notify_owner=True,
    )


def _record_rejected(
    db: Database,
    now: str,
    message_id: str,
    channel: str,
    sender_id: str,
    chat_id: str,
    chat_type: str,
    text: str,
    outcome: str,
    detail: str,
) -> dict[str, Any]:
    """Atomically record a message that is refused before any work.

    No owner notification is queued for an unauthorized sender or group chat;
    the inbound row is still durable so callback replay cannot retry it.
    """
    with db.tx() as conn:
        existing = conn.execute(
            "SELECT * FROM inbound_messages WHERE message_id=?", (message_id,)
        ).fetchone()
        if existing is not None:
            return {
                "ok": False,
                "duplicate": True,
                "outcome": existing["outcome"],
                "detail": existing["detail"] or "duplicate callback ignored",
            }
        conn.execute(
            "INSERT INTO inbound_messages(message_id, channel, sender_id, chat_id, chat_type, text, "
            "received_at, outcome, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message_id,
                channel,
                sender_id,
                chat_id,
                chat_type,
                (text or "")[:4000],
                now,
                outcome,
                json_dumps({"detail": detail}),
            ),
        )
    return {"ok": False, "outcome": outcome, "detail": detail}


def _finish_message(
    db: Database, now: str, message_id: str, outcome: str, detail: str, *, extra: dict | None = None
) -> dict[str, Any]:
    # 把处理结果写回 inbound_messages，并返回给调用方。
    # detail 里可以附带审批码/草稿 ID 等额外信息。
    with db.tx() as conn:
        conn.execute(
            "UPDATE inbound_messages SET outcome=?, detail=? WHERE message_id=?",
            (outcome, json_dumps({"detail": detail, **(extra or {})}), message_id),
        )
    return {
        "ok": outcome
        in {"approved", "rejected", "revised", "revision_queued", "already_approved"},
        "outcome": outcome,
        "detail": detail,
        **(extra or {}),
    }


def _outcome_text(outcome: str, detail: str, extra: dict[str, Any]) -> str:
    labels = {
        "approved": "已批准",
        "rejected": "已拒绝",
        "already_approved": "此前已批准",
        "delivery_incomplete": "未执行：审批通知未完整送达",
        "invalidated": "审批已失效（内容与审批时不一致）",
        "expired": "审批已过期",
        "revision_queued": "修改请求已排队",
        "revision_refused": "修改被拒绝：发布已开始或已完成",
        "revision_already_pending": "修改已在排队，不能重复提交",
        "revision_needs_instruction": "修改命令缺少具体意见",
        "invalid_code": "审批码格式无效",
        "unknown_code": "找不到该审批码",
        "race_lost": "审批状态已被其他操作改变",
    }
    lines = [f"审批结果：{labels.get(outcome, outcome)}"]
    if extra.get("code"):
        lines.append(f"审批码：{extra['code']}")
    lines.append(str(detail))
    return "\n".join(lines)


def decision_view(db: Database, approval_id: int) -> dict[str, Any] | None:
    # 控制台详情视图：审批记录 + 对应草稿 + 解析后的决策备注。
    approval = get_approval(db, approval_id=approval_id)
    if approval is None:
        return None
    view = dict(approval)
    draft = db.fetchone("SELECT * FROM drafts WHERE id=?", (approval["draft_id"],))
    view["draft"] = dict(draft) if draft is not None else None
    view["decision_detail"] = json_loads(view.get("decision_comment"), None)
    return view

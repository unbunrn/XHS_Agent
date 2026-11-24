"""Notification queue for approval messages.

The approval row is the source of truth.  A notification is only a request to
deliver it; delivery can be retried safely because the message is idempotent on
the approval code and the approval decision is idempotent on the message id.
"""

# =============================================================================
# 中文导读：审批通知的“待发件箱”
# =============================================================================
# 这个模块不负责“怎么发”（那是 adapters/notify.py 的事），
# 它负责“把要发的消息登记下来，然后一条条发出去”。
#
# 为什么不直接在创建审批时调飞书？
#   因为网络会失败、程序会重启。如果直接发，发到一半崩了，
#   就不知道到底发没发。改成“先写进数据库队列，再由投递器去发”：
#     - 崩溃后重启，队列里的 pending/retry 还会继续发；
#     - 同一条通知有 dedupe_key，重复登记不会产生两条；
#     - 发送失败会重试最多 5 次，之后标 failed，不会无限刷。
#
# 通知状态：
#   pending  刚登记，等待发送
#   retry    上次发送失败，等下一轮再试
#   sending  已占坑，正在发送（防止两个进程同时发）
#   sent     发送成功
#   failed   重试次数用尽，放弃
#
# 一个关键设计：审批行（approvals）才是“事实来源”，
# 通知只是“把审批送给人看”的快递单。快递丢了可以补发，
# 但审批本身不会因为通知失败而改变。
# =============================================================================

from __future__ import annotations

import sqlite3
from urllib.parse import urlsplit
from typing import Any

from ..adapters.notify import Notifier, approval_message
from ..config import Config
from ..db import Database, json_dumps, json_loads
from ..timeutil import Clock, to_iso

KIND_APPROVAL = "approval"
KIND_OUTCOME = "outcome"


def preview_url(config: Config, approval_id: int) -> str:
    # 生成审批详情页的本机地址，方便在通知里点开预览。
    # 如果控制台监听的是 0.0.0.0（所有网卡），浏览器不能直接打开这个地址，
    # 所以改写成 127.0.0.1；控制台本身也只允许本机访问。
    host = str(config.get("console.host", "127.0.0.1"))
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    port = int(config.get("console.port", 8765))
    return f"http://{host}:{port}/approval/{int(approval_id)}"


def publish_safety_note(config: Config) -> str:
    """Say explicitly whether approval can cause a real publish right now.

    The four-key publish gate is the only source of truth.  In particular a
    live config with the xiaohongshu provider must not hide the fact that
    ``live_enabled`` is still false or the environment key is absent.
    """
    if config.is_live_publish_armed():
        return "真实发布已武装；批准后仍由发布闸门核对审批、内容指纹与时间。"
    if str(config.get("publisher.provider", "mock")) == "mock" or config.mode != "live":
        return "模式：本地模拟（不会真实发布）。试运行：真实发布关闭，批准仅记录审稿决定。"
    return "试运行：真实发布关闭，批准仅记录审稿决定。"


def build_approval_message(db: Database, config: Config, approval: sqlite3.Row) -> dict[str, Any]:
    # 把审批行、草稿正文、图片清单、来源和不确定性拼成一条人类可读的消息。
    # 注意这里只“组装数据”，不发送；发送由 deliver_pending 负责。
    draft = db.fetchone("SELECT * FROM drafts WHERE id=?", (approval["draft_id"],))
    if draft is None:
        raise ValueError("draft for approval is missing")
    images = db.fetchall(
        "SELECT path FROM draft_images WHERE draft_id=? ORDER BY ordinal", (draft["id"],)
    )
    body_json = json_loads(draft["body_json"], {}) or {}
    # 来源列表来自写稿时保存的结构化数据（name + url），
    # 不是让 LLM 随手编的字符串，这样用户能点回原文核对。
    content_sources = body_json.get("sources") or []
    role_by_url: dict[str, tuple[str, bool]] = {}
    if draft["event_id"]:
        for row in db.fetchall(
            "SELECT source_url, source_role, source_verified FROM evidence WHERE event_id=?",
            (int(draft["event_id"]),),
        ):
            role_by_url[str(row["source_url"] or "")] = (
                str(row["source_role"] or "unknown"),
                bool(row["source_verified"]),
            )
    sources = []
    for item in content_sources:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        url = str(item.get("url") or "")
        if name or url:
            role, verified = role_by_url.get(url, ("unknown", False))
            try:
                source_host = (urlsplit(url).hostname or "").lower().rstrip(".")
            except ValueError:
                source_host = ""
            if source_host == "aihot.news":
                label = "AIHot 聚合摘要/原文未取到（非官方）"
            elif verified and role in {"official", "vendor_primary"}:
                label = "官方/一手"
            elif role == "secondary":
                label = "非官方媒体/转述"
            elif role == "unknown":
                label = "来源未核实"
            else:
                label = role
            sources.append(f"{name} {url}".strip() + f"（{label}）")
    uncertainties = [str(x) for x in (body_json.get("uncertainties") or []) if str(x).strip()]
    review_note = ""
    if draft["topic_request_id"]:
        topic = db.fetchone(
            "SELECT suggested_domains_json, source_review_json FROM topic_requests WHERE id=?",
            (int(draft["topic_request_id"]),),
        )
        if topic is not None:
            review = json_loads(topic["source_review_json"], {}) or {}
            domains = json_loads(topic["suggested_domains_json"], []) or []
            if isinstance(review, dict) and review.get("used") and domains:
                review_note = (
                    "来源范围说明：配置的可信域名没有直接命中，本次使用了模型建议的候选官方域名 "
                    + "、".join(str(item) for item in domains[:3])
                    + " 做一次性补充检索；候选未加入长期可信源，最终证据仍以正文引用和来源链接为准，"
                    "请你人工核对后再决定是否批准。"
                )
    # 只要发布闸门没有完全武装，就在消息里明确写“真实发布关闭”。
    # 旧逻辑仅在 mock/offline 时提示，导致 live + xiaohongshu + live_enabled=false
    # 的试运行配置反而看不到最关键的“批准只记录审稿决定”。
    simulated_note = publish_safety_note(config)
    return approval_message(
        code=approval["code"],
        platform=approval["platform"],
        account_id=approval["account_id"],
        draft_id=int(draft["id"]),
        draft_version=int(draft["version"]),
        title=draft["title"],
        body_sha256=approval["body_sha256"],
        images_sha256=approval["images_sha256"],
        scheduled_at=approval["scheduled_at"],
        expires_at=approval["expires_at"],
        timezone_name=config.timezone_name,
        preview_url=preview_url(config, int(approval["id"])),
        simulated_note=simulated_note,
        body=str(draft["body"]),
        image_paths=[str(row["path"]) for row in images],
        sources=sources,
        uncertainties=uncertainties,
        review_note=review_note,
    )


def queue_approval(
    db: Database,
    config: Config,
    clock: Clock,
    approval: sqlite3.Row,
    *,
    conn: sqlite3.Connection | None = None,
) -> int:
    # 把一条审批通知登记进队列，返回通知行 ID。
    # dedupe_key = approval:<id>，配合 INSERT OR IGNORE：
    # 同一个审批被登记多次，最终也只会有一条通知。
    message = build_approval_message(db, config, approval)
    payload = {
        "approval_id": int(approval["id"]),
        "code": approval["code"],
        "message": message,
    }
    dedupe = f"approval:{approval['id']}"
    now = to_iso(clock.now())

    def _insert(conn: sqlite3.Connection) -> int:
        # INSERT OR IGNORE + 唯一 dedupe_key：已经登记过就什么都不做。
        # 随后再按 dedupe_key 查出 ID，保证无论新插还是旧有都返回同一行。
        conn.execute(
            "INSERT OR IGNORE INTO notifications(kind, dedupe_key, channel, payload_json, state, attempts, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', 0, ?)",
            (KIND_APPROVAL, dedupe, str(config.get("notify.provider", "fake")), json_dumps(payload), now),
        )
        row = conn.execute("SELECT id FROM notifications WHERE dedupe_key=?", (dedupe,)).fetchone()
        return int(row["id"])

    # conn 参数的存在是为了让调用方把“创建审批”和“登记通知”
    # 放进同一个事务：要么两件事都成功，要么都不做。
    # 不传 conn 时自己开一个事务。
    if conn is not None:
        return _insert(conn)
    with db.tx() as own:
        return _insert(own)


def queue_outcome(
    db: Database,
    config: Config,
    clock: Clock,
    text: str,
    *,
    dedupe_key: str,
    image_paths: list[str] | None = None,
    cleanup_images: bool = False,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Queue a durable text (and optional image) outcome for the bound owner private chat.

    The payload carries no recipient: delivery always uses the notifier's
    configured owner open_id, never a sender supplied by the callback.
    """
    text = str(text or "").strip()
    if not text:
        raise ValueError("outcome notification text is empty")
    payload: dict[str, Any] = {"text": text}
    imgs = [str(p) for p in (image_paths or []) if str(p).strip()]
    if imgs:
        payload["image_paths"] = imgs
    if cleanup_images:
        payload["cleanup_images"] = True
    now = to_iso(clock.now())

    def _insert(target: sqlite3.Connection) -> int:
        target.execute(
            "INSERT OR IGNORE INTO notifications(kind, dedupe_key, channel, payload_json, state, attempts, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', 0, ?)",
            (
                KIND_OUTCOME,
                dedupe_key,
                str(config.get("notify.provider", "fake")),
                json_dumps(payload),
                now,
            ),
        )
        row = target.execute("SELECT id FROM notifications WHERE dedupe_key=?", (dedupe_key,)).fetchone()
        return int(row["id"])

    if conn is not None:
        return _insert(conn)
    with db.tx() as own:
        return _insert(own)


def deliver_pending(
    db: Database,
    config: Config,
    clock: Clock,
    notifier: Notifier,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    # 投递器：把队列里 pending/retry 的通知逐条发出去。
    # “发送”这个动作不能放在事务里（网络可能很慢），
    # 所以用“先占坑、再发送、最后记账”的三步：
    #   1. 事务内把状态改成 sending（条件更新，只有一个进程能成功）；
    #   2. 事务外调用通知适配器；
    #   3. 事务内写 sent 或 retry/failed。
    results: list[dict[str, Any]] = []
    rows = db.fetchall(
        "SELECT * FROM notifications WHERE state IN ('pending','retry') ORDER BY id LIMIT ?", (int(limit),)
    )
    for row in rows:
        # Claim transactionally; a single process is expected, but a restart or
        # a second worker must not send the same approval twice.
        with db.tx() as conn:
            cur = conn.execute(
                "UPDATE notifications SET state='sending', attempts=attempts+1 "
                "WHERE id=? AND state IN ('pending','retry')",
                (row["id"],),
            )
            if cur.rowcount != 1:
                # 没抢到：说明另一个进程已经把它改成 sending 了。
                # 直接跳过，绝不能两个进程发同一条。
                continue
        payload = json_loads(row["payload_json"], {}) or {}
        if row["kind"] == KIND_OUTCOME:
            text = str(payload.get("text") or "")
            image_paths = [str(p) for p in (payload.get("image_paths") or []) if str(p).strip()]
            cleanup_images = bool(payload.get("cleanup_images", False))
            try:
                if image_paths:
                    try:
                        result = notifier.send_text(text, image_paths=image_paths)
                    except TypeError:
                        result = notifier.send_text(text)
                else:
                    result = notifier.send_text(text)
            except Exception as exc:  # noqa: BLE001 - missing credentials must be explicit
                attempts = int(row["attempts"]) + 1
                state = "failed" if attempts >= 5 else "retry"
                with db.tx() as conn:
                    conn.execute(
                        "UPDATE notifications SET state=?, error=? WHERE id=?",
                        (state, f"{type(exc).__name__}: {exc}", row["id"]),
                    )
                if cleanup_images and state == "failed":
                    for img_path in image_paths:
                        try:
                            from pathlib import Path

                            Path(img_path).unlink(missing_ok=True)
                        except OSError:
                            pass
                results.append({"id": row["id"], "ok": False, "error": str(exc), "state": state})
                continue
            if not result.ok:
                attempts = int(row["attempts"]) + 1
                state = "failed" if attempts >= 5 else "retry"
                with db.tx() as conn:
                    conn.execute(
                        "UPDATE notifications SET state=?, error=? WHERE id=?",
                        (state, result.detail, row["id"]),
                    )
                if cleanup_images and state == "failed":
                    for img_path in image_paths:
                        try:
                            from pathlib import Path

                            Path(img_path).unlink(missing_ok=True)
                        except OSError:
                            pass
                results.append(
                    {"id": row["id"], "ok": False, "state": state, "detail": result.detail}
                )
                continue
            with db.tx() as conn:
                conn.execute(
                    "UPDATE notifications SET state='sent', sent_at=?, error=NULL WHERE id=?",
                    (to_iso(clock.now()), row["id"]),
                )
            if cleanup_images:
                for img_path in image_paths:
                    try:
                        from pathlib import Path

                        Path(img_path).unlink(missing_ok=True)
                    except OSError:
                        pass
            results.append(
                {
                    "id": row["id"],
                    "ok": True,
                    "channel": result.channel,
                    "message_id": result.message_id,
                    "state": "sent",
                }
            )
            continue
        if row["kind"] != KIND_APPROVAL:
            with db.tx() as conn:
                conn.execute(
                    "UPDATE notifications SET state='failed', error=? WHERE id=?",
                    (f"unsupported notification kind: {row['kind']}", row["id"]),
                )
            results.append({"id": row["id"], "ok": False, "state": "failed"})
            continue
        approval_id = payload.get("approval_id")
        # A notification is only a prompt to decide on a still-pending approval.
        # If the approval was already accepted (for example in the authenticated
        # local console), do not resend a stale prompt and do not create a
        # duplicate decision path.
        approval_status = db.scalar(
            "SELECT status FROM approvals WHERE id=?", (int(approval_id or 0),)
        )
        if approval_status != "pending":
            with db.tx() as conn:
                conn.execute(
                    "UPDATE notifications SET state='cancelled', error=? WHERE id=?",
                    (
                        f"approval is already {approval_status or 'missing'}; prompt not resent",
                        row["id"],
                    ),
                )
            results.append(
                {
                    "id": row["id"],
                    "ok": False,
                    "cancelled": True,
                    "state": "cancelled",
                    "detail": f"approval is already {approval_status or 'missing'}",
                }
            )
            continue
        # 消息正文在登记时就已冻结在 payload 里。
        # 即使之后草稿被修改，这条待发通知仍保持当时的审批内容，
        # 不会“发出去的”和“批准的”对不上。
        message = payload.get("message") or {}
        try:
            result = notifier.send_approval(message)
        except Exception as exc:  # noqa: BLE001 - missing credentials must be explicit
            # 发送异常也要落库，不能让通知永远停在 sending。
            # 最多试 5 次（第 5 次失败后标 failed），避免无限重试。
            attempts = int(row["attempts"]) + 1
            state = "failed" if attempts >= 5 else "retry"
            with db.tx() as conn:
                conn.execute(
                    "UPDATE notifications SET state=?, error=? WHERE id=?",
                    (state, f"{type(exc).__name__}: {exc}", row["id"]),
                )
            results.append({"id": row["id"], "ok": False, "error": str(exc), "state": state})
            continue
        if not result.ok:
            # Partial delivery (for example, text arrived but an image upload
            # failed) is retryable and visible.  It is not "sent", so the Feishu
            # approval path cannot become effective yet.
            attempts = int(row["attempts"]) + 1
            state = "failed" if attempts >= 5 else "retry"
            with db.tx() as conn:
                conn.execute(
                    "UPDATE notifications SET state=?, error=? WHERE id=?",
                    (state, result.detail, row["id"]),
                )
            results.append(
                {
                    "id": row["id"],
                    "ok": False,
                    "message_id": result.message_id,
                    "channel": result.channel,
                    "state": state,
                    "detail": result.detail,
                    "images_sent": result.images_sent,
                    "images_total": result.images_total,
                }
            )
            continue
        cancelled = False
        with db.tx() as conn:
            # Re-check inside the final transaction: the owner may have accepted
            # the decision locally while the Feishu send was in flight.  Do not
            # create a second visible approval prompt in that case.
            current = conn.execute(
                "SELECT status FROM approvals WHERE id=?", (int(approval_id or 0),)
            ).fetchone()
            if current is None or current["status"] != "pending":
                conn.execute(
                    "UPDATE notifications SET state='cancelled', error=? WHERE id=?",
                    (
                        f"approval is already {current['status'] if current is not None else 'missing'}; "
                        "prompt not marked delivered",
                        row["id"],
                    ),
                )
                cancelled = True
            else:
                # 先记“通知已完整发送”，再更新审批的 channel/notified_at。
                # notified_at IS NULL 条件保证只记录第一次成功通知的时间。
                conn.execute(
                    "UPDATE notifications SET state='sent', sent_at=?, error=NULL WHERE id=?",
                    (to_iso(clock.now()), row["id"]),
                )
                conn.execute(
                    "UPDATE approvals SET channel=?, notified_at=? WHERE id=? AND notified_at IS NULL",
                    (result.channel, to_iso(clock.now()), payload.get("approval_id")),
                )
        if cancelled:
            results.append(
                {
                    "id": row["id"],
                    "ok": False,
                    "cancelled": True,
                    "state": "cancelled",
                    "detail": "approval was accepted while the notification was being sent",
                }
            )
            continue
        results.append(
            {
                "id": row["id"],
                "ok": result.ok,
                "message_id": result.message_id,
                "channel": result.channel,
                "path": result.path,
                "state": "sent",
            }
        )
    return results


def recover_inflight(db: Database) -> int:
    """A crash while sending leaves 'sending'; retry is safe (same approval code)."""
    # 中文：进程在发送途中崩溃，会留下 sending 状态。
    # 重启后把这些通知改回 retry 再发一次。为什么重发是安全的？
    # 因为同一条审批的消息内容完全相同，且审批码相同，
    # 用户看到两条一样的通知也只是重复，不会造成错误批准。
    # 这与“发布”完全不同：发布重试可能造成重复发帖，所以那边绝不重试。
    with db.tx() as conn:
        cur = conn.execute("UPDATE notifications SET state='retry' WHERE state='sending'")
        return int(cur.rowcount)

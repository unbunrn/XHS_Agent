"""Draft generation: research evidence -> LLM -> validation -> PNG -> approval."""

# =============================================================================
# 中文导读：把选题变成“待审批的一版稿子”
# =============================================================================
# 一次 create_draft 的完整流程：
#   1. 检查限额（今天写了几篇、待审批几篇、队列几篇）；
#   2. 选出要写的事件，取出证据；
#   3. 调模型写稿；
#   4. 用 content.py 严格校验；
#   5. 插入 drafts 表（版本号 +1，算正文哈希）；
#   6. 渲染 PNG 卡片，保存每张图的哈希；
#   7. 生成审批单，把状态改成 review_pending，并排队发通知。
#
# 为什么“修订”不是原地改？
#   用户的审批只对“某个具体版本”有效。修订会创建新版本草稿，
#   旧审批标记为 superseded，旧草稿也标记 superseded。
#   新版本必须重新审批。这样绝不会出现“批的是 A，发的是 B”。
#
# 为什么渲染失败就标 failed？
#   没有图片就没有可审批的发布物。宁可失败重来，也不能拿半成品去让人批。
# =============================================================================

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from PIL import Image

from ..adapters.types import BudgetExceeded, LLMError
from ..audit import audit
from ..config import Config
from ..content import DraftContent, validate_draft_payload
from ..db import Database, json_dumps, json_loads
from ..editorial import (
    RevisionUnsatisfied,
    change_summary_lines,
    deterministic_revision_check,
    infer_revision_request,
    parse_semantic_review,
    semantic_review_payload,
)
from ..render import RenderedImage, render_draft
from ..security import images_digest, sha256_file, sha256_text
from ..timeutil import Clock, local_day_bounds_utc, next_publish_slot, parse_iso, to_iso
from . import approvals, notifications
from .critic import build_critic_payload, critic_instruction, parse_critic_review
from .research import (
    build_writer_payload,
    date_is_unknown,
    next_researched_event,
    research_event,
)

TERMINAL_DRAFT_STATUSES = {
    # “终态”= 这篇草稿不会再继续走发布流程。
    # active_topic_count 用它来判断还有多少选题“在流程中”。
    "published",
    "simulated",
    "rejected",
    "expired",
    "failed",
    "approval_invalidated",
    "superseded",
    "publish_unknown",
}


class DraftError(RuntimeError):
    # 写稿阶段的“业务规则不允许”错误：限额、证据不足、校验失败、渲染失败。
    # worker 捕获它后直接跳过，不重试——重试只会重复违反同一条规则。
    pass


def drafts_created_today(db: Database, config: Config, clock: Clock, *, manual: bool | None = None) -> int:
    start, end = local_day_bounds_utc(clock.now(), config.timezone_name)
    sql = "SELECT COUNT(*) FROM drafts d WHERE d.created_at >= ? AND d.created_at < ?"
    if manual is not None:
        membership = "EXISTS" if manual else "NOT EXISTS"
        sql += " AND " + membership + " (SELECT 1 FROM topic_requests t WHERE t.event_id=d.event_id)"
        # Revisions do not consume another commissioned topic slot.
        if manual:
            sql += " AND NOT EXISTS (SELECT 1 FROM drafts prior WHERE prior.event_id=d.event_id AND prior.id<d.id)"
    return int(db.scalar(sql, (to_iso(start), to_iso(end))) or 0)


def active_topic_count(db: Database) -> int:
    # 统计“还没结束”的草稿数量。SQL 的 IN 占位符按状态数量动态生成，
    # 参数用元组传入，避免把状态字符串直接拼进 SQL。
    placeholders = ",".join("?" for _ in TERMINAL_DRAFT_STATUSES)
    return int(
        db.scalar(
            f"SELECT COUNT(*) FROM drafts WHERE status NOT IN ({placeholders})",
            tuple(TERMINAL_DRAFT_STATUSES),
        )
        or 0
    )


def pending_approval_count(db: Database) -> int:
    # 待审批数量。太多会让用户被消息淹没，所以有限额。
    return int(db.scalar("SELECT COUNT(*) FROM approvals WHERE status='pending'") or 0)


def _ensure_generation_allowed(db: Database, config: Config, clock: Clock) -> None:
    # 两道总闸：
    #   1. 全局暂停开关（app.pause_all / 环境变量），用于紧急停止；
    #   2. 预算模块的当天暂停状态。
    # 这里只读 snapshot，不会触发新的预算检查。
    if bool(config.get("app.pause_all", False)):
        raise DraftError("global pause is on (app.pause_all or BOT_PAUSE_ALL)")
    from ..budget import BudgetGuard

    snapshot = BudgetGuard(db, config, clock).snapshot()
    if snapshot.paused:
        raise DraftError(f"generation is paused: {snapshot.pause_reason or 'budget exhausted'}")


def _research_mode_for(db: Database, *, topic_request_id, event_id: int) -> str:
    if topic_request_id is not None:
        row = db.fetchone('SELECT research_mode FROM topic_requests WHERE id=?', (int(topic_request_id),))
        if row is not None and row['research_mode']:
            return str(row['research_mode'])
    row = db.fetchone(
        'SELECT research_mode FROM topic_requests WHERE event_id=? ORDER BY id DESC LIMIT 1',
        (int(event_id),),
    )
    if row is not None and row['research_mode']:
        return str(row['research_mode'])
    return 'news'


def _schedule(config: Config, clock: Clock) -> tuple[str, str]:
    # 计算“计划发布时间”和“审批过期时间”。
    #   - 演示模式：立刻“发布”，方便一口气看完整流程；
    #   - 正常模式：下一个本地发布时段（默认晚上 20:00）。
    # 过期时间取“审批 TTL”和“计划时间 + 2 小时”中较晚的那个，
    # 保证审批至少能覆盖到计划发布之后。
    now = clock.now()
    # ``publish_immediately`` means "as soon as the owner approves", not
    # "publish before approval".  The approval gate remains mandatory; this
    # only removes the artificial wait for the next evening slot.
    if config.demo_pipeline or bool(config.get("limits.publish_immediately", False)):
        scheduled = now
    else:
        scheduled = next_publish_slot(
            now,
            config.timezone_name,
            int(config.get("limits.publish_hour_local", 20)),
            int(config.get("limits.publish_minute_local", 0)),
        )
    ttl = timedelta(hours=float(config.get("limits.approval_ttl_hours", 24)))
    expires = max(now + ttl, scheduled + timedelta(hours=2))
    return to_iso(scheduled), to_iso(expires)


def _insert_draft(
    db: Database,
    config: Config,
    clock: Clock,
    *,
    event: sqlite3.Row,
    content: DraftContent,
    status: str,
    scheduled_at: str,
    expires_at: str,
    revision_feedback: str | None = None,
    topic_request_id: int | None = None,
    research_mode: str = 'news',
    submission_key: str | None = None,
    submission_hash: str | None = None,
    body_text: str | None = None,
) -> sqlite3.Row:
    # 插入一版草稿。版本号 = 同一事件当前最大版本 + 1，
    # 配合 UNIQUE(event_id, version) 保证版本不重复。
    # body_sha256 是正文指纹；后续审批单会绑定它。
    now = to_iso(clock.now())
    account = config.account()
    with db.tx() as conn:
        version = int(
            conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM drafts WHERE event_id=?", (event["id"],)
            ).fetchone()[0]
        ) + 1
        body = str(body_text) if body_text is not None else content.render_body()
        cur = conn.execute(
            "INSERT INTO drafts(event_id, version, status, title, body, body_json, body_sha256, "
            "platform, account_id, scheduled_at, expires_at, created_at, updated_at, revision_feedback, "
            "topic_request_id, research_mode, submission_key, submission_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event["id"],
                version,
                status,
                content.title,
                body,
                json_dumps(content.as_dict()),
                sha256_text(body),
                str(account.get("platform", "xiaohongshu")),
                str(account.get("id", "xhs-main")),
                scheduled_at,
                expires_at,
                now,
                now,
                revision_feedback,
                topic_request_id,
                str(research_mode or 'news'),
                submission_key,
                submission_hash,
            ),
        )
        return conn.execute("SELECT * FROM drafts WHERE id=?", (cur.lastrowid,)).fetchone()


def _render_and_store(
    db: Database, config: Config, clock: Clock, draft: sqlite3.Row, content: DraftContent
) -> list[RenderedImage]:
    # 渲染并保存图片记录。每张图一条 draft_images；再算总哈希写回 drafts。
    # 输出目录按草稿 ID 分开，避免不同草稿的同名文件互相覆盖。
    out_dir = config.resolve_path(str(config.get("render.output_dir", "var/drafts"))) / f"draft-{draft['id']}"
    try:
        rendered = render_draft(content, out_dir=out_dir, config=config, draft_key=f"draft-{draft['id']}")
    except Exception as exc:
        # 渲染失败就把草稿标成 failed，避免它停留在 drafted 状态
        # 被后续流程误认为“可以审批”。
        with db.tx() as conn:
            conn.execute(
                "UPDATE drafts SET status='failed', updated_at=? WHERE id=?",
                (to_iso(clock.now()), draft["id"]),
            )
        raise DraftError(f"rendering failed: {type(exc).__name__}: {exc}") from exc
    if content.storyboard is not None:
        expected = len(content.storyboard.all_cards())
        if len(rendered) != expected:
            with db.tx() as conn:
                conn.execute(
                    "UPDATE drafts SET status='failed', updated_at=? WHERE id=?",
                    (to_iso(clock.now()), draft["id"]),
                )
            raise DraftError(
                f"rendered {len(rendered)} images for a {expected}-card storyboard"
            )
    with db.tx() as conn:
        for image in rendered:
            # INSERT OR REPLACE：重新渲染同一草稿时覆盖旧记录，
            # 不会留下两套互相矛盾的图片哈希。
            conn.execute(
                "INSERT OR REPLACE INTO draft_images(draft_id, ordinal, path, sha256, width, height, kind, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'card', ?)",
                (
                    draft["id"],
                    image.ordinal,
                    str(image.path),
                    image.sha256,
                    image.width,
                    image.height,
                    to_iso(clock.now()),
                ),
            )
        aggregate = images_digest([image.as_dict() for image in rendered])
        conn.execute(
            "UPDATE drafts SET images_sha256=?, updated_at=? WHERE id=?",
            (aggregate, to_iso(clock.now()), draft["id"]),
        )
        conn.execute(
            "UPDATE drafts SET body_sha256=? WHERE id=?",
            (sha256_text(str(draft["body"])), draft["id"]),
        )
    return rendered


def _persist_artifact_images(
    db: Database,
    config: Config,
    clock: Clock,
    draft: sqlite3.Row,
    images: list[dict[str, Any]],
) -> list[RenderedImage]:
    """Register an artifact's final immutable image manifest.

    Preserved files are copied into this draft's directory and their content
    hash is re-checked.  No renderer is called here, so submit cannot silently
    replace a preserved asset with a freshly rendered one.
    """
    if not images:
        raise DraftError("artifact has no final images")
    out_dir = config.resolve_path(str(config.get("render.output_dir", "var/drafts"))) / f"draft-{draft['id']}"
    out_dir.mkdir(parents=True, exist_ok=True)
    rendered: list[RenderedImage] = []
    ordinals: set[int] = set()
    for index, image in enumerate(images, 1):
        source = Path(str(image.get("path") or ""))
        if not source.is_file():
            raise DraftError(f"final image is missing: {source}")
        ordinal = int(image.get("ordinal") or index)
        if ordinal in ordinals:
            raise DraftError(f"duplicate final image ordinal: {ordinal}")
        ordinals.add(ordinal)
        asset_id = str(image.get("asset_id") or f"image:{ordinal}").replace(":", "-")
        target = out_dir / f"{ordinal:02d}-{asset_id}-{source.name}"
        if source.resolve() != target.resolve():
            import shutil

            shutil.copy2(source, target)
        digest = sha256_file(target)
        expected = str(image.get("sha256") or "")
        if expected and digest != expected:
            raise DraftError(
                f"final image {target} hash changed: expected {expected[:12]}, got {digest[:12]}"
            )
        # Always decode the copied file.  A nonzero width/height field is not
        # evidence that the bytes are a valid image.
        try:
            with Image.open(target) as im:
                width, height = int(im.width), int(im.height)
                im.verify()
        except Exception as exc:
            raise DraftError(f"final image is not decodable: {target}: {exc}") from exc
        if width <= 0 or height <= 0:
            raise DraftError(f"final image has invalid dimensions: {target}")
        rendered.append(
            RenderedImage(
                ordinal=ordinal,
                path=target,
                sha256=digest,
                width=width,
                height=height,
                kind=str(image.get("kind") or "card"),
            )
        )
    with db.tx() as conn:
        conn.execute("DELETE FROM draft_images WHERE draft_id=?", (int(draft["id"]),))
        for image in rendered:
            conn.execute(
                "INSERT INTO draft_images(draft_id, ordinal, path, sha256, width, height, kind, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(draft["id"]), image.ordinal, str(image.path), image.sha256,
                    image.width, image.height, image.kind, to_iso(clock.now()),
                ),
            )
        aggregate = images_digest([image.as_dict() for image in rendered])
        conn.execute(
            "UPDATE drafts SET images_sha256=?, updated_at=? WHERE id=?",
            (aggregate, to_iso(clock.now()), int(draft["id"])),
        )
    return rendered


def _finalize_with_approval(
    db: Database,
    config: Config,
    clock: Clock,
    *,
    draft_id: int,
    event_id: int,
    rendered: list[RenderedImage],
    revision_feedback: str | None = None,
    supersede_approval_id: int | None = None,
    supersede_draft_id: int | None = None,
    carry_topic_request_id: int | None = None,
    actor: str = "system",
    change_report: dict[str, Any] | None = None,
    guard: Any | None = None,
) -> dict[str, Any]:
    now = to_iso(clock.now())
    with db.tx() as conn:
        # 下面这一整段在同一个事务里完成：
        #   创建审批单 -> 草稿改 review_pending -> 事件改 review_pending
        #   -> 排队通知 -> 写审计。
        # 任何一步失败都会整体回滚，不会出现“有审批单但草稿状态没变”
        # 这种半截状态。
        draft = conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if draft is None:
            raise DraftError("draft disappeared before approval")
        if draft["images_sha256"] is None:
            raise DraftError("draft has no rendered image hash")
        if guard is not None:
            # The final cancellation/generation/version check must run inside
            # this transaction, immediately before an approval is created.
            guard(conn, draft)
        if supersede_approval_id is not None:
            # Old approval and draft become immutable history in the *same*
            # transaction that creates the replacement artifact.
            cur = conn.execute(
                "UPDATE approvals SET status='superseded', decided_at=?, decided_by=?, decision_comment=? "
                "WHERE id=? AND status IN ('pending','revision_pending')",
                (
                    now,
                    actor,
                    f"superseded by revision: {revision_feedback or ''}",
                    int(supersede_approval_id),
                ),
            )
            if cur.rowcount != 1:
                current = conn.execute(
                    "SELECT status FROM approvals WHERE id=?", (int(supersede_approval_id),)
                ).fetchone()
                raise DraftError(
                    f"approval is {current['status'] if current is not None else 'missing'}, "
                    "not revisable"
                )
            if supersede_draft_id is not None:
                if carry_topic_request_id is not None:
                    conn.execute(
                        "UPDATE drafts SET status='superseded', topic_request_id=NULL, updated_at=? "
                        "WHERE id=?",
                        (now, int(supersede_draft_id)),
                    )
                else:
                    conn.execute(
                        "UPDATE drafts SET status='superseded', updated_at=? WHERE id=?",
                        (now, int(supersede_draft_id)),
                    )
            conn.execute(
                "UPDATE notifications SET state='cancelled' WHERE dedupe_key=? AND state IN ('pending','retry')",
                (f"approval:{int(supersede_approval_id)}",),
            )
        approval = approvals.make_approval(
            db,
            config,
            clock,
            draft=draft,
            images=[image.as_dict() for image in rendered],
            scheduled_at=str(draft["scheduled_at"]),
            expires_at=str(draft["expires_at"]),
            conn=conn,
        )
        if carry_topic_request_id is not None:
            conn.execute(
                "UPDATE drafts SET topic_request_id=? WHERE id=?",
                (int(carry_topic_request_id), draft_id),
            )
            conn.execute(
                "UPDATE topic_requests SET draft_id=?, approval_id=?, result_code=?, event_id=?, "
                "progress=?, error=NULL, updated_at=? WHERE id=?",
                (
                    draft_id,
                    int(approval["id"]),
                    str(approval["code"]),
                    event_id,
                    "修订已接受，等待重新审批",
                    now,
                    int(carry_topic_request_id),
                ),
            )
        conn.execute(
            "UPDATE drafts SET status='review_pending', revision_feedback=?, updated_at=? WHERE id=?",
            (revision_feedback, now, draft_id),
        )
        conn.execute(
            "UPDATE events SET status='review_pending' WHERE id=? AND status NOT IN ('rejected','published')",
            (event_id,),
        )
        notifications.queue_approval(db, config, clock, approval, conn=conn)
        audit(
            conn,
            "writer",
            "draft.created",
            entity="draft",
            entity_id=draft_id,
            detail={
                "approval_code": approval["code"],
                "approval_id": approval["id"],
                "version": draft["version"],
                "scheduled_at": draft["scheduled_at"],
                "images_sha256": draft["images_sha256"],
                "superseded_approval_id": supersede_approval_id,
                "topic_request_id": carry_topic_request_id,
                "change_summary": change_summary_lines(change_report) if change_report else [],
            },
            at=now,
        )
    fresh = db.fetchone("SELECT * FROM drafts WHERE id=?", (draft_id,))
    return {
        "draft": dict(fresh or draft),
        "approval": dict(approval),
        "images": [image.as_dict() for image in rendered],
        "approval_id": int(approval["id"]),
        "approval_code": approval["code"],
        "change_report": change_report,
        "change_lines": change_summary_lines(change_report) if change_report else [],
    }


def _draft_result(db: Database, draft: sqlite3.Row, approval: sqlite3.Row | None) -> dict[str, Any]:
    images = [
        dict(row)
        for row in db.fetchall(
            "SELECT * FROM draft_images WHERE draft_id=? ORDER BY ordinal", (draft["id"],)
        )
    ]
    return {
        "draft": dict(draft),
        "approval": dict(approval) if approval is not None else None,
        "images": images,
        "approval_id": int(approval["id"]) if approval is not None else None,
        "approval_code": approval["code"] if approval is not None else None,
    }


def _stored_rendered_images(db: Database, draft: sqlite3.Row) -> list[RenderedImage] | None:
    """Reconstruct already-rendered images when their bytes still match.

    Recovery after a crash between render and approval must not re-render (and
    therefore never changes the files the approval will bind) when the stored
    PNGs are still present and valid.
    """
    if not draft["images_sha256"]:
        return None
    rows = db.fetchall(
        "SELECT ordinal, path, sha256, width, height FROM draft_images "
        "WHERE draft_id=? ORDER BY ordinal",
        (draft["id"],),
    )
    if not rows:
        return None
    rendered: list[RenderedImage] = []
    for row in rows:
        path = Path(str(row["path"]))
        try:
            if not path.is_file() or sha256_file(path) != str(row["sha256"]):
                return None
        except OSError:
            return None
        rendered.append(
            RenderedImage(
                ordinal=int(row["ordinal"]),
                path=path,
                sha256=str(row["sha256"]),
                width=int(row["width"]),
                height=int(row["height"]),
            )
        )
    if images_digest([image.as_dict() for image in rendered]) != str(draft["images_sha256"]):
        return None
    return rendered


def _resume_topic_draft(
    db: Database,
    config: Config,
    clock: Clock,
    draft: sqlite3.Row,
) -> dict[str, Any]:
    """Finish or return an already-created commissioned draft.

    This is the crash/retry recovery path.  It must never call the model again
    and must never insert a second draft for the same topic request.  If the
    crash happened after a successful render, reuse those exact PNGs; only an
    incomplete render is repeated locally.
    """
    approval = db.fetchone(
        "SELECT * FROM approvals WHERE draft_id=? ORDER BY id DESC LIMIT 1", (draft["id"],)
    )
    if approval is not None:
        return _draft_result(db, draft, approval)
    if str(draft["status"]) == "failed":
        raise DraftError("this topic request's draft previously failed during rendering")
    if str(draft["status"]) != "drafted":
        raise DraftError(f"topic draft is {draft['status']}, not resumable")
    try:
        content = DraftContent.from_dict(json_loads(draft["body_json"], {}) or {})
    except Exception as exc:
        raise DraftError(f"stored topic draft is not readable: {exc}") from exc
    rendered = _stored_rendered_images(db, draft)
    if rendered is None:
        rendered = _render_and_store(db, config, clock, draft, content)
    return _finalize_with_approval(
        db,
        config,
        clock,
        draft_id=int(draft["id"]),
        event_id=int(draft["event_id"]),
        rendered=rendered,
    )


def _review_candidate(
    db: Database,
    config: Config,
    clock: Clock,
    llm,
    *,
    event: sqlite3.Row,
    evidence: list[dict[str, Any]],
    content: DraftContent,
) -> dict[str, Any] | None:
    """Run one bounded editorial review.

    The critic is a quality layer, not a security boundary.  If the reviewer
    itself is unavailable, the draft still goes to the human approval gate;
    deterministic content/security validation has already run.  A malformed
    reviewer response is different: once the critic is enabled it must revise
    or fail, never silently become a pass.
    """

    payload = build_critic_payload(event=event, evidence=evidence, content=content, config=config)
    try:
        response = llm.complete_json(
            purpose="draft_review",
            payload=payload,
            max_output_tokens=int(config.get("agent.critic_max_output_tokens", 900)),
        )
    except BudgetExceeded:
        raise
    except LLMError as exc:
        with db.tx() as conn:
            audit(
                conn,
                "critic",
                "draft.critic_unavailable",
                entity="event",
                entity_id=event["id"],
                detail={"error": str(exc)[:300]},
                at=to_iso(clock.now()),
            )
        return None
    review = parse_critic_review(
        response.data,
        max_problems=int(config.get("agent.max_critic_problems", 5)),
    )
    with db.tx() as conn:
        audit(
            conn,
            "critic",
            "draft.critic_review",
            entity="event",
            entity_id=event["id"],
            detail={
                "verdict": review["verdict"],
                "summary": review["summary"],
                "problems": review["problems"],
            },
            at=to_iso(clock.now()),
        )
    return review


def _critic_loop(
    db: Database,
    config: Config,
    clock: Clock,
    llm,
    *,
    event: sqlite3.Row,
    evidence: list[dict[str, Any]],
    content: DraftContent,
) -> DraftContent:
    """Return either the initial draft or one bounded revision accepted by the critic."""

    if not bool(config.get("agent.critic_enabled", False)):
        return content
    review = _review_candidate(
        db, config, clock, llm, event=event, evidence=evidence, content=content
    )
    if review is None:
        return content
    if review["verdict"] == "reject":
        raise DraftError("前审稿拒绝该稿：" + "; ".join(
            problem["instruction"] for problem in review["problems"]
        ))
    revisions = 0
    max_revisions = max(0, int(config.get("agent.max_critic_revisions", 1)))
    while review is not None and review["verdict"] == "revise":
        if revisions >= max_revisions:
            raise DraftError(
                "审稿要求修改，但已达到最大修订轮数："
                + "; ".join(problem["instruction"] for problem in review["problems"])
            )
        revisions += 1
        revision_payload = build_writer_payload(
            db,
            config,
            event,
            evidence,
            clock=clock,
            instruction=critic_instruction(review),
            current_draft=content.as_dict(),
        )
        try:
            revision = llm.complete_json(
                purpose="revise",
                payload=revision_payload,
                max_output_tokens=int(config.get("budget.max_output_tokens", 1800)),
            )
        except BudgetExceeded:
            raise
        except LLMError as exc:
            raise DraftError(f"critic revision failed: {type(exc).__name__}: {exc}") from exc
        content = validate_draft_payload(
            revision.data,
            evidence_by_id={str(item["id"]): item for item in evidence},
            config=config,
            date_unknown=date_is_unknown(evidence),
            generator=revision.provider or "unknown",
            model=revision.model or "",
            require_storyboard=True,
        )
        with db.tx() as conn:
            audit(
                conn,
                "critic",
                "draft.critic_revised",
                entity="event",
                entity_id=event["id"],
                detail={
                    "revision": revisions,
                    "problems": review["problems"],
                },
                at=to_iso(clock.now()),
            )
        review = _review_candidate(
            db, config, clock, llm, event=event, evidence=evidence, content=content
        )
        if review is not None and review["verdict"] == "reject":
            raise DraftError("修订后审稿仍拒绝：" + "; ".join(
                problem["instruction"] for problem in review["problems"]
            ))
    return content


def create_draft(
    db: Database,
    config: Config,
    clock: Clock,
    llm,
    *,
    event_id: int | None = None,
    instruction: str | None = None,
    topic_request_id: int | None = None,
) -> dict[str, Any]:
    # 写稿主入口。先做限额检查，再选事件、取证据、调模型、校验、
    # 入库、渲染、生成审批。任何一步失败都抛 DraftError（BudgetExceeded 除外）。
    if topic_request_id is not None:
        existing = db.fetchone(
            "SELECT * FROM drafts WHERE topic_request_id=? ORDER BY id DESC LIMIT 1",
            (int(topic_request_id),),
        )
        if existing is not None:
            # Retry/restart after a crash: reuse the linked draft and finish any
            # missing render/approval step without another paid model call.
            return _resume_topic_draft(db, config, clock, existing)
    _ensure_generation_allowed(db, config, clock)
    manual = topic_request_id is not None
    quota_key = "limits.max_manual_drafts_per_day" if manual else "limits.max_drafts_per_day"
    if drafts_created_today(db, config, clock, manual=manual) >= int(config.get(quota_key, 1)):
        raise DraftError("daily draft limit reached")
    if pending_approval_count(db) >= int(config.get("limits.max_pending_approvals", 3)):
        raise DraftError("pending approval limit reached")
    # A manual topic bypasses the *automatic* queue cap, but not the daily
    # draft, pending-approval, evidence, age, approval or budget constraints.
    if topic_request_id is None and active_topic_count(db) >= int(
        config.get("limits.max_queued_topics", 3)
    ):
        raise DraftError("queued topic limit reached")

    event = (
        db.fetchone("SELECT * FROM events WHERE id=?", (int(event_id),))
        if event_id is not None
        else next_researched_event(db)
    )
    if event is None:
        raise DraftError("no researched event is waiting for a draft")
    if event["status"] in {"rejected", "published", "expired"}:
        raise DraftError(f"event status {event['status']} is not draftable")

    evidence = research_event(db, int(event["id"]), clock)
    if not evidence:
        raise DraftError("event has no usable evidence")
    unknown_date = date_is_unknown(evidence)
    payload = build_writer_payload(db, config, event, evidence, clock=clock, instruction=instruction)

    try:
        response = llm.complete_json(
            purpose="draft",
            payload=payload,
            max_output_tokens=int(config.get("budget.max_output_tokens", 1800)),
        )
    except BudgetExceeded:
        # 预算不足是“保护机制生效”，要原样往上抛，让 worker 暂停生成，
        # 不要包装成普通的 DraftError 被悄悄跳过。
        raise
    except LLMError as exc:
        # 模型调用失败：写审计，然后转成 DraftError。
        with db.tx() as conn:
            audit(
                conn,
                "writer",
                "draft.llm_failed",
                entity="event",
                entity_id=event["id"],
                detail={"error": str(exc)},
                at=to_iso(clock.now()),
            )
        raise DraftError(f"writing failed: {type(exc).__name__}: {exc}") from exc

    evidence_map = {str(item["id"]): item for item in evidence}
    try:
        # 注意：这里传的是“模型输出的 JSON”，不是已信任的对象。
        # validate_draft_payload 会重新检查结构、证据引用、数字和禁用词。
        content = validate_draft_payload(
            response.data,
            evidence_by_id=evidence_map,
            config=config,
            date_unknown=unknown_date,
            generator=response.provider or "unknown",
            model=response.model or "",
            require_storyboard=True,
        )
    except Exception as exc:
        with db.tx() as conn:
            audit(
                conn,
                "writer",
                "draft.validation_failed",
                entity="event",
                entity_id=event["id"],
                detail={"error": str(exc)},
                at=to_iso(clock.now()),
            )
        raise DraftError(f"generated draft failed validation: {exc}") from exc

    try:
        content = _critic_loop(
            db,
            config,
            clock,
            llm,
            event=event,
            evidence=evidence,
            content=content,
        )
    except DraftError:
        raise
    except Exception as exc:
        with db.tx() as conn:
            audit(
                conn,
                "critic",
                "draft.critic_failed",
                entity="event",
                entity_id=event["id"],
                detail={"error": str(exc)[:300]},
                at=to_iso(clock.now()),
            )
        raise DraftError(f"critic/strict revision failed: {exc}") from exc

    scheduled_at, expires_at = _schedule(config, clock)
    research_mode = _research_mode_for(db, topic_request_id=topic_request_id, event_id=int(event['id']))
    # 先以 drafted 状态入库，渲染成功后再推进到 review_pending。
    # 这样即使渲染中途崩溃，数据库里也能看到一篇“未完成”的草稿。
    draft = _insert_draft(
        db,
        config,
        clock,
        event=event,
        content=content,
        status="drafted",
        scheduled_at=scheduled_at,
        expires_at=expires_at,
        topic_request_id=topic_request_id,
        research_mode=research_mode,
    )
    rendered = _render_and_store(db, config, clock, draft, content)
    return _finalize_with_approval(
        db,
        config,
        clock,
        draft_id=int(draft["id"]),
        event_id=int(event["id"]),
        rendered=rendered,
    )


def revise_draft(
    db: Database,
    config: Config,
    clock: Clock,
    llm,
    *,
    approval_id: int,
    instruction: str,
    actor: str = "owner",
) -> dict[str, Any]:
    # 修订流程：
    #   1. 只允许修改 pending/revision_pending 状态的审批；
    #   2. 先验证原审批的正文/图片哈希仍然匹配（防止内容被偷偷改过）；
    #   3. 用原稿 + 用户指令再写一版，走完整校验；
    #   4. 新版本入库；旧审批标 superseded、旧草稿标 superseded；
    #   5. 取消还没发出的旧通知，再给新版本建新审批。
    # 结果：用户必须重新批准新版本。
    _ensure_generation_allowed(db, config, clock)
    approval = approvals.get_approval(db, approval_id=approval_id)
    if approval is None:
        raise DraftError("approval not found")
    # The inbound callback atomically moves pending/approved to
    # revision_pending before queueing this job.  Older direct callers may
    # still pass a pending approval; anything else is not revisable.
    if approval["status"] not in {"pending", "revision_pending"}:
        raise DraftError(f"approval is {approval['status']}, not revisable")
    ok, reason, _ = approvals.verify_approval_artifact(db, approval)
    if not ok:
        raise DraftError(f"approval artifact no longer matches: {reason}")
    draft = db.fetchone("SELECT * FROM drafts WHERE id=?", (approval["draft_id"],))
    if draft is None:
        raise DraftError("draft not found")
    event = db.fetchone("SELECT * FROM events WHERE id=?", (draft["event_id"],))
    evidence = research_event(db, int(draft["event_id"]), clock)
    if event is None or not evidence:
        raise DraftError("event/evidence no longer available")
    try:
        current = DraftContent.from_dict(json_loads(draft["body_json"], {}) or {})
    except Exception as exc:
        raise DraftError(f"stored draft is not readable: {exc}") from exc
    request = infer_revision_request(instruction, current)
    payload = build_writer_payload(
        db,
        config,
        event,
        evidence,
        clock=clock,
        instruction=instruction,
        current_draft=current.as_dict(),
        revision_request=request.as_dict(),
    )
    response = llm.complete_json(
        purpose="revise",
        payload=payload,
        max_output_tokens=int(config.get("budget.max_output_tokens", 1800)),
    )
    content = validate_draft_payload(
        response.data,
        evidence_by_id={str(item["id"]): item for item in evidence},
        config=config,
        date_unknown=date_is_unknown(evidence),
        generator=response.provider or "unknown",
        model=response.model or "",
        require_storyboard=True,
    )

    # Acceptance is independent of the writer.  Deterministic checks run first;
    # only an artifact-changing result reaches the single bounded review call.
    deterministic = deterministic_revision_check(current, content, request, config)
    report: dict[str, Any] = dict(deterministic)
    if not deterministic["accepted"]:
        raise RevisionUnsatisfied(
            "revision did not satisfy the requested artifact changes: "
            + "; ".join(deterministic["issues"]),
            report,
        )
    review_response = llm.complete_json(
        purpose="review_revision",
        payload=semantic_review_payload(current, content, request, deterministic),
        max_output_tokens=int(config.get("revision.review_max_output_tokens", 700)),
    )
    review = parse_semantic_review(
        review_response.data,
        max_problems=int(config.get("revision.max_review_problems", 5)),
    )
    report["semantic_review"] = review
    if not review["satisfied"]:
        raise RevisionUnsatisfied(
            "independent review did not accept the revision: "
            + "; ".join(review["problems"] or ["no verified change"]),
            report,
        )

    now = to_iso(clock.now())
    scheduled_at, expires_at = _schedule(config, clock)
    research_mode = str(draft['research_mode'] or _research_mode_for(db, topic_request_id=draft['topic_request_id'], event_id=int(draft['event_id'])))
    new_draft = _insert_draft(
        db,
        config,
        clock,
        event=event,
        content=content,
        status="drafted",
        scheduled_at=scheduled_at,
        expires_at=expires_at,
        revision_feedback=instruction,
        research_mode=research_mode,
    )
    rendered = _render_and_store(db, config, clock, new_draft, content)
    topic_request_id = (
        int(draft["topic_request_id"]) if draft["topic_request_id"] is not None else None
    )
    result = _finalize_with_approval(
        db,
        config,
        clock,
        draft_id=int(new_draft["id"]),
        event_id=int(event["id"]),
        rendered=rendered,
        revision_feedback=instruction,
        supersede_approval_id=int(approval["id"]),
        supersede_draft_id=int(approval["draft_id"]),
        carry_topic_request_id=topic_request_id,
        actor=actor,
        change_report=report,
    )
    with db.tx() as conn:
        audit(
            conn,
            actor,
            "draft.revised",
            entity="draft",
            entity_id=new_draft["id"],
            detail={
                "from_draft": approval["draft_id"],
                "old_approval": approval["code"],
                "instruction": instruction,
                "new_approval": result["approval"]["code"],
                "requested_changes": request.as_dict(),
                "change_summary": result["change_lines"],
                "semantic_review": review,
            },
            at=now,
        )
    return result


def draft_view(db: Database, draft_id: int) -> dict[str, Any] | None:
    # 组装控制台展示用的完整视图：草稿 + 结构化内容 + 图片列表 + 最近审批。
    # 图片按 ordinal 排序，保证展示顺序和发布顺序一致。
    draft = db.fetchone("SELECT * FROM drafts WHERE id=?", (int(draft_id),))
    if draft is None:
        return None
    view = dict(draft)
    view["content"] = json_loads(draft["body_json"], {}) or {}
    view["images"] = [dict(row) for row in db.fetchall(
        "SELECT * FROM draft_images WHERE draft_id=? ORDER BY ordinal", (int(draft_id),)
    )]
    approval = db.fetchone(
        "SELECT * FROM approvals WHERE draft_id=? ORDER BY id DESC LIMIT 1", (int(draft_id),)
    )
    view["approval"] = dict(approval) if approval is not None else None
    return view


"""Post metrics that never invent numbers.

Xiaohongshu data is only available through a connected account.  When it is not
available the checkpoint is stored with an explicit ``missing`` list; callers
must not render a missing metric as zero.  Nothing here changes the persona or
publishing policy from a single post.
"""

# =============================================================================
# 中文导读：发布后的数据记录——不知道就是不知道
# =============================================================================
# 这个模块记录帖子发布 24 小时和 72 小时后的表现（浏览、点赞、评论、
# 收藏、转发）。它最重要的一条规则是：绝不编数字。
#
# 为什么？因为“没有数据”和“数据是 0”是两回事：
#   - 数据是 0：帖子发了，确实没人看；
#   - 没有数据：根本没连平台，或者平台没返回。
# 如果后者也写成 0，运营者会误判内容质量，甚至据此改变选题方向。
#
# 所以每条快照都带一个 missing 列表，明确写出“哪些指标没拿到”。
# 调用方展示时必须显示“暂无数据”，不能把缺失当 0。
#
# 另外：单条帖子的数据不驱动人设或发布策略的改变；
# 这里只做记录，不做自动决策。
# =============================================================================

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

from ..db import Database, json_dumps, json_loads
from ..timeutil import Clock, parse_iso, to_iso

CHECKPOINTS = (24, 72)
# 只允许这五个指标。白名单之外的名字一律报错，
# 防止平台改字段名后程序悄悄记下一堆没用的键。
METRIC_KEYS = ("views", "likes", "comments", "collects", "shares")
CHECKPOINT_LABELS = {24: "24h", 72: "72h"}


class MetricsError(ValueError):
    # 指标数据不合法（未知指标名、负数、非整数等）。
    pass


def _validate(metrics: dict[str, Any]) -> dict[str, int]:
    # 校验并清洗指标：
    #   - 键必须在白名单里；
    #   - None 表示“没拿到”，直接跳过（不会变成 0）；
    #   - 必须是真正的 int，且不能是 bool（Python 里 True 也是 int，
    #     所以要单独排除），且不能为负。
    clean: dict[str, int] = {}
    for key, value in metrics.items():
        if key not in METRIC_KEYS:
            raise MetricsError(f"unknown metric: {key}")
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise MetricsError(f"metric {key} must be a non-negative integer")
        clean[key] = value
    return clean


def record_metrics(
    db: Database,
    *,
    draft_id: int,
    checkpoint: int,
    metrics: dict[str, Any],
    missing: list[str] | None = None,
    source: str = "platform",
    captured_at: str | None = None,
) -> dict[str, Any]:
    # 记录某个时间点（24h 或 72h）的快照。
    # missing 的计算方式：
    #   “调用方明确说缺的” 加上 “五个指标里这次没提供的”，
    # 去重后排序。这样即使调用方忘了写 missing，也不会漏报。
    if checkpoint not in CHECKPOINTS:
        raise MetricsError("checkpoint must be 24 or 72")
    clean = _validate(metrics)
    missing = sorted(set(missing or []) | (set(METRIC_KEYS) - set(clean)))
    with db.tx() as conn:
        # 每个 (draft_id, checkpoint) 只有一行：重复采集时覆盖旧值。
        # 用 UPSERT（ON CONFLICT DO UPDATE）而不是先删后插，
        # 这样原子、简单，也不会出现短暂的“没有记录”窗口。
        conn.execute(
            "INSERT INTO metrics_snapshots(draft_id,checkpoint,captured_at,metrics_json,missing_json,source) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(draft_id,checkpoint) DO UPDATE SET "
            "captured_at=excluded.captured_at, metrics_json=excluded.metrics_json, "
            "missing_json=excluded.missing_json, source=excluded.source",
            (
                draft_id,
                checkpoint,
                captured_at or to_iso(__import__("datetime").datetime.now().astimezone()),
                json_dumps(clean),
                json_dumps(missing),
                source,
            ),
        )
    return {"draft_id": draft_id, "checkpoint": checkpoint, "metrics": clean, "missing": missing, "source": source}


def record_unavailable(
    db: Database,
    *,
    draft_id: int,
    checkpoint: int,
    reason: str,
    captured_at: str | None = None,
) -> dict[str, Any]:
    # 记录“这次完全拿不到数据”。五个指标全部进 missing，
    # 原因写进 source，例如 unavailable: platform metrics not connected。
    return record_metrics(
        db,
        draft_id=draft_id,
        checkpoint=checkpoint,
        metrics={},
        missing=list(METRIC_KEYS),
        source=f"unavailable: {reason}"[:180],
        captured_at=captured_at,
    )


def due_checkpoints(published_at: str, now: str) -> list[int]:
    # 从发布时间算起，哪些检查点已经到点了。
    # 比如发布已满 30 小时，就返回 [24]；满 80 小时返回 [24, 72]。
    # 时间解析失败就返回空列表：宁可这次不记录，也不乱猜时间。
    start = parse_iso(published_at)
    at = parse_iso(now)
    if start is None or at is None:
        return []
    return [cp for cp in CHECKPOINTS if at >= start + timedelta(hours=cp)]


def capture_unavailable_metrics(db: Database, clock: Clock, *, limit: int = 20) -> int:
    """For published posts, record explicit missing metrics when checkpoints pass.

    A real collector can call :func:`record_metrics` later to replace these rows.
    Simulated exports are deliberately excluded.
    """
    # 中文：给已经真实发布的帖子，在到点时先写入一条“明确缺失”的快照。
    # 为什么要先写？因为“没采集”和“采集了但没有”必须能区分开。
    # 真正的采集器以后可以调用 record_metrics 覆盖这一行。
    # 注意只查 state='published' 的尝试：simulated（本地模拟导出）
    # 根本没有平台数据，不应该进入统计。
    now = to_iso(clock.now())
    rows = db.fetchall(
        "SELECT d.id, d.status, a.finished_at FROM drafts d "
        "JOIN publish_attempts a ON a.draft_id=d.id AND a.state='published' "
        "WHERE d.status='published' ORDER BY d.id DESC LIMIT ?",
        (limit,),
    )
    written = 0
    for row in rows:
        if not row["finished_at"]:
            continue
        for checkpoint in due_checkpoints(str(row["finished_at"]), now):
            exists = db.fetchone(
                "SELECT id FROM metrics_snapshots WHERE draft_id=? AND checkpoint=?",
                (row["id"], checkpoint),
            )
            if exists is not None:
                # 已经有快照（可能是真实采集到的数据），绝不覆盖。
                continue
            record_unavailable(
                db,
                draft_id=int(row["id"]),
                checkpoint=checkpoint,
                reason="platform metrics not connected or not returned",
                captured_at=now,
            )
            written += 1
    return written


def metrics_view(db: Database, draft_id: int) -> dict[str, Any]:
    # 只读视图：把某篇草稿的所有快照按检查点排序返回。
    # metrics 和 missing 分开返回，界面才能正确显示“0”和“暂无数据”。
    rows = db.fetchall(
        "SELECT * FROM metrics_snapshots WHERE draft_id=? ORDER BY checkpoint", (draft_id,)
    )
    return {
        "draft_id": draft_id,
        "checkpoints": [
            {
                "checkpoint": int(row["checkpoint"]),
                "label": CHECKPOINT_LABELS.get(int(row["checkpoint"]), str(row["checkpoint"])),
                "captured_at": row["captured_at"],
                "metrics": json_loads(row["metrics_json"], {}) or {},
                "missing": json_loads(row["missing_json"], []) or [],
                "source": row["source"],
            }
            for row in rows
        ],
    }

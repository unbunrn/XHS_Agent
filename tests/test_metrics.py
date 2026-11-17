"""Metrics checkpoints must say missing rather than invent zeroes."""

# =============================================================================
# 中文导读：数据不好拿时，写“缺失”，不要写 0
# =============================================================================
# “0 次浏览”和“还没拿到浏览数”是两件完全不同的事。
# 如果把拿不到的数据记成 0，之后做复盘就会得出完全错误的结论。
# 本文件验证：
#   - 到 24 小时只生成 24h 检查点，72h 还没到；
#   - 没有真实数据时，metrics 是空对象，missing 列出全部指标；
#   - 只填了两个指标时，missing 自动算出剩下三个；
#   - 负数指标直接报错；
#   - 本地模拟（simulated）没有真实帖子，因此根本不生成检查点。
# =============================================================================

from __future__ import annotations

import unittest
from datetime import timedelta

from bot.services.analytics import (
    METRIC_KEYS,
    MetricsError,
    capture_unavailable_metrics,
    due_checkpoints,
    metrics_view,
    record_metrics,
)
from bot.timeutil import to_iso
from tests.helpers import cleanup, make_draft, new_env


class MetricsTests(unittest.TestCase):
    def test_missing_metrics_are_explicit_not_zero(self) -> None:
        # 手工造一条“真实发布成功”的记录：30 小时前完成，
        # 因此只有 24h 检查点到期，72h 还没到（due_checkpoints 返回 [24]）。
        env = new_env("metrics")
        try:
            draft = make_draft(env)
            draft_id = draft["draft"]["id"]
            finished = to_iso(env.clock.now() - timedelta(hours=30))
            env.db.execute(
                "UPDATE drafts SET status='published' WHERE id=?", (draft_id,)
            )
            env.db.execute(
                "INSERT INTO publish_attempts(approval_id,draft_id,dedupe_key,platform,account_id,"
                "mode,state,external_post_id,external_url,finished_at,created_at,started_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    draft["approval_id"],
                    draft_id,
                    f"test:{draft_id}",
                    "xiaohongshu",
                    "account",
                    "real",
                    "published",
                    "post-1",
                    "https://example.com/post-1",
                    finished,
                    finished,
                    finished,
                    finished,
                ),
            )
            self.assertEqual(due_checkpoints(finished, to_iso(env.clock.now())), [24])
            # 没有接入真实数据源：写入一条“不可用”记录。
            # metrics 必须是 {}，missing 必须是全部五个指标，绝不能补 0。
            self.assertEqual(capture_unavailable_metrics(env.db, env.clock), 1)
            view = metrics_view(env.db, draft_id)
            self.assertEqual(len(view["checkpoints"]), 1)
            checkpoint = view["checkpoints"][0]
            self.assertEqual(checkpoint["checkpoint"], 24)
            self.assertEqual(checkpoint["metrics"], {})
            self.assertEqual(set(checkpoint["missing"]), set(METRIC_KEYS))
            self.assertIn("unavailable", checkpoint["source"])

            # 再过 50 小时（发布后 80 小时），72h 检查点也到期了。
            env.clock.advance(hours=50)
            self.assertEqual(capture_unavailable_metrics(env.db, env.clock), 1)
            labels = [c["checkpoint"] for c in metrics_view(env.db, draft_id)["checkpoints"]]
            self.assertEqual(labels, [24, 72])

            # 手工补录两个真实指标：missing 自动变成剩下三个。
            record_metrics(
                env.db,
                draft_id=draft_id,
                checkpoint=24,
                metrics={"views": 120, "likes": 7},
                captured_at=to_iso(env.clock.now()),
            )
            view = metrics_view(env.db, draft_id)
            first = view["checkpoints"][0]
            self.assertEqual(first["metrics"], {"views": 120, "likes": 7})
            self.assertEqual(set(first["missing"]), {"comments", "collects", "shares"})
            # 负数一定是调用方算错了，直接报 MetricsError，不静默接受。
            with self.assertRaises(MetricsError):
                record_metrics(
                    env.db,
                    draft_id=draft_id,
                    checkpoint=24,
                    metrics={"views": -1},
                )
        finally:
            cleanup(env)

    def test_simulated_exports_do_not_get_metrics_checkpoints(self) -> None:
        # simulated 只是本地导出，平台上没有帖子，
        # 所以不生成任何数据检查点（生成也只会是假数据）。
        env = new_env("metrics-sim")
        try:
            draft = make_draft(env)
            env.db.execute(
                "UPDATE drafts SET status='simulated' WHERE id=?", (draft["draft"]["id"],)
            )
            self.assertEqual(capture_unavailable_metrics(env.db, env.clock), 0)
            self.assertEqual(metrics_view(env.db, draft["draft"]["id"])["checkpoints"], [])
        finally:
            cleanup(env)


if __name__ == "__main__":
    unittest.main()

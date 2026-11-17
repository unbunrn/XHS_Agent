"""Worker end-to-end recovery and real-publisher guard tests."""

# =============================================================================
# 中文导读：worker 整条链路 + 重启恢复
# =============================================================================
# 这里模拟一个真实 worker 的一生：
#   startup -> tick 抓取/研究/写稿 -> 等待审批
#   -> 重启（审批不能丢、也不能提前发布）
#   -> 人工批准 -> tick 发布（mock 模式 = simulated，没有帖子 ID）
#   -> 再 tick 不能重复发布。
#
# 第二个场景更关键：如果进程在“已占坑、还没拿到结果”时挂掉，
# 重启只能把它标成 unknown（可能已经发出去了），绝不能自动重发。
#
# 最后验证真实发布器的三道门：未武装、缺 token、演示管线，都必须拒绝构建。
# =============================================================================

from __future__ import annotations

import copy
import os
import random
import unittest
from pathlib import Path
from unittest import mock

from bot.adapters.publishers import MockPublisher, build_publisher
from bot.adapters.types import NotConfigured
from bot.config import DEFAULTS, Config, Secrets
from bot.services import publish
from bot.worker import Worker
from tests.helpers import approve, cleanup, make_draft, new_env


class WorkerLoopTests(unittest.TestCase):
    def test_offline_loop_approval_publish_and_restart(self) -> None:
        # 离线模式跑完整条链路：一个 tick 就应该产出草稿 + 待审批记录，
        # 但此时绝不能有发布尝试。
        env = new_env("worker-e2e")
        try:
            worker = Worker(
                env.config,
                clock=env.clock,
                db=env.db,
                http=env.http,
                rng=random.Random(7),
            )
            info = worker.startup()
            self.assertEqual(info["mode"], "offline")
            # The old automatic collect/draft path is retired.  This test now
            # exercises the shared approval/publish/restart machinery with an
            # explicitly created draft, not an implicit collector.
            result = make_draft(env)
            draft = result["draft"]
            approval = result["approval"]
            self.assertIsNotNone(draft)
            self.assertEqual(draft["status"], "review_pending")
            self.assertEqual(approval["status"], "pending")
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 0)
            image_count = env.db.scalar(
                "SELECT COUNT(*) FROM draft_images WHERE draft_id=?", (draft["id"],)
            )
            self.assertGreaterEqual(image_count, 1)

            # A restart with a pending approval must not publish or lose it.
            # 中文：重启时审批还是 pending——既不能丢，也不能趁机发布。
            # 这是“审批必须绑定最终版本”的运行时体现。
            restarted = Worker(
                env.config,
                clock=env.clock,
                db=env.db,
                http=env.http,
                rng=random.Random(8),
            )
            restarted.startup()
            restarted.tick()
            self.assertEqual(
                env.db.scalar("SELECT status FROM approvals WHERE id=?", (approval["id"],)),
                "pending",
            )
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 0)

            approve(env, approval["code"])
            # 批准后跑两轮 tick：mock 发布器的结果是 simulated，
            # 没有真实帖子 ID/URL，审批和草稿状态也同步改成 simulated。
            for _ in range(2):
                restarted.tick()
            attempt = env.db.fetchone("SELECT * FROM publish_attempts")
            self.assertEqual(attempt["state"], "simulated")
            self.assertEqual(attempt["mode"], "mock")
            self.assertIsNone(attempt["external_post_id"])
            self.assertIsNone(attempt["external_url"])
            self.assertEqual(
                env.db.scalar("SELECT status FROM approvals WHERE id=?", (approval["id"],)),
                "simulated",
            )
            self.assertEqual(env.db.scalar("SELECT status FROM drafts WHERE id=?", (draft["id"],)), "simulated")
            # Extra ticks never create a second attempt.
            # 中文：再多跑几轮也只能有 1 条发布尝试（幂等键兜底）。
            restarted.tick()
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 1)
        finally:
            cleanup(env)

    def test_interrupted_publish_is_recovered_as_unknown_never_resent(self) -> None:
        # 模拟“占坑成功、但进程在调用发布器前后崩溃”：
        # 先手工 claim（数据库里留下 publishing 的尝试），然后新建 worker 启动。
        # startup 的恢复逻辑必须把它改成 unknown，并写明 restart；
        # 之后 tick 也不能再发一次。
        env = new_env("worker-unknown")
        try:
            draft = make_draft(env)
            approve(env, draft["approval_code"])
            approval = env.db.fetchone(
                "SELECT * FROM approvals WHERE id=?", (draft["approval_id"],)
            )
            claim, message = publish.claim_publish(
                env.db,
                env.config,
                env.clock,
                int(approval["id"]),
                MockPublisher(env.config, env.clock),
            )
            self.assertIsNotNone(claim, message)
            worker = Worker(
                env.config,
                clock=env.clock,
                db=env.db,
                http=env.http,
                rng=random.Random(9),
            )
            worker.startup()
            attempt = env.db.fetchone("SELECT * FROM publish_attempts")
            self.assertEqual(attempt["state"], "unknown")
            self.assertIn("restart", attempt["reconcile_note"] or "")
            self.assertEqual(
                env.db.scalar("SELECT status FROM approvals WHERE id=?", (approval["id"],)),
                "publish_unknown",
            )
            worker.tick()
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 1)
        finally:
            cleanup(env)


class RealPublisherGuardTests(unittest.TestCase):
    def _live_config(self, root: Path, **publisher) -> Config:
        # 构造一份“看起来已经可以真实发布”的 live 配置：
        # DeepSeek + 飞书 + 真实 RSS 来源 + 小红书发布器。
        # 各测试再逐项拆掉开关，验证每一道门都独立生效。
        data = copy.deepcopy(DEFAULTS)
        data["app"]["mode"] = "live"
        data["llm"]["provider"] = "deepseek"
        data["notify"]["provider"] = "lark"
        data["sources"] = [
            {
                "name": "News",
                "url": "https://example.com/feed.xml",
                "kind": "rss",
                "trust_tier": 2,
                "enabled": True,
            }
        ]
        data["publisher"]["provider"] = "xiaohongshu"
        data["publisher"]["live_enabled"] = True
        data["publisher"]["xiaohongshu"].update(publisher)
        return Config(data, root, secrets=Secrets())

    def test_unarmed_or_unconfigured_xhs_is_not_reported_connected(self) -> None:
        # 第一种：live_enabled=false -> 没武装，拒绝构建发布器。
        # 第二种：开关都开了、环境变量也给了，但没有 token -> 仍然拒绝。
        # 两种情况都不能返回一个“看起来能发”的发布器。
        root = Path.cwd() / "var"
        cfg = self._live_config(root)
        cfg.data["publisher"]["live_enabled"] = False
        with self.assertRaises(NotConfigured):
            build_publisher(cfg, None)
        cfg.data["publisher"]["live_enabled"] = True
        with mock.patch.dict(os.environ, {"BOT_ALLOW_REAL_PUBLISH": "1"}):
            self.assertFalse(cfg.secrets.xiaohongshu_token)
            with self.assertRaises(NotConfigured):
                build_publisher(cfg, None)

    def test_demo_pipeline_cannot_enable_real_publishing(self) -> None:
        # live 模式 + fake 模型 = 演示管线。
        # 即使有真实 token、发布开关也开了，也必须拒绝真实发布器。
        data = copy.deepcopy(DEFAULTS)
        data["app"]["mode"] = "live"
        data["llm"]["provider"] = "fake"
        data["publisher"]["provider"] = "xiaohongshu"
        data["publisher"]["live_enabled"] = True
        cfg = Config(data, Path.cwd() / "var", secrets=Secrets(xiaohongshu_token="x"))
        self.assertTrue(cfg.demo_pipeline)
        with self.assertRaises(NotConfigured):
            build_publisher(cfg, None)


if __name__ == "__main__":
    unittest.main()

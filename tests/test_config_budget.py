"""Configuration guards and conservative API budget tests."""

# =============================================================================
# 中文导读：配置守卫 + API 预算
# =============================================================================
# 配置守卫要回答的是：“这组配置有没有可能误发/误连？”
#   - live 模式 + 演示管线 + 真实发布 -> 直接判定为危险，不允许武装；
#   - 真实发布必须四道开关同时满足；
#   - live 里用 fake LLM/通知 -> 除非显式 allow_fake_in_live，否则拒绝；
#   - 控制台绑定 0.0.0.0 -> 配置校验失败；
#   - 环境变量覆盖必须落到正确的嵌套键。
#
# 预算部分要回答的是：“DeepSeek 的花费会不会失控？”
#   - 未知 token 用量要按预留金额记账，不能当 0；
#   - 预算/次数用完 -> 暂停生成，但审批和模拟发布仍然可用；
#   - 到了第二天自动重置。
# =============================================================================

from __future__ import annotations

import copy
import os
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from bot.adapters.llm import DeepSeekLLM, _RetryableHTTP
from bot.adapters.publishers import MockPublisher, Publisher
from bot.config import DEFAULTS, Config, Secrets, load_config
from bot.services import drafts as drafts_service
from bot.services import publish
from bot.services.approvals import get_approval
from bot.budget import BudgetGuard
from bot.adapters.types import BudgetExceeded, PublishOutcome
from tests.helpers import approve, cleanup, make_draft, new_env


def _config(root: Path, **overrides) -> Config:
    # 构造一份“默认 live 倾向”的配置：DeepSeek + 一个 RSS 来源。
    # overrides 按 section 合并，比如 app={...}、publisher={...}。
    data = copy.deepcopy(DEFAULTS)
    data["llm"]["provider"] = "deepseek"
    data["sources"] = [
        {
            "name": "News",
            "url": "https://example.com/feed.xml",
            "kind": "rss",
            "trust_tier": 2,
            "enabled": True,
        }
    ]
    for section, values in overrides.items():
        data.setdefault(section, {}).update(values)
    return Config(data, root, secrets=Secrets())


class ConfigGuardTests(unittest.TestCase):
    def test_live_demo_with_real_publish_enabled_is_fatal(self) -> None:
        # 最危险的组合：live 模式 + fake 模型（演示管线）+ 真实发布开关。
        # 必须识别为 demo_pipeline，runtime_guards 报出相关问题，
        # 并且 is_live_publish_armed() 为 False。
        cfg = _config(
            Path.cwd() / "var",
            app={"mode": "live", "allow_fake_in_live": True},
            llm={"provider": "fake"},
            publisher={"provider": "xiaohongshu", "live_enabled": True},
        )
        self.assertTrue(cfg.demo_pipeline)
        self.assertTrue(any("demo" in p for p in cfg.runtime_guards()))
        self.assertFalse(cfg.is_live_publish_armed())

    def test_live_publish_requires_all_switches(self) -> None:
        # 逐步验证四道开关里的环境变量那一项：
        # 没有 BOT_ALLOW_REAL_PUBLISH -> 未武装；
        # 有它但 live_enabled=false -> 仍然未武装。
        cfg = _config(
            Path.cwd() / "var",
            app={"mode": "live"},
            publisher={"provider": "xiaohongshu", "live_enabled": True},
        )
        self.assertFalse(cfg.demo_pipeline)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BOT_ALLOW_REAL_PUBLISH", None)
            self.assertFalse(cfg.is_live_publish_armed())
        with mock.patch.dict(os.environ, {"BOT_ALLOW_REAL_PUBLISH": "1"}):
            self.assertTrue(cfg.is_live_publish_armed())
        cfg.data["publisher"]["live_enabled"] = False
        with mock.patch.dict(os.environ, {"BOT_ALLOW_REAL_PUBLISH": "1"}):
            self.assertFalse(cfg.is_live_publish_armed())

    def test_fake_live_is_blocked_unless_explicitly_allowed(self) -> None:
        # live 模式里默认不允许 fake LLM / fake 通知；
        # 只有显式 allow_fake_in_live=true 才放行（用于演练）。
        cfg = _config(
            Path.cwd() / "var",
            app={"mode": "live", "allow_fake_in_live": False},
            llm={"provider": "fake"},
            notify={"provider": "fake"},
        )
        problems = cfg.runtime_guards()
        self.assertTrue(any("fake LLM" in p for p in problems))
        self.assertTrue(any("notification" in p for p in problems))
        cfg.data["app"]["allow_fake_in_live"] = True
        self.assertEqual(cfg.runtime_guards(), [])

    def test_owner_status_is_not_chat_id(self) -> None:
        # 回归测试：owner open_id（批准人）和 chat_id（群/会话）
        # 是两个不同的东西，不能拿 chat_id 冒充 owner。
        secrets = Secrets(lark_chat_id="chat-123", lark_owner_open_id=None)
        self.assertEqual(secrets.status()["lark_owner_open_id"], "missing")
        self.assertEqual(secrets.status()["lark_chat_id"], "set")

    def test_console_must_bind_loopback(self) -> None:
        # 控制台默认只允许回环地址；0.0.0.0 会让整个局域网都能访问，
        # 所以 validate() 必须直接报错。
        cfg = _config(Path.cwd() / "var", console={"host": "0.0.0.0"})
        with self.assertRaises(Exception):
            cfg.validate()

    def test_publisher_url_env_override_uses_nested_leaf(self) -> None:
        # BOT_PUBLISHER_URL 应该覆盖 publisher.xiaohongshu.base_url，
        # 而不是生成一个不存在的 publisher.base_url。
        # 这防止环境变量映射到错误的层级，导致配置看起来改了、实际没生效。
        url = "http://127.0.0.1:19999"
        with mock.patch.dict(os.environ, {"BOT_PUBLISHER_URL": url}, clear=False):
            cfg = load_config(None, root=Path.cwd() / "var", require_file=False)
        self.assertEqual(cfg.get("publisher.xiaohongshu.base_url"), url)
        self.assertIsNone(cfg.get("publisher.base_url"))

    def test_budget_is_explicit_usd_and_prices_are_usd(self) -> None:
        # 预算单位统一用美元，避免人民币/美元混用导致限额算错。
        # 同时验证默认价格下 100 万输入 + 100 万输出 = 1.50 美元。
        self.assertIn("daily_usd", DEFAULTS["budget"])
        self.assertIn("input_usd_per_million", DEFAULTS["budget"])
        self.assertIn("output_usd_per_million", DEFAULTS["budget"])
        self.assertNotIn("daily_cny", DEFAULTS["budget"])
        self.assertNotIn("input_cny_per_million", DEFAULTS["budget"])
        env = new_env("budget-usd")
        try:
            guard = BudgetGuard(env.db, env.config, env.clock)
            self.assertAlmostEqual(guard.estimate_usd(1_000_000, 1_000_000), 1.50)
            self.assertEqual(guard.snapshot().as_dict()["currency"], "USD")
        finally:
            cleanup(env)


class _LivePublisher(Publisher):
    # 只用于测试的 live 发布器：如果闸门没拦住，它会被调用。
    # publish 标了 pragma: no cover，因为正确行为是“根本走不到这里”。
    name = "live-dummy"
    mode = "live"

    def __init__(self) -> None:
        self.calls = 0

    def publish(self, request):  # pragma: no cover - the gate must block first
        self.calls += 1
        return PublishOutcome(state="failed", detail="must not be called")


class LiveDemoPublishGateTests(unittest.TestCase):
    def test_live_publish_refuses_fixture_source_and_fake_generator(self) -> None:
        # 假设有人绕过了配置层守卫，直接塞一个 live 发布器：
        # 发布闸门仍要拒绝 fixture 来源；把来源改成 rss 后，
        # 还要拒绝 fake 模型生成的稿子。发布器一次都不能被调用。
        env = new_env(
            "live-demo-gate",
            config_kwargs={
                "llm_provider": "fake",
                "account_enabled": True,
                "account_status": "verified",
            },
        )
        try:
            draft = make_draft(env)
            # Keep the fixture-generated artifact but simulate the live gate:
            # a live publisher must refuse it even if config-level guards were
            # bypassed by a custom caller.
            env.config.data["app"]["mode"] = "live"
            env.config.data["publisher"]["live_enabled"] = True
            approve(env, draft["approval_code"])
            approval = get_approval(env.db, approval_id=draft["approval_id"])
            publisher = _LivePublisher()

            ok, reason, _, _ = publish.validate_publish(
                env.db, env.config, env.clock, approval, publisher
            )
            self.assertFalse(ok)
            self.assertIn("fixture", reason)

            # Simulate a non-fixture source while keeping the fake-generated
            # draft; the generator check must still stop a live publish.
            env.db.execute("UPDATE sources SET kind='rss'")
            ok, reason, _, _ = publish.validate_publish(
                env.db, env.config, env.clock, approval, publisher
            )
            self.assertFalse(ok)
            self.assertIn("fake", reason)
            self.assertEqual(publisher.calls, 0)
        finally:
            cleanup(env)


class BudgetTests(unittest.TestCase):
    def test_unknown_usage_is_reserved_not_counted_as_free(self) -> None:
        # 模型没返回 usage 时，不能把费用当 0，否则预算会被悄悄突破。
        # 这里记录一笔“未知用量、预留 0.004 美元”：
        #   spent_usd 仍是 0（因为不知道实际花费）；
        #   reserved_usd 和 used_usd 是 0.004（按预留算进预算）。
        env = new_env("budget-unknown")
        try:
            guard = BudgetGuard(env.db, env.config, env.clock)
            guard.record(
                provider="deepseek",
                model="deepseek-flash",
                purpose="draft",
                prompt_tokens=None,
                completion_tokens=None,
                total_tokens=None,
                usage_known=False,
                reserved_usd=0.004,
            )
            snap = guard.snapshot()
            self.assertEqual(snap.calls, 1)
            self.assertAlmostEqual(snap.reserved_usd, 0.004)
            self.assertAlmostEqual(snap.spent_usd, 0.0)
            self.assertAlmostEqual(snap.used_usd, 0.004)
            row = env.db.fetchone("SELECT usage_known, cost_usd, reserved_usd FROM llm_usage")
            self.assertEqual(row["usage_known"], 0)
            self.assertEqual(row["cost_usd"], 0.0)
            self.assertEqual(row["reserved_usd"], 0.004)
        finally:
            cleanup(env)

    def test_budget_exhaustion_pauses_generation_but_approval_still_works(self) -> None:
        # 把每日预算调到 0.001，生成会被 BudgetExceeded / DraftError 挡住并暂停；
        # 但“批准”和“mock 发布”不花 API 钱，必须仍然可以完成。
        # 这条边界很重要：钱花完了不能把已经写好的内容也卡死。
        env = new_env("budget-pause")
        try:
            draft = make_draft(env)
            env.config.data["budget"]["daily_usd"] = 0.001
            guard = BudgetGuard(env.db, env.config, env.clock)
            with self.assertRaises(BudgetExceeded):
                guard.ensure_available(0.01)
            snap = guard.snapshot()
            self.assertTrue(snap.paused)
            with self.assertRaises(drafts_service.DraftError):
                drafts_service.create_draft(env.db, env.config, env.clock, env.llm)
            # Approval and mock publication do not spend API budget.
            approve(env, draft["approval_code"])
            result = publish.publish_approval(
                env.db,
                env.config,
                env.clock,
                draft["approval_id"],
                publisher=MockPublisher(env.config, env.clock),
            )
            self.assertEqual(result["state"], "simulated")
        finally:
            cleanup(env)

    def test_call_limit_and_next_day_reset(self) -> None:
        # 每天最多 1 次调用：用完后再申请会被拒；
        # 时钟前进一天后，暂停自动解除、计数归零。
        env = new_env("budget-day")
        try:
            env.config.data["budget"]["max_calls_per_day"] = 1
            guard = BudgetGuard(env.db, env.config, env.clock)
            guard.record(
                provider="deepseek",
                model="deepseek-flash",
                purpose="draft",
                prompt_tokens=100,
                completion_tokens=100,
                total_tokens=200,
                usage_known=True,
            )
            with self.assertRaises(BudgetExceeded):
                guard.ensure_available(0.0)
            env.clock.advance(days=1)
            self.assertFalse(guard.snapshot().paused)
            self.assertEqual(guard.snapshot().calls, 0)
        finally:
            cleanup(env)

    def test_simultaneous_reservations_with_tiny_budget_allow_only_one(self) -> None:
        # 两个线程同时想花掉最后一笔预算：检查和插入必须在同一个写事务里，
        # 否则两边都可能通过，然后才各自发起付费请求。
        env = new_env("budget-race")
        try:
            env.config.data["budget"]["daily_usd"] = 0.01
            guard = BudgetGuard(env.db, env.config, env.clock)
            barrier = threading.Barrier(2)
            reserved: list[int] = []
            denied: list[BaseException] = []

            def run() -> None:
                try:
                    barrier.wait(timeout=5)
                    reserved.append(
                        guard.reserve(
                            0.006,
                            provider="deepseek",
                            model="deepseek-flash",
                            purpose="draft",
                        )
                    )
                except BaseException as exc:  # noqa: BLE001
                    denied.append(exc)

            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            self.assertEqual(len(reserved), 1, denied)
            self.assertEqual(len(denied), 1)
            self.assertIsInstance(denied[0], BudgetExceeded)
            rows = env.db.fetchall("SELECT * FROM llm_usage")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["settled"], 0)
            self.assertAlmostEqual(rows[0]["reserved_usd"], 0.006)
            self.assertTrue(guard.snapshot().paused)
            self.assertGreaterEqual(guard.snapshot().used_usd, 0.006)

            guard.settle(
                reserved[0],
                prompt_tokens=100,
                completion_tokens=100,
                total_tokens=200,
                usage_known=True,
                ok=True,
            )
            row = env.db.fetchone("SELECT * FROM llm_usage")
            self.assertEqual(row["settled"], 1)
            self.assertEqual(row["usage_known"], 1)
            self.assertGreater(row["cost_usd"], 0)
            self.assertEqual(row["reserved_usd"], 0)
        finally:
            cleanup(env)

    def test_deepseek_reserves_before_network_and_settles_actual_usage(self) -> None:
        # 在假 _post() 里检查数据库：网络调用发生时，必须已经有一条
        # settled=0 的预留记录；响应回来后再结算实际 token 用量。
        env = new_env("budget-reserve-settle")
        try:
            env.config.secrets.deepseek_api_key = "test-key-not-used"
            env.config.data["budget"]["daily_usd"] = 0.10
            guard = BudgetGuard(env.db, env.config, env.clock)
            llm = DeepSeekLLM(env.config, env.clock, guard, sleep=lambda _s: None)
            seen: list[int] = []

            def fake_post(body, key):
                seen.append(int(env.db.scalar("SELECT COUNT(*) FROM llm_usage WHERE settled=0")))
                return {
                    "model": "deepseek-flash",
                    "choices": [{"message": {"content": '{"title": "ok"}'}}],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
                }

            llm._post = fake_post  # type: ignore[assignment]
            response = llm.complete_json(purpose="draft", payload={"evidence": []})
            self.assertEqual(response.data["title"], "ok")
            self.assertEqual(seen, [1])
            row = env.db.fetchone("SELECT * FROM llm_usage")
            self.assertEqual(row["settled"], 1)
            self.assertEqual(row["usage_known"], 1)
            self.assertEqual(row["prompt_tokens"], 120)
            self.assertGreater(row["cost_usd"], 0)
            self.assertEqual(row["reserved_usd"], 0)
        finally:
            cleanup(env)

    def test_unknown_network_error_retains_conservative_reservation(self) -> None:
        # 重试型网络错误可能已经被服务商计费：不能记 0，必须保留预留。
        env = new_env("budget-retain")
        try:
            env.config.secrets.deepseek_api_key = "test-key-not-used"
            env.config.data["budget"]["daily_usd"] = 0.10
            env.config.data["llm"]["max_retries"] = 0
            guard = BudgetGuard(env.db, env.config, env.clock)
            llm = DeepSeekLLM(env.config, env.clock, guard, sleep=lambda _s: None)

            def failing_post(body, key):
                raise _RetryableHTTP("simulated connection reset")

            llm._post = failing_post  # type: ignore[assignment]
            with self.assertRaises(Exception):
                llm.complete_json(purpose="draft", payload={"evidence": []})
            row = env.db.fetchone("SELECT * FROM llm_usage")
            self.assertEqual(row["settled"], 1)
            self.assertEqual(row["usage_known"], 0)
            self.assertEqual(row["ok"], 0)
            self.assertGreater(row["reserved_usd"], 0)
            self.assertEqual(row["cost_usd"], 0)
        finally:
            cleanup(env)


if __name__ == "__main__":
    unittest.main()

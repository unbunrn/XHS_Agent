"""Offline tests for the optional DeepSeek Search/Fetch source adapter."""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path
from unittest import mock

from bot.adapters.sources import DeepSeekSearchSource
from bot.adapters.types import BudgetExceeded, SourceError
from bot.budget import BudgetGuard
from bot.services import research
from tests.helpers import cleanup, make_http, new_env

ROOT = Path(__file__).resolve().parents[1]


def _entry(**overrides) -> dict:
    entry = {
        "name": "DeepSeek Search",
        "query": "AI 工具 发布",
        "kind": "deepseek_search",
        "trust_tier": 2,
        "allowed_domains": ["example.com"],
        "max_results": 3,
        "fetch_content": False,
    }
    entry.update(overrides)
    return entry


class DeepSeekSearchTests(unittest.TestCase):
    def _source(self, env, *, budget=None, **entry_overrides):
        env.config.secrets.deepseek_api_key = "test-key"
        return DeepSeekSearchSource(
            _entry(**entry_overrides),
            env.config,
            make_http(env.config),
            env.clock,
            budget=budget or BudgetGuard(env.db, env.config, env.clock),
            sleep=lambda _s: None,
        )

    def test_search_results_keep_provenance_and_skip_empty_content(self) -> None:
        env = new_env("ds-search")
        try:
            source = self._source(env)

            def fake_post(path, payload):
                self.assertEqual(path, "/v1/tools/search")
                self.assertEqual(payload["count"], 3)
                return {
                    "results": [
                        {
                            "title": "New tool",
                            "url": "https://example.com/a",
                            "content": "Official content for evidence.",
                            "summary": "short summary",
                            "date": "2026-11-05T08:00:00Z",
                        },
                        {
                            "title": "Empty",
                            "url": "https://example.com/empty",
                            "content": "",
                            "summary": "summary is not evidence",
                            "date": None,
                        },
                        {
                            "title": "No date",
                            "url": "https://example.com/b",
                            "content": "Undated public content.",
                            "summary": "",
                            "date": None,
                        },
                    ]
                }

            source._post_once = fake_post  # type: ignore[assignment]
            fetched = source.fetch()
            self.assertEqual(len(fetched.entries), 2)
            self.assertEqual(fetched.entries[0].title, "New tool")
            self.assertEqual(fetched.entries[0].content, "Official content for evidence.")
            self.assertEqual(fetched.entries[0].published_raw, "2026-11-05T08:00:00Z")
            self.assertEqual(fetched.entries[1].published_raw, "")
            row = env.db.fetchone("SELECT * FROM llm_usage")
            self.assertEqual(row["settled"], 1)
            self.assertAlmostEqual(row["cost_usd"], 0.00075)
            self.assertEqual(row["reserved_usd"], 0)
        finally:
            cleanup(env)

    def test_provider_summary_never_becomes_evidence(self) -> None:
        env = new_env("ds-summary-evidence")
        try:
            env.config.secrets.deepseek_api_key = "test-key"
            env.config.data["sources"] = [_entry(enabled=True, fetch_content=False)]
            research.sync_sources(env.db, env.config)
            source = research.source_row(env.db, "DeepSeek Search")

            def fake_post(self, path, payload):
                return {
                    "results": [
                        {
                            "title": "Tool release",
                            "url": "https://example.com/a",
                            "content": "真实正文第一句说明工具已经发布。真实正文第二句说明它是免费的。",
                            "summary": "合成摘要声称工具尚未发布且必须付费。",
                            "date": "2026-11-05T08:00:00Z",
                        }
                    ]
                }

            with mock.patch.object(DeepSeekSearchSource, "_post_once", fake_post):
                result = research.ingest_source(
                    env.db,
                    env.config,
                    make_http(env.config),
                    env.clock,
                    source,
                    budget=BudgetGuard(env.db, env.config, env.clock),
                )
            self.assertIsNone(result["error"], result)
            quotes = [
                row["quote"]
                for row in env.db.fetchall("SELECT quote FROM evidence ORDER BY id")
            ]
            self.assertTrue(any("真实正文" in quote for quote in quotes), quotes)
            self.assertFalse(any("合成摘要" in quote for quote in quotes), quotes)
            self.assertEqual(
                env.db.scalar("SELECT summary FROM raw_items"), ""
            )
        finally:
            cleanup(env)

    def test_retry_reserves_before_each_physical_attempt(self) -> None:
        env = new_env("ds-retry-budget")
        try:
            env.config.data["network"]["max_retries"] = 1
            source = self._source(env, fetch_content=False)
            attempts = 0
            reservations_before_call: list[int] = []

            def fake_post(path, payload):
                nonlocal attempts
                reservations_before_call.append(
                    int(env.db.scalar("SELECT COUNT(*) FROM llm_usage"))
                )
                attempts += 1
                if attempts == 1:
                    raise SourceError("simulated timeout", retryable=True)
                return {
                    "results": [
                        {
                            "title": "Recovered",
                            "url": "https://example.com/a",
                            "content": "Recovered content after a billed timeout.",
                            "summary": "synthetic summary",
                            "date": "2026-11-05T08:00:00Z",
                        }
                    ]
                }

            source._post_once = fake_post  # type: ignore[assignment]
            fetched = source.fetch()
            self.assertEqual(attempts, 2)
            # Each physical attempt saw its own reservation already inserted.
            self.assertEqual(reservations_before_call, [1, 2])
            rows = env.db.fetchall("SELECT * FROM llm_usage ORDER BY id")
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["settled"], 1)
            self.assertEqual(rows[0]["ok"], 0)
            self.assertGreater(rows[0]["reserved_usd"], 0)
            self.assertEqual(rows[1]["settled"], 1)
            self.assertAlmostEqual(rows[1]["cost_usd"], 0.00075)
            self.assertEqual(rows[1]["reserved_usd"], 0)
            self.assertEqual(fetched.entries[0].content, "Recovered content after a billed timeout.")
        finally:
            cleanup(env)

    def test_tiny_budget_blocks_retry_network_attempt(self) -> None:
        env = new_env("ds-retry-denied")
        try:
            env.config.data["budget"]["daily_usd"] = 0.00075
            env.config.data["network"]["max_retries"] = 2
            source = self._source(env, fetch_content=False)
            attempts = 0

            def fake_post(path, payload):
                nonlocal attempts
                attempts += 1
                raise SourceError("simulated timeout", retryable=True)

            source._post_once = fake_post  # type: ignore[assignment]
            with self.assertRaises(BudgetExceeded):
                source.fetch()
            self.assertEqual(attempts, 1)
            rows = env.db.fetchall("SELECT * FROM llm_usage ORDER BY id")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["settled"], 1)
            self.assertGreater(rows[0]["reserved_usd"], 0)
        finally:
            cleanup(env)

    def test_after_filter_advances_with_clock_and_keeps_original_date(self) -> None:
        env = new_env("ds-after-clock")
        try:
            source = self._source(env, fetch_content=False, lookback_days=3)
            queries: list[str] = []

            def fake_post(path, payload):
                queries.append(payload["query"])
                return {
                    "results": [
                        {
                            "title": "Dated result",
                            "url": "https://example.com/a",
                            "content": "Current content used for the after filter test.",
                            "summary": "synthetic summary",
                            "date": "2020-01-02T00:00:00Z",
                        }
                    ]
                }

            source._post_once = fake_post  # type: ignore[assignment]
            first = source.fetch()
            self.assertEqual(queries[-1], "AI 工具 发布 after:2026-11-02")
            # The result's original date is retained, never replaced by today.
            self.assertEqual(first.entries[0].published_raw, "2020-01-02T00:00:00Z")

            env.clock.advance(hours=48)
            source.fetch()
            self.assertEqual(queries[-1], "AI 工具 发布 after:2026-11-04")
        finally:
            cleanup(env)

    def test_exact_allowlist_excludes_community_subdomain_by_default(self) -> None:
        env = new_env("ds-exact-host")
        try:
            source = self._source(env, fetch_content=False)

            def fake_post(path, payload):
                return {
                    "results": [
                        {
                            "title": "Official",
                            "url": "https://example.com/a",
                            "content": "Official source content.",
                            "summary": "",
                            "date": "2026-11-05T08:00:00Z",
                        },
                        {
                            "title": "Community",
                            "url": "https://community.example.com/b",
                            "content": "Community source content.",
                            "summary": "",
                            "date": "2026-11-05T08:00:00Z",
                        },
                    ]
                }

            source._post_once = fake_post  # type: ignore[assignment]
            urls = [entry.url for entry in source.fetch().entries]
            self.assertEqual(urls, ["https://example.com/a"])

            opt_in = self._source(
                env, fetch_content=False, allowed_domains=["*.example.com"]
            )
            opt_in._post_once = fake_post  # type: ignore[assignment]
            urls = [entry.url for entry in opt_in.fetch().entries]
            self.assertIn("https://community.example.com/b", urls)
        finally:
            cleanup(env)

    def test_old_search_result_is_not_magically_fresh(self) -> None:
        env = new_env("ds-old-result")
        try:
            env.config.secrets.deepseek_api_key = "test-key"
            env.config.data["sources"] = [_entry(enabled=True, fetch_content=False)]
            research.sync_sources(env.db, env.config)
            source = research.source_row(env.db, "DeepSeek Search")

            def fake_post(self, path, payload):
                return {
                    "results": [
                        {
                            "title": "OpenAI 发布免费 API 工具教程",
                            "url": "https://example.com/old",
                            "content": "这是一条多年前的旧消息，介绍已经结束的免费 API 活动和教程。",
                            "summary": "synthetic summary",
                            "date": "2020-01-02T00:00:00Z",
                        }
                    ]
                }

            with mock.patch.object(DeepSeekSearchSource, "_post_once", fake_post):
                result = research.ingest_source(
                    env.db,
                    env.config,
                    make_http(env.config),
                    env.clock,
                    source,
                    budget=BudgetGuard(env.db, env.config, env.clock),
                )
            self.assertIsNone(result["error"], result)
            raw = env.db.fetchone("SELECT published_at FROM raw_items")
            self.assertTrue(raw["published_at"].startswith("2020-01-02"), raw["published_at"])
            self.assertEqual(research.ensure_research_queue(env.db, env.config, env.clock), [])
            self.assertEqual(
                env.db.scalar("SELECT COUNT(*) FROM events WHERE status='researched'"), 0
            )
        finally:
            cleanup(env)

    def test_fetch_content_is_used_and_each_call_is_settled(self) -> None:
        env = new_env("ds-fetch")
        try:
            source = self._source(env, fetch_content=True, max_fetches=1, max_results=1)
            calls: list[str] = []

            def fake_post(path, payload):
                calls.append(path)
                if path == "/v1/tools/search":
                    return {
                        "results": [
                            {
                                "title": "Tool",
                                "url": "https://example.com/a",
                                "content": "",
                                "summary": "",
                                "date": "2026-11-05",
                            }
                        ]
                    }
                self.assertEqual(path, "/v1/tools/fetch")
                self.assertEqual(payload, {"url": "https://example.com/a"})
                return {"title": "Tool", "content": "Fetched markdown evidence."}

            source._post_once = fake_post  # type: ignore[assignment]
            fetched = source.fetch()
            self.assertEqual(calls, ["/v1/tools/search", "/v1/tools/fetch"])
            self.assertEqual(fetched.entries[0].content, "Fetched markdown evidence.")
            rows = env.db.fetchall("SELECT * FROM llm_usage ORDER BY id")
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row["cost_usd"] == 0.00075 for row in rows))
        finally:
            cleanup(env)

    def test_disallowed_private_and_credentialed_targets_are_rejected(self) -> None:
        env = new_env("ds-allowlist")
        try:
            source = self._source(env)
            source._post_once = lambda path, payload: {  # type: ignore[assignment]
                "results": [
                    {"title": "loopback", "url": "http://127.0.0.1/x", "content": "x"},
                    {"title": "creds", "url": "https://user:pass@example.com/x", "content": "x"},
                    {"title": "other", "url": "https://evil.invalid/x", "content": "x"},
                ]
            }
            self.assertEqual(source.fetch().entries, [])
        finally:
            cleanup(env)

    def test_known_empty_search_settles_at_zero(self) -> None:
        env = new_env("ds-empty")
        try:
            source = self._source(env)
            source._post_once = lambda path, payload: {"results": []}  # type: ignore[assignment]
            self.assertEqual(source.fetch().entries, [])
            row = env.db.fetchone("SELECT * FROM llm_usage")
            self.assertEqual(row["settled"], 1)
            self.assertEqual(row["cost_usd"], 0)
            self.assertEqual(row["reserved_usd"], 0)
        finally:
            cleanup(env)

    def test_ambiguous_transport_error_retains_reservation(self) -> None:
        env = new_env("ds-ambiguous")
        try:
            source = self._source(env)

            def fail(path, payload):
                raise SourceError("simulated reset", retryable=True)

            source._post_once = fail  # type: ignore[assignment]
            with self.assertRaises(SourceError):
                source.fetch()
            row = env.db.fetchone("SELECT * FROM llm_usage")
            self.assertEqual(row["settled"], 1)
            self.assertEqual(row["ok"], 0)
            self.assertEqual(row["cost_usd"], 0)
            self.assertGreater(row["reserved_usd"], 0)
        finally:
            cleanup(env)

    def test_config_entry_reaches_the_adapter_and_example_is_documented(self) -> None:
        env = new_env("ds-config")
        try:
            env.config.secrets.deepseek_api_key = "test-key"
            env.config.data["sources"] = [_entry(enabled=True)]
            research.sync_sources(env.db, env.config)
            source = research.source_row(env.db, "DeepSeek Search")
            adapter = research.adapter_for(
                env.db,
                env.config,
                make_http(env.config),
                env.clock,
                source,
                budget=BudgetGuard(env.db, env.config, env.clock),
            )
            self.assertIsInstance(adapter, DeepSeekSearchSource)
            self.assertEqual(adapter.query, "AI 工具 发布")
            self.assertIn("example.com", adapter.allowed_domains)

            with (ROOT / "config" / "bot.example.toml").open("rb") as handle:
                example = tomllib.load(handle)
            search = [s for s in example["sources"] if s.get("kind") == "deepseek_search"]
            self.assertTrue(search)
            self.assertTrue(search[0]["allowed_domains"])
        finally:
            cleanup(env)

    def test_search_source_ingests_without_local_dns_resolution(self) -> None:
        env = new_env("ds-ingest")
        try:
            env.config.secrets.deepseek_api_key = "test-key"
            env.config.data["sources"] = [_entry(enabled=True)]
            research.sync_sources(env.db, env.config)
            source = research.source_row(env.db, "DeepSeek Search")

            def fake_post(self, path, payload):
                return {
                    "results": [
                        {
                            "title": "Public item",
                            "url": "https://example.com/a",
                            "content": "Public retrieved evidence.",
                            "summary": "",
                            "date": "2026-11-05T08:00:00Z",
                        }
                    ]
                }

            with mock.patch.object(DeepSeekSearchSource, "_post_once", fake_post):
                result = research.ingest_source(
                    env.db,
                    env.config,
                    make_http(env.config),
                    env.clock,
                    source,
                    budget=BudgetGuard(env.db, env.config, env.clock),
                )
            self.assertIsNone(result["error"], result)
            self.assertEqual(result["new_items"], 1)
            self.assertEqual(
                env.db.scalar("SELECT content FROM raw_items"), "Public retrieved evidence."
            )
        finally:
            cleanup(env)

    def test_budget_failure_prevents_transport(self) -> None:
        env = new_env("ds-budget")
        try:
            env.config.data["budget"]["daily_usd"] = 0.0001
            source = self._source(env)
            called = False

            def fake_post(path, payload):
                nonlocal called
                called = True
                return {"results": []}

            source._post_once = fake_post  # type: ignore[assignment]
            with self.assertRaises(BudgetExceeded):
                source.fetch()
            self.assertFalse(called)
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM llm_usage"), 0)
        finally:
            cleanup(env)


if __name__ == "__main__":
    unittest.main()

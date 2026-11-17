"""S2 planning and source-identity contract tests (offline)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase

from bot.services.research_plan import build_research_plan, fallback_plan
from bot.services.source_identity import (
    SourceVerdict,
    apply_verdicts,
    persist_verdicts,
    verify_source_material,
)
from tests.helpers import cleanup, new_env


class StubLLM:
    def __init__(self, data=None, error=None):
        self.data = data or {}
        self.error = error
        self.calls = []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(data=self.data, provider="stub")


def _entry(url, content="", title="material"):
    return SimpleNamespace(url=url, title=title, content=content)


class PlanTests(TestCase):
    def setUp(self):
        self.env = new_env("s2-plan")

    def tearDown(self):
        cleanup(self.env)

    def test_fallback_preserves_two_distinct_versions_and_three_query_cap(self):
        plan = fallback_plan(
            topic="Fable 5.2 与 Mythos 5.2 发布",
            query_base="Fable 5.2 Mythos 5.2",
            supplied_url=None,
        )
        self.assertEqual(plan.versions, ["Fable 5.2", "Mythos 5.2"])
        self.assertLessEqual(len(plan.queries), 3)

    def test_model_plan_is_one_call_and_capped_at_three_queries(self):
        llm = StubLLM(
            {
                "entities": ["Fable", "Mythos"],
                "versions": ["Fable 5.2", "Mythos 5.2"],
                "queries": ["q1", "q2", "q3", "q4", "q5"],
                "claims": ["claim"],
                "source_urls": ["https://example.com/a"],
            }
        )
        plan = build_research_plan(
            self.env.db,
            self.env.config,
            self.env.clock,
            llm,
            topic="两个版本",
            query_base="两个版本",
            supplied_url=None,
        )
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(plan.queries, ["q1", "q2", "q3"])
        self.assertEqual(plan.versions, ["Fable 5.2", "Mythos 5.2"])

    def test_planning_failure_falls_back_without_retry(self):
        llm = StubLLM(error=RuntimeError("model down"))
        plan = build_research_plan(
            self.env.db,
            self.env.config,
            self.env.clock,
            llm,
            topic="Fable 5.2",
            query_base="Fable 5.2",
            supplied_url=None,
        )
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(plan.generator, "fallback")
        self.assertIn("planning fallback", plan.note)


class IdentityTests(TestCase):
    def setUp(self):
        self.env = new_env("s2-identity")

    def tearDown(self):
        cleanup(self.env)

    def _verify(self, entries, data, trusted):
        llm = StubLLM(data)
        verdicts = verify_source_material(
            self.env.db,
            self.env.config,
            self.env.clock,
            llm,
            request={"topic": "Fable 5.2", "research_mode": "news"},
            entries=entries,
            trusted_domains=trusted,
        )
        self.assertEqual(len(llm.calls), 1)
        return verdicts

    def test_configured_domain_needs_real_content_support(self):
        entries = [_entry("https://example.com/release", "Fable 5.2 正式发布，包含新能力。")]
        verdicts = self._verify(
            entries,
            {
                "sources": [
                    {
                        "evidence_id": "E1",
                        "role": "official",
                        "organization": "Example",
                        "supported_claims": ["Fable 5.2"],
                        "relation": "configured_trusted_domain",
                        "confidence": 0.9,
                        "insufficient": False,
                    }
                ]
            },
            ["example.com"],
        )
        self.assertTrue(verdicts[0].verified)
        self.assertEqual(verdicts[0].role, "official")
        apply_verdicts(self.env.db, self.env.clock, entries, verdicts)
        self.assertTrue(entries[0].source_verified)

    def test_two_entries_use_one_batch_identity_call(self):
        entries = [
            _entry("https://example.com/one", "Fable 5.2 正式发布，包含新能力。"),
            _entry("https://example.com/two", "Fable 5.2 文档说明如下。"),
        ]
        verdicts = self._verify(
            entries,
            {
                "sources": [
                    {
                        "evidence_id": "E1",
                        "role": "official",
                        "organization": "Example",
                        "supported_claims": ["Fable 5.2"],
                        "relation": "configured_trusted_domain",
                        "confidence": 0.9,
                        "insufficient": False,
                    },
                    {
                        "evidence_id": "E2",
                        "role": "secondary",
                        "organization": "Example",
                        "supported_claims": ["Fable 5.2"],
                        "relation": "configured_trusted_domain",
                        "confidence": 0.8,
                        "insufficient": False,
                    },
                ]
            },
            ["example.com"],
        )
        self.assertEqual(len(verdicts), 2)
        self.assertTrue(all(verdict.verified for verdict in verdicts))

    def test_unlisted_official_docs_with_body_support_is_verified(self):
        entries = [
            _entry(
                "https://code.claude.com/docs/en/model-config",
                "Claude Code 的模型配置文档说明 Fable 5.2 与 Opus 的用途。",
            )
        ]
        data = {
            "sources": [
                {
                    "evidence_id": "E1",
                    "role": "official",
                    "organization": "Anthropic",
                    "supported_claims": ["Fable 5.2", "Opus"],
                    "relation": "official_docs",
                    "confidence": 0.9,
                    "insufficient": False,
                }
            ]
        }
        verdicts = self._verify(entries, data, [])
        self.assertTrue(verdicts[0].verified)
        self.assertEqual(verdicts[0].role, "official")
        apply_verdicts(self.env.db, self.env.clock, entries, verdicts)
        self.assertTrue(entries[0].source_verified)
        self.assertTrue(entries[0].source_usable)
        persist_verdicts(self.env.db, self.env.config, self.env.clock, verdicts)
        second = verify_source_material(
            self.env.db,
            self.env.config,
            self.env.clock,
            StubLLM({"sources": []}),
            request={"topic": "Fable 5.2", "research_mode": "news"},
            entries=entries,
            trusted_domains=[],
        )
        self.assertTrue(second[0].verified)

    def test_unlisted_official_docs_uses_real_title_when_claims_are_paraphrased(self):
        entries = [
            _entry(
                "https://code.claude.com/docs/en/model-config",
                "Model configuration - Claude Code Docs\n"
                "Use /model fable to select Fable 5.2 in Claude Code v2.1.321+.",
                title="Model configuration - Claude Code Docs",
            )
        ]
        verdicts = self._verify(
            entries,
            {
                "sources": [
                    {
                        "evidence_id": "E1",
                        "role": "official",
                        "organization": "Anthropic",
                        "supported_claims": [
                            "Fable 别名在 Claude apps gateway 中解析为 Fable 5",
                            "Fable 5.2 需要 Claude Code v2.1.321 或更高版本",
                        ],
                        "relation": "official_docs",
                        "reason": "这是 Anthropic 的 Claude Code 官方文档页面。",
                        "confidence": 0.95,
                        "insufficient": False,
                    }
                ]
            },
            [],
        )
        self.assertTrue(verdicts[0].verified)
        self.assertEqual(verdicts[0].role, "official")
        self.assertEqual(
            verdicts[0].supported_claims, ["Model configuration - Claude Code Docs"]
        )

    def test_official_title_supports_page_when_fetched_body_omits_html_title(self):
        body = (
            "Use /model fable to select Fable 5.2 in Claude Code v2.1.321 or later. "
            "The fable alias resolves to Fable 5.2 outside the Claude apps gateway. "
            "Opus 5.5 requires v2.1.280 and Sonnet 5.5 requires v2.1.284. "
            "Fable models are never an account default and must be selected explicitly. "
        ) * 4
        entries = [
            _entry(
                "https://code.claude.com/docs/en/model-config",
                body,
                title="Model configuration - Claude Code Docs",
            )
        ]
        verdicts = self._verify(
            entries,
            {
                "sources": [
                    {
                        "evidence_id": "E1",
                        "role": "official",
                        "organization": "Anthropic",
                        "supported_claims": [
                            "Fable 别名在 Claude apps gateway 中解析为 Fable 5",
                            "Fable 5.2 需要 Claude Code v2.1.321 或更高版本",
                        ],
                        "relation": "official_docs",
                        "reason": "这是 Anthropic 的 Claude Code 官方文档页面。",
                        "confidence": 0.95,
                        "insufficient": False,
                    }
                ]
            },
            [],
        )
        self.assertTrue(verdicts[0].verified)
        self.assertEqual(
            verdicts[0].supported_claims, ["Model configuration - Claude Code Docs"]
        )

    def test_official_blog_reason_coerces_unknown_without_supported_claims(self):
        entries = [
            _entry(
                "https://claude.com/blog/claude-model-and-effort-level-in-claude-code",
                "Claude Code 的模型与 effort 选择说明。" * 30,
                title="Claude Code effort level and model selection | Claude by Anthropic",
            )
        ]
        verdicts = self._verify(
            entries,
            {
                "sources": [
                    {
                        "evidence_id": "E1",
                        "role": "unknown",
                        "organization": "Anthropic",
                        "supported_claims": [],
                        "relation": "official_announcement",
                        "reason": "claude.com 为 Anthropic 官方域名，正文为官方博客长文，直接支持主题。",
                        "confidence": 0.93,
                        "insufficient": True,
                    }
                ]
            },
            [],
        )
        self.assertTrue(verdicts[0].verified)
        self.assertEqual(verdicts[0].role, "official")
        self.assertEqual(
            verdicts[0].supported_claims,
            ["Claude Code effort level and model selection | Claude by Anthropic"],
        )

    def test_self_claimed_official_without_trusted_link_is_rejected(self):
        entries = [_entry("https://vendor.example/product", "Fable 5.2 official site")]
        verdicts = self._verify(
            entries,
            {
                "sources": [
                    {
                        "evidence_id": "E1",
                        "role": "official",
                        "organization": "Vendor",
                        "supported_claims": ["Fable 5.2"],
                        "relation": "we are the official site",
                        "confidence": 0.99,
                        "insufficient": False,
                    }
                ]
            },
            [],
        )
        self.assertFalse(verdicts[0].verified)
        self.assertEqual(verdicts[0].role, "unknown")

    def test_fabricated_evidence_id_never_verifies(self):
        entries = [_entry("https://vendor.example/product", "Fable 5.2 official site")]
        verdicts = self._verify(
            entries,
            {
                "sources": [
                    {
                        "evidence_id": "E99",
                        "role": "official",
                        "organization": "Vendor",
                        "supported_claims": ["Fable 5.2"],
                        "relation": "trusted_link",
                        "confidence": 0.99,
                    }
                ]
            },
            [],
        )
        self.assertFalse(verdicts[0].verified)
        self.assertEqual(verdicts[0].evidence_id, "E1")

    def test_real_link_from_trusted_page_can_verify_new_vendor_domain(self):
        entries = [
            _entry(
                "https://example.com/news",
                "官方新闻：产品页面 https://vendor.example/product 已发布 Fable 5.2。",
            ),
            _entry("https://vendor.example/product", "Fable 5.2 正式发布。"),
        ]
        verdicts = self._verify(
            entries,
            {
                "sources": [
                    {
                        "evidence_id": "E1",
                        "role": "secondary",
                        "supported_claims": ["Fable 5.2"],
                        "relation": "configured_trusted_domain",
                        "confidence": 0.9,
                    },
                    {
                        "evidence_id": "E2",
                        "role": "official",
                        "organization": "Vendor",
                        "supported_claims": ["Fable 5.2"],
                        "relation": "trusted_link",
                        "confidence": 0.9,
                        "insufficient": False,
                    },
                ]
            },
            ["example.com"],
        )
        self.assertTrue(verdicts[0].verified)
        self.assertTrue(verdicts[1].verified)
        self.assertEqual(verdicts[1].role, "official")

    def test_secondary_report_without_configured_domain_is_usable_but_not_official(self):
        entries = [
            _entry(
                "https://ifanr.example/fable",
                "据媒体报道，Fable 5.2 正式发布，新增记忆检索能力。",
            )
        ]
        verdicts = self._verify(
            entries,
            {
                "sources": [
                    {
                        "evidence_id": "E1",
                        "role": "secondary",
                        "organization": "iFanr",
                        "supported_claims": ["Fable 5.2"],
                        "relation": "media_report",
                        "confidence": 0.8,
                        "insufficient": True,
                    }
                ]
            },
            [],
        )
        self.assertFalse(verdicts[0].verified)
        self.assertTrue(verdicts[0].usable)
        self.assertEqual(verdicts[0].role, "secondary")
        apply_verdicts(self.env.db, self.env.clock, entries, verdicts)
        self.assertTrue(entries[0].source_usable)
        self.assertFalse(entries[0].source_verified)

    def test_secondary_report_with_empty_claims_uses_real_title(self):
        body = (
            "OpenRouter 对 MCP、CLI 与 Skills 三种工具调用方式做了统一任务实测，"
            "比较上下文 token 消耗与费用差异，并给出了工具选择建议。" * 10
        )
        entries = [
            _entry(
                "https://theainews.example/openrouter-mcp-cli-skills",
                body,
                title="OpenRouter 实测 MCP、CLI 与 Skill 的上下文成本",
            )
        ]
        verdicts = self._verify(
            entries,
            {
                "sources": [
                    {
                        "evidence_id": "E1",
                        "role": "secondary",
                        "organization": "The AI News",
                        "supported_claims": [],
                        "relation": "media_report",
                        "confidence": 0.7,
                        "insufficient": True,
                    }
                ]
            },
            [],
        )
        self.assertFalse(verdicts[0].verified)
        self.assertTrue(verdicts[0].usable)
        self.assertEqual(verdicts[0].role, "secondary")
        self.assertEqual(
            verdicts[0].supported_claims,
            ["OpenRouter 实测 MCP、CLI 与 Skill 的上下文成本"],
        )

    def test_secondary_report_cache_stays_usable_not_verified(self):
        entries = [
            _entry("https://ifanr.example/fable", "据媒体报道，Fable 5.2 正式发布。")
        ]
        data = {
            "sources": [
                {
                    "evidence_id": "E1",
                    "role": "secondary",
                    "organization": "iFanr",
                    "supported_claims": ["Fable 5.2"],
                    "relation": "media_report",
                    "confidence": 0.8,
                }
            ]
        }
        request = {"topic": "Fable 5.2", "research_mode": "news"}
        first = StubLLM(data)
        verdicts = verify_source_material(
            self.env.db,
            self.env.config,
            self.env.clock,
            first,
            request=request,
            entries=entries,
            trusted_domains=[],
        )
        self.assertTrue(verdicts[0].usable)
        self.assertFalse(verdicts[0].verified)
        persist_verdicts(self.env.db, self.env.config, self.env.clock, verdicts)
        second = StubLLM(data)
        cached = verify_source_material(
            self.env.db,
            self.env.config,
            self.env.clock,
            second,
            request=request,
            entries=entries,
            trusted_domains=[],
        )
        self.assertTrue(cached[0].usable)
        self.assertFalse(cached[0].verified)
        self.assertEqual(second.calls, [])

    def test_model_failure_never_turns_configured_domain_into_verified_evidence(self):
        entries = [
            _entry("https://example.com/a", "body"),
            _entry("https://other.example/b", "body"),
        ]
        llm = StubLLM(error=RuntimeError("judge down"))
        verdicts = verify_source_material(
            self.env.db,
            self.env.config,
            self.env.clock,
            llm,
            request={"topic": "x"},
            entries=entries,
            trusted_domains=["example.com"],
        )
        # The configured domain keeps its diagnostic role, but a failed judge
        # must never verify fetched content or promote it to evidence.
        self.assertEqual(verdicts[0].role, "official")
        self.assertFalse(verdicts[0].verified)
        self.assertTrue(verdicts[0].insufficient)
        self.assertFalse(verdicts[1].verified)
        self.assertIsInstance(verdicts[0], SourceVerdict)

    def test_identity_cache_requires_exact_material_and_policy(self):
        entries = [_entry("https://example.com/release", "Fable 5.2 正式发布。")]
        data = {
            "sources": [
                {
                    "evidence_id": "E1",
                    "role": "official",
                    "organization": "Example",
                    "supported_claims": ["Fable 5.2"],
                    "relation": "configured_trusted_domain",
                    "confidence": 0.9,
                    "insufficient": False,
                }
            ]
        }
        request = {"topic": "Fable 5.2", "research_mode": "news"}
        first = StubLLM(data)
        verdicts = verify_source_material(
            self.env.db,
            self.env.config,
            self.env.clock,
            first,
            request=request,
            entries=entries,
            trusted_domains=["example.com"],
        )
        self.assertTrue(verdicts[0].verified)
        self.assertEqual(len(first.calls), 1)
        persist_verdicts(
            self.env.db, self.env.config, self.env.clock, verdicts
        )

        second = StubLLM(data)
        cached = verify_source_material(
            self.env.db,
            self.env.config,
            self.env.clock,
            second,
            request=request,
            entries=entries,
            trusted_domains=["example.com"],
        )
        self.assertTrue(cached[0].verified)
        self.assertEqual(second.calls, [])

        changed_policy = StubLLM(data)
        verify_source_material(
            self.env.db,
            self.env.config,
            self.env.clock,
            changed_policy,
            request=request,
            entries=entries,
            trusted_domains=["example.com", "other.example"],
        )
        self.assertEqual(len(changed_policy.calls), 1)

        changed_material = StubLLM(data)
        verify_source_material(
            self.env.db,
            self.env.config,
            self.env.clock,
            changed_material,
            request=request,
            entries=[_entry("https://example.com/release", "Fable 5.3 正式发布。")],
            trusted_domains=["example.com"],
        )
        self.assertEqual(len(changed_material.calls), 1)

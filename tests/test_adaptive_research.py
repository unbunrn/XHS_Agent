"""Adaptive research loop tests.

The loop is deliberately deterministic: it names observable coverage gaps,
adds at most two follow-up searches, and never promotes an unfetched claim.
"""

from __future__ import annotations

import unittest

from bot.adapters.types import SourceEntry
from bot.services.adaptive_research import coverage_decision
from bot.services.research_orchestrator import PlanRun, run_followups
from bot.services.research_plan import ResearchPlan
from bot.services.topic_research import ResearchOutcome


def _entry(url: str, *, title: str = "模型发布", body: str = "支持 100 个并发") -> SourceEntry:
    return SourceEntry(title=title, url=url, summary=body, content=body)


def _plan(topic: str = "Gemini 4 Carbon 对比 价格") -> ResearchPlan:
    return ResearchPlan(
        topic=topic,
        queries=["q1", "q2", "q3"],
        research_mode="news",
        generator="test",
    )


class CoverageDecisionTests(unittest.TestCase):
    def test_empty_material_requests_official_and_independent_followups(self) -> None:
        run = PlanRun(status="no_evidence", counts={"task_physical_fetches": 0})
        decision = coverage_decision(
            run,
            _plan(),
            trusted_domains=["example.com"],
            topic="Gemini 4 Carbon 对比 价格",
            max_queries=2,
            max_fetch=6,
        )
        self.assertIn("official_source", decision.gaps)
        self.assertIn("independent_verification", decision.gaps)
        self.assertEqual(len(decision.queries), 2)
        self.assertTrue(any("site:example.com" in q for q in decision.queries))
        self.assertEqual(decision.reason, "follow_up")

    def test_covered_material_does_not_spend_extra_queries(self) -> None:
        run = PlanRun(
            status="completed",
            entries=[
                _entry("https://example.com/a", body="价格 100 元，支持 20 个任务"),
                _entry("https://other.example/b", body="价格 120 元，支持 10 个任务"),
            ],
            counts={"task_physical_fetches": 2},
        )
        decision = coverage_decision(
            run,
            _plan("OpenAI 发布 价格"),
            trusted_domains=["example.com"],
            topic="OpenAI 发布 价格",
        )
        self.assertEqual(decision.gaps, [])
        self.assertEqual(decision.queries, [])
        self.assertEqual(decision.reason, "coverage_sufficient")

    def test_fetch_limit_stops_before_any_followup(self) -> None:
        run = PlanRun(
            status="no_evidence",
            counts={"task_physical_fetches": 6, "failures": {"fetch_limit": 3}},
        )
        decision = coverage_decision(
            run, _plan(), trusted_domains=["example.com"], max_fetch=6
        )
        self.assertEqual(decision.queries, [])
        self.assertEqual(decision.reason, "fetch_limit")

    def test_deferred_run_does_not_spend_more(self) -> None:
        run = PlanRun(status="deferred", counts={"task_physical_fetches": 1})
        decision = coverage_decision(run, _plan(), max_fetch=6)
        self.assertEqual(decision.queries, [])
        self.assertEqual(decision.reason, "deferred")


class FollowupMergeTests(unittest.TestCase):
    def test_followup_merges_unique_material_and_counts(self) -> None:
        run = PlanRun(
            status="completed",
            entries=[_entry("https://example.com/a")],
            counts={
                "raw_results": 1,
                "usable_bodies": 1,
                "task_physical_fetches": 1,
                "failures": {},
            },
            diagnostics={"queries": ["q1"], "outcomes": ["completed"]},
        )
        calls: list[str] = []

        def research(query: str, direct: str | None, label: str) -> ResearchOutcome:
            calls.append(query)
            return ResearchOutcome(
                entries=[
                    _entry("https://other.example/b"),
                    _entry("https://example.com/a"),  # duplicate must not double-count
                ],
                attempt_id=7,
                counts={
                    "raw_results": 2,
                    "usable_bodies": 2,
                    "task_physical_fetches": 1,
                    "failures": {},
                },
                diagnostics={},
                receipt="adaptive receipt",
                round_label=label,
                status="completed",
            )

        merged = run_followups(run, ["follow up 1", "follow up 2"], research)
        self.assertEqual(calls, ["follow up 1", "follow up 2"])
        self.assertEqual(merged.status, "completed")
        self.assertEqual(len(merged.entries), 2)
        self.assertEqual(merged.counts["raw_results"], 5)
        self.assertEqual(merged.counts["usable_bodies"], 5)
        self.assertEqual(merged.counts["task_physical_fetches"], 1)  # last task snapshot
        self.assertEqual(merged.diagnostics["adaptive"]["executed"], 2)
        self.assertEqual(merged.diagnostics["adaptive"]["stopped"], "max_queries")
        self.assertEqual(merged.diagnostics["queries"], ["q1", "follow up 1", "follow up 2"])
        self.assertEqual(merged.attempt_id, 7)

    def test_followup_preserves_coverage_gaps(self) -> None:
        run = PlanRun(
            status="completed",
            entries=[_entry("https://example.com/a")],
            diagnostics={
                "queries": ["q1"],
                "outcomes": ["completed"],
                "adaptive": {"gaps": ["official_source"], "queries": ["q2"]},
            },
        )

        def research(query: str, direct: str | None, label: str) -> ResearchOutcome:
            return ResearchOutcome(
                entries=[],
                attempt_id=9,
                counts={"task_physical_fetches": 1, "failures": {}},
                diagnostics={},
                receipt="adaptive receipt",
                status="completed",
            )

        merged = run_followups(run, ["follow up"], research)
        self.assertEqual(merged.diagnostics["adaptive"]["gaps"], ["official_source"])
        self.assertEqual(merged.diagnostics["adaptive"]["queries"], ["follow up"])
        self.assertEqual(merged.diagnostics["adaptive"]["executed"], 1)


if __name__ == "__main__":
    unittest.main()

"""Metric-layer tests for the offline evaluation harness."""

from __future__ import annotations

import unittest

from bot.services.agent_eval import aggregate, parse_record, render_markdown


class EvalMetricTests(unittest.TestCase):
    def test_ablation_metrics_are_defined_consistently(self) -> None:
        records = [
            parse_record(
                {
                    "case_id": "case-1",
                    "arm": "workflow",
                    "passed": False,
                    "official_found": True,
                    "claims": 10,
                    "unsupported_claims": 2,
                    "queries": 3,
                    "cost_usd": 0.02,
                    "latency_s": 30,
                    "human_edited": True,
                }
            ),
            parse_record(
                {
                    "case_id": "case-1",
                    "arm": "adaptive",
                    "passed": True,
                    "official_found": True,
                    "claims": 10,
                    "unsupported_claims": 1,
                    "queries": 5,
                    "cost_usd": 0.03,
                    "latency_s": 40,
                    "human_edited": False,
                }
            ),
            parse_record(
                {
                    "case_id": "case-1",
                    "arm": "critic",
                    "passed": True,
                    "official_found": True,
                    "claims": 10,
                    "unsupported_claims": 0,
                    "queries": 5,
                    "cost_usd": 0.04,
                    "latency_s": 55,
                    "human_edited": False,
                }
            ),
        ]
        summary = aggregate(records)
        self.assertEqual(summary["arms"]["workflow"]["first_pass_acceptance"], 0.0)
        self.assertEqual(summary["arms"]["workflow"]["unsupported_claim_rate"], 0.2)
        self.assertEqual(summary["arms"]["adaptive"]["first_pass_acceptance"], 1.0)
        self.assertEqual(summary["arms"]["critic"]["unsupported_claim_rate"], 0.0)
        self.assertEqual(summary["arms"]["workflow"]["avg_queries"], 3.0)
        markdown = render_markdown(summary)
        self.assertIn("固定工作流", markdown)
        self.assertIn("+自适应检索+审稿", markdown)

    def test_invalid_arm_and_unsupported_over_claims_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_record({"case_id": "x", "arm": "oracle"})
        with self.assertRaises(ValueError):
            parse_record(
                {"case_id": "x", "arm": "workflow", "claims": 1, "unsupported_claims": 2}
            )

    def test_empty_input_is_safe(self) -> None:
        summary = aggregate([])
        self.assertEqual(summary["comparable_arms"], [])
        self.assertEqual(summary["arms"]["workflow"]["cases"], 0)
        self.assertEqual(render_markdown(summary), "| arm | cases | first-pass | official recall | unsupported claims | human edit | avg queries | avg cost | avg latency |\n|---|---|---|---|---|---|---|---|---|")


if __name__ == "__main__":
    unittest.main()

import unittest

from bot.content import _check_numbers, _repair_unsupported_caption
from bot.adapters.types import LLMOutputError


class NumericNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.evidence = {
            "E1": {
                "quote": "Project Fortify achieved 100 per cent recall and 95 per cent precision.",
                "published_at": "2026-11-10T00:00:00Z",
            }
        }

    def test_percent_and_per_cent_are_equivalent(self):
        _check_numbers(
            "它在漏洞扫描中达到 100% 召回率、95% 精确率。",
            "storyboard.cards[1]",
            self.evidence,
            [],
            evidence_ids=["E1"],
        )

    def test_unmentioned_number_still_fails(self):
        with self.assertRaises(LLMOutputError):
            _check_numbers(
                "它在漏洞扫描中达到 101% 召回率。",
                "storyboard.cards[1]",
                self.evidence,
                [],
                evidence_ids=["E1"],
            )


class CaptionClaimRepairTests(unittest.TestCase):
    def setUp(self):
        self.evidence = {
            "E1": {
                "quote": "Hy4 flash 已接入 WorkBuddy、腾讯云 TokenHub 等产品。",
                "published_at": "2026-11-16T00:00:00Z",
            }
        }

    def test_unsupported_date_and_promo_sentence_is_removed(self):
        caption, removed, owner = _repair_unsupported_caption(
            [
                "腾讯混元发布了 Hy4 flash。",
                "现在 WorkBuddy 也能用，11月30日前 WorkBuddy 免费、API 打5折。",
            ],
            self.evidence,
            [],
            evidence_ids=["E1"],
        )
        self.assertEqual(caption, ["腾讯混元发布了 Hy4 flash。"])
        self.assertTrue(removed)
        self.assertEqual(owner, [])
        self.assertIn("11月30日", removed[0])

    def test_unsupported_promo_without_number_is_removed(self):
        caption, removed, owner = _repair_unsupported_caption(
            ["现在 WorkBuddy 免费。"], self.evidence, [], evidence_ids=["E1"]
        )
        self.assertEqual(
            caption, ["这条消息涉及的日期/优惠信息暂时无法核实，请以官方发布为准。"]
        )
        self.assertTrue(removed)
        self.assertEqual(owner, [])

    def test_owner_context_counts_as_numeric_support(self):
        _check_numbers(
            "11月30日前 WorkBuddy 免费。",
            "caption",
            {},
            [],
            evidence_ids=[],
            extra_text="11月30日前 WorkBuddy 免费",
        )
        with self.assertRaises(LLMOutputError):
            _check_numbers(
                "11月30日前 WorkBuddy 免费。",
                "caption",
                {},
                [],
                evidence_ids=[],
            )

    def test_owner_supplied_promo_is_kept_with_marker(self):
        caption, removed, owner = _repair_unsupported_caption(
            ["现在 WorkBuddy 也能用，11月30日前 WorkBuddy 免费。"],
            self.evidence,
            [],
            evidence_ids=["E1"],
            owner_context="11月30日前 WorkBuddy 免费",
        )
        self.assertEqual(caption, ["现在 WorkBuddy 也能用，11月30日前 WorkBuddy 免费。"])
        self.assertEqual(removed, [])
        self.assertEqual(owner, ["现在 WorkBuddy 也能用，11月30日前 WorkBuddy 免费。"])

    def test_evidence_backed_promo_is_kept(self):
        evidence = {"E1": {"quote": "WorkBuddy 免费。"}}
        caption, removed, owner = _repair_unsupported_caption(
            ["WorkBuddy 免费。"], evidence, [], evidence_ids=["E1"]
        )
        self.assertEqual(caption, ["WorkBuddy 免费。"])
        self.assertEqual(removed, [])
        self.assertEqual(owner, [])


if __name__ == "__main__":
    unittest.main()

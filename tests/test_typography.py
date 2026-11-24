from __future__ import annotations

import unittest

from bot.content import _check_numbers, _clean_headline, validate_draft_payload
from bot.typography import cjk_latin_tokens, normalize_zh, render_inline_html
from tests.helpers import cleanup, new_env


class TypographyTests(unittest.TestCase):
    def test_a2_chinese_punctuation_and_numbers_preserved(self) -> None:
        self.assertEqual(
            normalize_zh("98%相信是对的,但人没懂(据报道):真的?"),
            "98%相信是对的，但人没懂（据报道）：真的？",
        )
        self.assertEqual(
            normalize_zh("版本 3.5,价格 1,000 元"),
            "版本 3.5，价格 1,000 元",
        )

    def test_ellipsis_dash_quotes_and_tilde(self) -> None:
        self.assertEqual(normalize_zh("他说\"等等...\"--然后走了!"), "他说“等等……”——然后走了！")
        self.assertEqual(normalize_zh("准确率98%~99%,很稳"), "准确率98%～99%，很稳")
        self.assertEqual(normalize_zh("准确率98%~99%,很稳", fullwidth_tilde=False), "准确率98%~99%，很稳")

    def test_urls_and_pure_latin_untouched(self) -> None:
        self.assertEqual(
            normalize_zh("详情见 https://example.com/a?x=1,2&y=3:4, 原文如此."),
            "详情见 https://example.com/a?x=1,2&y=3:4，原文如此.",
        )
        self.assertEqual(
            normalize_zh("Hello, world! (see note: A; B?)"),
            "Hello, world! (see note: A; B?)",
        )

    def test_fullwidth_punctuation_compatible_with_check_numbers(self) -> None:
        env = new_env("typo-numbers")
        try:
            evidence = {
                "E1": {
                    "quote": "Version 3.5 costs 1,000 dollars and reached 98%~99% accuracy.",
                    "url": "https://example.com/v35",
                    "source_name": "Official",
                    "published_at": "2026-11-22T00:00:00Z",
                }
            }
            raw_payload = {
                "title": "版本3.5评测",
                "caption": ["版本 3.5,价格 1,000 美元,准确率98%~99%."],
                "editorial_plan": {
                    "audience_need": "了解版本变化",
                    "angle": "价格和准确率",
                    "takeaway": "3.5 准确率高",
                    "caption_image_division": "正文说明",
                    "rationale": "数据简单",
                    "strategy": "caption_led",
                    "visual_approach": "大字封面",
                },
                "storyboard": {
                    "cover": {
                        "layout": "cover",
                        "headline": "98%~99%\n准确率提升",
                        "evidence_ids": ["E1"],
                    },
                    "cards": [],
                },
                "claims": [{"text": "准确率 98%~99%", "type": "fact", "evidence_ids": ["E1"]}],
                "tags": ["AI"],
                "uncertainties": [],
            }
            content_default = validate_draft_payload(
                raw_payload,
                evidence_by_id=evidence,
                config=env.config,
                require_storyboard=True,
                include_sources_in_body=False,
                public_disclaimers=False,
            )
            self.assertEqual(content_default.caption[0], "版本 3.5，价格 1,000 美元，准确率98%~99%.")
            self.assertEqual(content_default.storyboard.cover.headline, "98%~99%\n准确率提升")
            _check_numbers(" ".join(content_default.caption), "caption", evidence, content_default.claims)

            env.config.data.setdefault("typography", {})["fullwidth_tilde"] = True
            content_tilde = validate_draft_payload(
                raw_payload,
                evidence_by_id=evidence,
                config=env.config,
                require_storyboard=True,
                include_sources_in_body=False,
                public_disclaimers=False,
            )
            self.assertEqual(content_tilde.caption[0], "版本 3.5，价格 1,000 美元，准确率98%～99%.")
            self.assertEqual(content_tilde.storyboard.cover.headline, "98%～99%\n准确率提升")
            _check_numbers(" ".join(content_tilde.caption), "caption", evidence, content_tilde.claims)
        finally:
            cleanup(env)

    def test_clean_headline_limits_newlines_and_tokens_protect_nb(self) -> None:
        self.assertEqual(_clean_headline("第一行\n第二行\n第三行\n第四行", "cover.headline"), "第一行\n第二行\n第三行 第四行")
        tokens = cjk_latin_tokens("1700 万行 Lean 代码和 Claude 4.5")
        nbs = [seg for seg, kind in tokens if kind != "text"]
        self.assertIn("1700 万行", nbs)
        self.assertIn("Lean", nbs)
        self.assertIn("Claude 4.5", nbs)
        rendered = render_inline_html("信Claude的<script>证明", emphasis=["Claude"])
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;", rendered)
        self.assertIn("&gt;", rendered)
        self.assertIn("<mark>", rendered)
        self.assertIn('<span class="nb', rendered)


if __name__ == "__main__":
    unittest.main()

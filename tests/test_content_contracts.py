from __future__ import annotations

import unittest

from bot.adapters.types import LLMOutputError
from bot.content import DraftContent, validate_draft_payload
from tests.helpers import cleanup, new_env


def _base_payload(**overrides):
    data = {
        "title": "新布局测试",
        "caption": ["这是一条测试正文，数据来自官方公告 98% 和 11月20日。"],
        "editorial_plan": {
            "audience_need": "了解新能力",
            "angle": "核心数据",
            "takeaway": "准确率 98%",
            "caption_image_division": "图片放结构，正文讲边界",
            "rationale": "有数据和时间线",
            "strategy": "balanced",
            "visual_approach": "统计卡片加时间线",
            "opening_move": "一个数字",
            "visual_theme": "editorial",
        },
        "storyboard": {
            "cover": {
                "layout": "cover",
                "variant": "headline",
                "headline": "准确率\n98%",
                "evidence_ids": ["E1"],
            },
            "cards": [],
        },
        "claims": [{"text": "准确率 98%", "type": "fact", "evidence_ids": ["E1"]}],
        "tags": ["AI"],
        "uncertainties": [],
    }
    data.update(overrides)
    return data


class ContentContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = new_env("contracts")
        self.evidence = {
            "E1": {
                "quote": "2026年11月20日官方公告称准确率达到 98%，耗时 12 年，共 940 页。梅纳德表示：真正关键的是人的理解。",
                "url": "https://example.com/official",
                "source_name": "官方公告",
                "source_role": "official",
                "published_at": "2026-11-20T00:00:00Z",
            },
            "E2": {
                "quote": "媒体报道提到：还有待独立验证，11月21日仍未认可。",
                "url": "https://example.com/media",
                "source_name": "科技日报",
                "source_role": "secondary",
                "published_at": "2026-11-21T00:00:00Z",
            },
        }

    def tearDown(self) -> None:
        cleanup(self.env)

    def _validate(self, payload):
        return validate_draft_payload(
            payload,
            evidence_by_id=self.evidence,
            config=self.env.config,
            require_storyboard=True,
            include_sources_in_body=False,
            public_disclaimers=False,
        )

    def test_a3_new_layouts_positive_and_negative(self) -> None:
        self.env.config.data["writer"]["storyboard_max_cards"] = 6
        payload = _base_payload(
            storyboard={
                "cover": {
                    "layout": "cover",
                    "variant": "stat",
                    "headline": "关键数字一览",
                    "stats": [{"value": "98", "unit": "%", "label": "准确率"}],
                    "emphasis": ["关键数字", "不存在的子串"],
                    "icon": "sparkles",
                    "evidence_ids": ["E1"],
                },
                "cards": [
                    {
                        "layout": "stat",
                        "headline": "核心规模",
                        "stats": [
                            {"value": "98", "unit": "%", "label": "准确率"},
                            {"value": "940", "unit": "页", "label": "草稿页数"},
                        ],
                        "evidence_ids": ["E1"],
                    },
                    {
                        "layout": "quote",
                        "headline": "研究者原话",
                        "subhead": "真正关键的是人的理解",
                        "attribution": "梅纳德，据报道",
                        "evidence_ids": ["E2"],
                    },
                    {
                        "layout": "timeline",
                        "headline": "时间线",
                        "points": ["11月20日 公开声明", "11月21日 仍未认可"],
                        "evidence_ids": ["E1", "E2"],
                    },
                    {
                        "layout": "checklist",
                        "headline": "适用清单",
                        "columns": [
                            {"label": "已支持", "text": "官方准确率 98%"},
                            {"label": "未完成", "text": "学术界独立验证"},
                        ],
                        "evidence_ids": ["E1", "E2"],
                    },
                    {
                        "layout": "qa",
                        "headline": "常见疑问",
                        "points": [
                            "准确率多少？｜98%",
                            "现在算通过了吗？｜还没有独立验证",
                        ],
                        "evidence_ids": ["E1", "E2"],
                    },
                ],
            }
        )
        content = self._validate(payload)
        self.assertEqual(content.storyboard.cover.variant, "stat")
        self.assertEqual(content.storyboard.cover.emphasis, ["关键数字"])
        self.assertEqual(content.storyboard.cover.icon, "sparkles")
        self.assertEqual(
            [c.layout for c in content.storyboard.cards],
            ["stat", "quote", "timeline", "checklist", "qa"],
        )

        # Negative: stat missing stats
        bad_stat = _base_payload(storyboard={
            "cover": {"layout": "cover", "headline": "标题", "evidence_ids": ["E1"]},
            "cards": [{"layout": "stat", "headline": "空数字", "stats": [], "evidence_ids": ["E1"]}],
        })
        with self.assertRaises(LLMOutputError):
            self._validate(bad_stat)

        # Negative: stat unsupported number -> LLMOutputError
        bad_stat_num = _base_payload(storyboard={
            "cover": {"layout": "cover", "headline": "标题", "evidence_ids": ["E1"]},
            "cards": [{
                "layout": "stat",
                "headline": "编造数字",
                "stats": [{"value": "999", "unit": "%", "label": "虚假提升"}],
                "evidence_ids": ["E1"],
            }],
        })
        with self.assertRaises(LLMOutputError):
            self._validate(bad_stat_num)

        # Negative: quote missing attribution or missing reporting tag on secondary source
        bad_quote = _base_payload(storyboard={
            "cover": {"layout": "cover", "headline": "标题", "evidence_ids": ["E1"]},
            "cards": [{
                "layout": "quote",
                "headline": "原话",
                "subhead": "还有待独立验证",
                "attribution": "某媒体",
                "evidence_ids": ["E2"],
            }],
        })
        with self.assertRaises(LLMOutputError):
            self._validate(bad_quote)

        # Negative: timeline without leading date
        bad_timeline = _base_payload(storyboard={
            "cover": {"layout": "cover", "headline": "标题", "evidence_ids": ["E1"]},
            "cards": [{
                "layout": "timeline",
                "headline": "时间线",
                "points": ["首先公布声明", "然后等待验证"],
                "evidence_ids": ["E1"],
            }],
        })
        with self.assertRaises(LLMOutputError):
            self._validate(bad_timeline)

        # Negative: checklist wrong columns count
        bad_checklist = _base_payload(storyboard={
            "cover": {"layout": "cover", "headline": "标题", "evidence_ids": ["E1"]},
            "cards": [{
                "layout": "checklist",
                "headline": "清单",
                "columns": [{"label": "只有一列", "text": "不够"}],
                "evidence_ids": ["E1"],
            }],
        })
        with self.assertRaises(LLMOutputError):
            self._validate(bad_checklist)

        # Negative: qa without pipe delimiter
        bad_qa = _base_payload(storyboard={
            "cover": {"layout": "cover", "headline": "标题", "evidence_ids": ["E1"]},
            "cards": [{
                "layout": "qa",
                "headline": "问答",
                "points": ["没有分隔符", "第二条也没有"],
                "evidence_ids": ["E1"],
            }],
        })
        with self.assertRaises(LLMOutputError):
            self._validate(bad_qa)

    def test_a3_aesthetic_fields_downgrade_silently(self) -> None:
        payload = _base_payload()
        payload["editorial_plan"]["visual_theme"] = "nonexistent-theme"
        payload["storyboard"]["cover"]["variant"] = "unknown-variant"
        payload["storyboard"]["cover"]["emphasis"] = ["根本不在标题里"]
        payload["storyboard"]["cover"]["icon"] = "not-a-lucide-icon"
        content = self._validate(payload)
        self.assertEqual(content.editorial_plan.visual_theme, "")
        self.assertEqual(content.storyboard.cover.variant, "headline")
        self.assertEqual(content.storyboard.cover.emphasis, [])
        self.assertEqual(content.storyboard.cover.icon, "")

    def test_a10_old_storyboard_deserializes_cleanly(self) -> None:
        old_dict = {
            "title": "旧版草稿",
            "sections": [],
            "claims": [],
            "tags": ["AI"],
            "uncertainties": [],
            "sources": [],
            "caption": ["正文一段"],
            "editorial_plan": {
                "audience_need": "需求",
                "angle": "角度",
                "takeaway": "结论",
                "caption_image_division": "分工",
                "rationale": "原因",
                "strategy": "balanced",
                "visual_approach": "封面加要点",
            },
            "storyboard": {
                "cover": {
                    "layout": "cover",
                    "headline": "旧封面",
                    "kicker": "标签",
                    "subhead": "副题",
                    "note": "注",
                    "evidence_ids": ["E1"],
                },
                "cards": [
                    {
                        "layout": "keypoints",
                        "headline": "要点",
                        "points": ["第一条", "第二条"],
                        "evidence_ids": ["E1"],
                    }
                ],
            },
        }
        content = DraftContent.from_dict(old_dict)
        self.assertEqual(content.storyboard.cover.variant, "headline")
        self.assertEqual(content.storyboard.cover.stats, [])
        self.assertEqual(content.storyboard.cards[0].emphasis, [])
        self.assertEqual(content.storyboard.cards[0].icon, "")


if __name__ == "__main__":
    unittest.main()

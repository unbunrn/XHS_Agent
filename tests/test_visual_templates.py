from __future__ import annotations

import copy
import unittest

from bot.content import DraftContent, EditorialPlan, ImageCard, ImageColumn, ImageStoryboard
from bot.visual.selector import load_recent, record_recent, select_theme_and_palette
from bot.visual.templates import build_card_html
from bot.visual.themes import THEMES, Palette, Theme, available_themes, resolve_theme
from tests.helpers import cleanup, new_env


class VisualTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = new_env("visual-tpl")
        self.env.db.init()

    def tearDown(self) -> None:
        cleanup(self.env)

    def test_a6_html_escaping_prevents_script_and_external_img(self) -> None:
        theme = resolve_theme(THEMES["editorial"], self.env.config)
        self.assertTrue(theme.available)
        card = ImageCard(
            layout="cover",
            headline='危险标题 <script>alert(1)</script> <img src="http://x">',
            kicker='副签 <script>bad()</script>',
            subhead='副题 <img src="http://evil.invalid/a.png">',
            note='注 <script>n()</script>',
            evidence_ids=["E1"],
        )
        html_doc = build_card_html(
            card,
            theme=theme,
            palette=theme.palettes[0],
            page=1,
            total=1,
            account_label="z-Morty",
            run_key="run-xss",
        )
        self.assertNotIn("<script>", html_doc)
        self.assertNotIn('<img src="http://x">', html_doc)
        self.assertIn("&lt;", html_doc)
        self.assertIn("&gt;", html_doc)
        self.assertIn("evil.invalid", html_doc)

    def test_all_12_layouts_and_5_cover_variants_render_html(self) -> None:
        theme = resolve_theme(THEMES["swiss"], self.env.config)
        palette = theme.palettes[0]
        covers = [
            ImageCard(layout="cover", variant="headline", headline="大字封面\n第二行", emphasis=["大字"], icon="sparkles", evidence_ids=["E1"]),
            ImageCard(layout="cover", variant="stat", headline="关键指标", stats=[{"value": "98", "unit": "%", "label": "准确率"}], evidence_ids=["E1"]),
            ImageCard(layout="cover", variant="quote", headline="原话引述", subhead="真正关键的是人的理解", attribution="梅纳德，据报道", evidence_ids=["E1"]),
            ImageCard(layout="cover", variant="question", headline="AI真能证明吗？", evidence_ids=["E1"]),
            ImageCard(layout="cover", variant="split", headline="已公开｜未验证", subhead="两面看", evidence_ids=["E1"]),
        ]
        for idx, c in enumerate(covers, 1):
            doc = build_card_html(c, theme=theme, palette=palette, page=1, total=2, account_label="z-Morty", run_key=f"cov-{idx}")
            self.assertIn("theme-swiss", doc)
        cards = [
            ImageCard(layout="comparison", headline="对比", columns=[ImageColumn("A", "左边"), ImageColumn("B", "右边")], evidence_ids=["E1"]),
            ImageCard(layout="process", headline="流程", points=["第一步", "第二步"], evidence_ids=["E1"]),
            ImageCard(layout="keypoints", headline="要点", points=["要点一", "要点二"], evidence_ids=["E1"]),
            ImageCard(layout="diagram", headline="层级", points=["输入层", "输出层"], evidence_ids=["E1"]),
            ImageCard(layout="argument", headline="主张", points=["理由一", "理由二"], evidence_ids=["E1"]),
            ImageCard(layout="annotated", headline="注解", subhead="来源摘录句", points=["注释一"], evidence_ids=["E1"]),
            ImageCard(layout="stat", headline="统计", stats=[{"value": "1700", "unit": "万行", "label": "代码量"}], evidence_ids=["E1"]),
            ImageCard(layout="quote", headline="引语", subhead="人类理解优先", attribution="梅纳德", evidence_ids=["E1"]),
            ImageCard(layout="timeline", headline="时间线", points=["11月20日 公布", "11月21日 讨论"], evidence_ids=["E1"]),
            ImageCard(layout="checklist", headline="清单", columns=[ImageColumn("适合", "批处理"), ImageColumn("不适合", "一次性")], evidence_ids=["E1"]),
            ImageCard(layout="qa", headline="问答", points=["是什么？｜工具", "收费吗？｜按量"], evidence_ids=["E1"]),
        ]
        for idx, card in enumerate(cards, 1):
            doc = build_card_html(card, theme=theme, palette=palette, page=idx, total=len(cards), account_label="z-Morty", run_key=f"card-{idx}")
            self.assertIn("<!doctype html>", doc)

    def test_a7_theme_cooldown_palette_rotation_and_missing_font_skip(self) -> None:
        # Cooldown = 2: consecutive 6 picks never repeat any theme within adjacent 3 picks
        recent: list[dict] = []
        picked: list[str] = []
        for i in range(6):
            theme, pal = select_theme_and_palette(
                run_key=f"run-rot-{i}",
                layouts={"cover", "keypoints"},
                recent=recent,
                config=self.env.config,
            )
            picked.append(theme.id)
            if i >= 1:
                self.assertNotEqual(theme.id, picked[-2])
            if i >= 2:
                self.assertNotEqual(theme.id, picked[-3])
            recent.append({"run_key": f"run-rot-{i}", "theme": theme.id, "palette": pal.name})

        # Same theme twice in a row (with cooldown=0): palette rotates
        self.env.config.data["render"]["theme_cooldown"] = 0
        t1, p1 = select_theme_and_palette(
            run_key="same-1", explicit_theme="editorial", recent=[], config=self.env.config
        )
        t2, p2 = select_theme_and_palette(
            run_key="same-1",
            explicit_theme="editorial",
            recent=[{"run_key": "prev", "theme": "editorial", "palette": p1.name}],
            config=self.env.config,
        )
        self.assertEqual(t1.id, t2.id)
        self.assertNotEqual(p1.name, p2.name)

        # Missing font theme is skipped
        registry = copy.deepcopy(THEMES)
        registry["broken"] = Theme(
            id="broken",
            name="缺失字体",
            summary="missing",
            css_file="editorial.css",
            tags=("cover",),
            palettes=(THEMES["editorial"].palettes[0],),
            fonts={"display": ("NoSuchFont_12345.ttf",), "body": ("NoSuchFont_67890.ttf",)},
        )
        avail_ids = {t.id for t in available_themes(self.env.config, registry=registry)}
        self.assertNotIn("broken", avail_ids)
        self.assertIn("editorial", avail_ids)

    def test_recent_ring_buffer_persists_in_settings(self) -> None:
        for i in range(15):
            record_recent(
                self.env.db,
                {"run_key": f"rk-{i}", "theme": "editorial", "palette": "terracotta"},
                clock=self.env.clock,
            )
        rows = load_recent(self.env.db)
        self.assertEqual(len(rows), 12)
        self.assertEqual(rows[0]["run_key"], "rk-3")
        self.assertEqual(rows[-1]["run_key"], "rk-14")


if __name__ == "__main__":
    unittest.main()

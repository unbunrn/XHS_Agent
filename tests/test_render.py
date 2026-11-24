"""PNG rendering and preview/final byte-binding tests."""

# =============================================================================
# 中文导读：你预览到的，就是最终发出去的
# =============================================================================
# 平台不接受“先看预览、发布时再重新生成”的做法——重新生成可能得到不同结果。
# 所以这里验证三件事：
#   1. 每张 PNG 都能真正打开、尺寸 1080x1440、文件哈希与数据库一致；
#   2. 批准后导出的正文和图片哈希，与草稿里被批准的那一份完全相同；
#   3. 长文会自动分成多张卡片；超过上限时报错，而不是偷偷截断内容。
# =============================================================================

from __future__ import annotations

import json
import unittest
from pathlib import Path

from PIL import Image

from bot.adapters.publishers import MockPublisher
from bot.content import DraftContent, Section, SourceRef
from bot.render import RenderError, _Line, _paginate, _wrap, render_draft
from bot.services import publish
from tests.helpers import approve, cleanup, make_draft, new_env


class RenderTests(unittest.TestCase):
    def test_png_opens_matches_hashes_and_preview_is_final(self) -> None:
        # 第一段：验证图片文件本身。
        # Image.verify() 能发现“下载/写入时损坏”的 PNG；
        # 再打开一次读尺寸，确认不是占位图；
        # 最后把文件字节的 sha256 和数据库记录对比。
        env = new_env("render")
        try:
            draft = make_draft(env)
            self.assertGreaterEqual(len(draft["images"]), 1)
            for image in draft["images"]:
                path = Path(image["path"])
                self.assertTrue(path.exists())
                self.assertEqual(path.suffix, ".png")
                with Image.open(path) as img:
                    img.verify()
                with Image.open(path) as img:
                    self.assertEqual(img.size, (1080, 1440))
                import hashlib

                self.assertEqual(
                    hashlib.sha256(path.read_bytes()).hexdigest(), image["sha256"]
                )
            self.assertTrue(
                env.db.scalar(
                    "SELECT COUNT(*) FROM draft_images WHERE draft_id=?",
                    (draft["draft"]["id"],),
                )
                == len(draft["images"])
            )
            approve(env, draft["approval_code"])
            result = publish.publish_approval(
                env.db,
                env.config,
                env.clock,
                draft["approval_id"],
                publisher=MockPublisher(env.config, env.clock),
            )
            self.assertEqual(result["state"], "simulated")
            attempt = env.db.fetchone("SELECT * FROM publish_attempts")
            receipt = json.loads(attempt["receipt_json"])
            manifest = json.loads(Path(receipt["manifest_path"]).read_text(encoding="utf-8"))
            draft_row = env.db.fetchone(
                "SELECT * FROM drafts WHERE id=?", (draft["draft"]["id"],)
            )
            # The exact approved text and image bytes are what the export got.
            # 中文：导出的 manifest 必须逐字节对应“被批准的那一份”，
            # 而不是重新排版/重新生成后的另一份。
            self.assertEqual(manifest["body"], draft_row["body"])
            self.assertEqual(manifest["body_sha256"], draft_row["body_sha256"])
            self.assertEqual(
                [i["sha256"] for i in manifest["images"]],
                [i["sha256"] for i in draft["images"]],
            )
            exported = sorted(Path(env.config.resolve_path("var/exports")).glob("*.png"))
            self.assertTrue(exported)
            # 导出目录里的 PNG 与草稿目录里的源文件字节完全相同。
            self.assertEqual(
                exported[0].read_bytes(),
                Path(draft["images"][0]["path"]).read_bytes(),
            )
        finally:
            cleanup(env)

    def test_latin_words_are_not_split_when_they_fit(self) -> None:
        class _FakeDraw:
            def textlength(self, text, font=None):
                return len(text) * 10

        lines = _wrap(_FakeDraw(), "使用 Fetch 和 RAG 以及 lang 参数", None, 60)
        for word in ("Fetch", "RAG", "lang"):
            self.assertTrue(any(word in line for line in lines), (word, lines))

    def test_section_heading_is_not_orphaned_at_page_bottom(self) -> None:
        heading = _Line("【限制与不确定性】", None, 30, keep_with_next=True)
        body = _Line("第一条正文内容", None, 30)
        filler = _Line("filler", None, 50)
        pages = _paginate(
            [heading, body],
            available=100,
            cover=[filler],
            continuation_header=lambda: [],
            max_cards=5,
        )
        self.assertEqual([line.text for line in pages[0]], ["filler"])
        self.assertEqual(pages[1][0].text, "【限制与不确定性】")
        self.assertEqual(pages[1][1].text, "第一条正文内容")

    def test_long_text_splits_without_truncation(self) -> None:
        # 四个章节、每章一段很长的文字：必须自动分页成至少 2 张，
        # 但也不能超过 render.max_cards（默认 6）张。
        env = new_env("split")
        try:
            long_paragraph = (
                "这是一段用于验证自动分页的中文内容，说明工具的能力边界、价格、适用人群和限制。"
                "读者需要先核对官方文档，再用小任务测试，不要直接迁移生产流程。"
            ) * 4
            content = DraftContent(
                title="长文自动分页验证",
                sections=[
                    Section("what", "发生了什么", [long_paragraph]),
                    Section("audience", "对谁有用", [long_paragraph]),
                    Section("how", "具体怎么用", [long_paragraph]),
                    Section("limits", "限制与不确定性", [long_paragraph]),
                ],
                claims=[],
                tags=[],
                uncertainties=[],
                sources=[
                    SourceRef(
                        evidence_id="e1",
                        name="测试来源",
                        url="https://example.com/source",
                        published_at=None,
                    )
                ],
                generator="test",
                model="test",
            )
            images = render_draft(
                content,
                out_dir=env.root / "long",
                config=env.config,
                draft_key="long",
            )
            self.assertGreaterEqual(len(images), 2)
            self.assertLessEqual(len(images), int(env.config.get("render.max_cards", 6)))
            for image in images:
                with Image.open(image.path) as img:
                    img.verify()
        finally:
            cleanup(env)

    def test_too_many_pages_raises_instead_of_truncating(self) -> None:
        # 把上限压到 1 张，内容却有四章很长的文字。
        # 正确行为是抛 RenderError 让人处理，而不是悄悄删掉后半篇——
        # 被截断的内容一旦发出去就收不回来了。
        env = new_env("overflow")
        try:
            env.config.data["render"]["max_cards"] = 1
            content = DraftContent(
                title="分页上限",
                sections=[
                    Section("what", "发生了什么", ["很长的内容。" * 80]),
                    Section("audience", "对谁有用", ["很长的内容。" * 80]),
                    Section("how", "具体怎么用", ["很长的内容。" * 80]),
                    Section("limits", "限制与不确定性", ["很长的内容。" * 80]),
                ],
                claims=[],
                tags=[],
                uncertainties=[],
                sources=[],
                generator="test",
                model="test",
            )
            with self.assertRaises(RenderError):
                render_draft(
                    content,
                    out_dir=env.root / "overflow",
                    config=env.config,
                    draft_key="overflow",
                )
        finally:
            cleanup(env)


class RenderFallbackAndLayoutsTests(unittest.TestCase):
    def setUp(self) -> None:
        import shutil
        import tempfile
        from pathlib import Path

        from tests.helpers import make_config

        self._shutil = shutil
        self.tmp = Path(tempfile.mkdtemp(prefix="xhs-render-ext-"))
        self.cfg = make_config(self.tmp)
        self.cfg.data["render"]["engine"] = "pillow"
        self.cfg.data["render"]["max_cards"] = 6

    def tearDown(self) -> None:
        self._shutil.rmtree(self.tmp, ignore_errors=True)

    def _six_layout_content(self):
        from bot.content import DraftContent

        return DraftContent.from_dict(
            {
                "title": "六个新版式全覆盖",
                "caption": ["正文说明。"],
                "tags": ["测试"],
                "storyboard": {
                    "cover": {"layout": "cover", "kicker": "实测汇总", "headline": "关键变化", "subhead": "六个板块"},
                    "cards": [
                        {"layout": "stat", "headline": "核心数字", "stats": [{"value": "98%", "unit": "置信", "label": "作者估算"}]},
                        {"layout": "quote", "headline": "原话摘录", "subhead": "没有人真正读完之前，证明还不算完成。", "attribution": "James Maynard"},
                        {"layout": "timeline", "headline": "关键节点", "points": ["03月 公布初步进展", "04月 完整仓库开源"]},
                        {"layout": "checklist", "headline": "谁该跟进", "columns": [{"label": "建议试", "text": "做代码审计的研究团队"}, {"label": "可跳过", "text": "日常办公写作场景"}]},
                        {"layout": "qa", "headline": "常见疑问", "points": ["普通人能跑吗？｜需要本地 16GB 显存起步"]},
                    ],
                },
                "claims": [{"type": "fact", "text": "包含 98% 与 16GB 参数。", "evidence_ids": ["E1"], "core": True}],
            }
        )

    def test_a9_pillow_engine_renders_all_new_layouts_and_footer_modes(self) -> None:
        content = self._six_layout_content()
        for mode in ("last", "every", "none"):
            self.cfg.data["render"]["footer_disclaimer"] = mode
            imgs = render_draft(
                content,
                out_dir=self.tmp / f"out-{mode}",
                config=self.cfg,
                draft_key=f"pillow-{mode}",
            )
            self.assertEqual(len(imgs), 6)
            for img in imgs:
                self.assertEqual((img.width, img.height), (1080, 1440))
                self.assertTrue(img.path.is_file())

    def test_a10_browser_launch_error_fallback_vs_none_and_overflow(self) -> None:
        from unittest.mock import patch
        from bot.visual.engine import BrowserLaunchError

        content = self._six_layout_content()
        self.cfg.data["render"]["engine"] = "html"
        self.cfg.data["render"]["fallback_engine"] = "pillow"

        with patch("bot.visual.engine.render_storyboard_html", side_effect=BrowserLaunchError("no edge")):
            imgs = render_draft(content, out_dir=self.tmp / "fb", config=self.cfg, draft_key="fb")
        self.assertEqual(len(imgs), 6)

        self.cfg.data["render"]["fallback_engine"] = "none"
        with patch("bot.visual.engine.render_storyboard_html", side_effect=BrowserLaunchError("no edge")):
            with self.assertRaises(BrowserLaunchError):
                render_draft(content, out_dir=self.tmp / "fb-off", config=self.cfg, draft_key="fb-off")

        self.cfg.data["render"]["fallback_engine"] = "pillow"
        with patch("bot.visual.engine.render_storyboard_html", side_effect=RenderError("card 2: slot overflows")):
            with self.assertRaises(RenderError):
                render_draft(content, out_dir=self.tmp / "overflow", config=self.cfg, draft_key="overflow")


if __name__ == "__main__":
    unittest.main()

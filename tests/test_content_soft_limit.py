import unittest

from bot.content import DraftContent, SourceRef, _fit_rendered_body


class SoftBodyLimitTests(unittest.TestCase):
    def test_over_limit_caption_is_trimmed_with_warning_and_sources_kept(self):
        content = DraftContent(
            title="测试",
            sections=[],
            claims=[],
            tags=[],
            uncertainties=[],
            sources=[
                SourceRef(
                    evidence_id="E1",
                    name="官方文档",
                    url="https://example.com/docs",
                    published_at="2026-11-16",
                    date_precision="date",
                )
            ],
            caption=["这是一段很长的正文。" * 200],
        )

        _fit_rendered_body(content, 220)

        body = content.render_body()
        self.assertLessEqual(len(body), 220)
        self.assertIn("自动压缩", body)
        self.assertIn("https://example.com/docs", body)
        self.assertTrue(any("自动压缩" in item for item in content.uncertainties))

    def test_public_body_can_omit_source_urls(self):
        content = DraftContent(
            title="测试",
            sections=[],
            claims=[],
            tags=[],
            uncertainties=[],
            sources=[
                SourceRef(
                    evidence_id="E1",
                    name="官方",
                    url="https://example.com/private-source",
                    date_precision="unknown",
                )
            ],
            caption=["这是一条小红书正文，不贴来源网址。"],
        )
        public = content.render_body(include_sources=False)
        self.assertNotIn("https://", public)
        self.assertNotIn("【来源】", public)
        self.assertEqual(content.sources[0].url, "https://example.com/private-source")

    def test_short_body_is_unchanged(self):
        content = DraftContent(
            title="测试",
            sections=[],
            claims=[],
            tags=[],
            uncertainties=[],
            sources=[
                SourceRef(
                    evidence_id="E1",
                    name="官方",
                    url="https://example.com",
                    date_precision="unknown",
                )
            ],
            caption=["短正文。"],
        )
        before = content.render_body()
        _fit_rendered_body(content, 1000)
        self.assertEqual(before, content.render_body())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from bot.adapters.llm import LLMOutputError, _parse_json


class WriterJsonRepairTests(unittest.TestCase):
    def test_repairs_raw_newlines_and_unescaped_quotes_inside_body(self) -> None:
        broken = (
            '{"title":"测试标题","body":"第一行\n第二行 他说"你好"然后继续",'
            '"tags":["a",]}'
        )
        data = _parse_json(broken)
        self.assertEqual(data["title"], "测试标题")
        self.assertEqual(data["body"], '第一行\n第二行 他说"你好"然后继续')
        self.assertEqual(data["tags"], ["a"])

    def test_valid_json_is_unchanged(self) -> None:
        text = '{"title":"标题","body":"正文","tags":["a","b"]}'
        self.assertEqual(_parse_json(text)["body"], "正文")

    def test_unrepairable_structural_error_still_raises(self) -> None:
        with self.assertRaises(LLMOutputError):
            _parse_json('{"title": }')

    def test_markdown_fence_is_stripped(self) -> None:
        text = '```json\n{"title":"标题","body":"正文"}\n```'
        self.assertEqual(_parse_json(text)["title"], "标题")


if __name__ == "__main__":
    unittest.main()

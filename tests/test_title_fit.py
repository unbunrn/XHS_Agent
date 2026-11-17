"""Regression tests for the platform title cap.

The Xiaohongshu title field is limited to 20 characters.  A 21-22 character
model title must not abort an otherwise valid draft; the deterministic fitter
shortens it with one ellipsis character.
"""

from __future__ import annotations

import unittest

from bot.adapters.types import LLMOutputError
from bot.content import _fit_title, validate_draft_payload
from tests.helpers import cleanup, new_env


class TitleFitTests(unittest.TestCase):
    def test_overlong_title_is_shortened_not_rejected(self) -> None:
        title = "Anthropic 发布 Fable 5.2 与 Mythos 5.2"
        self.assertGreater(len(title), 20)
        fitted = _fit_title(title, 20)
        self.assertLessEqual(len(fitted), 20)
        self.assertTrue(fitted.endswith("…"))
        self.assertFalse(fitted.endswith(" …"))

    def test_exact_limit_is_unchanged(self) -> None:
        title = "Anthropic Fable 5.2"
        self.assertLessEqual(len(title), 20)
        self.assertEqual(_fit_title(title, 20), title)

    def test_tiny_limit_keeps_one_character(self) -> None:
        self.assertEqual(_fit_title("标题", 1), "标")
        self.assertEqual(_fit_title("标题", 0), "")

    def test_validation_does_not_reject_long_title(self) -> None:
        env = new_env("title_fit_validation")
        try:
            with self.assertRaises(LLMOutputError) as caught:
                validate_draft_payload(
                    {"title": "Anthropic 发布 Fable 5.2 与 Mythos 5.2"},
                    evidence_by_id={},
                    config=env.config,
                )
            self.assertNotIn("title is", str(caught.exception))
        finally:
            cleanup(env)


if __name__ == "__main__":
    unittest.main()

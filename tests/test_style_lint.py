from __future__ import annotations

import json
import unittest
from pathlib import Path

from bot.content import DraftContent
from bot.style_lint import RecentPost, lint_draft, title_skeleton
from bot.voice import Voice, load_voice
from tests.helpers import PROJECT_ROOT, cleanup, new_env


class StyleLintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.samples = json.loads(
            (PROJECT_ROOT / "fixtures" / "style_samples.json").read_text(encoding="utf-8")
        )
        env = new_env("style-lint")
        self.env = env
        self.voice = load_voice(env.config)

    def tearDown(self) -> None:
        cleanup(self.env)

    def test_voice_loads_and_samples_deterministically(self) -> None:
        self.assertIn("z-Morty", self.voice.persona)
        a = self.voice.as_prompt_dict("run-abc", recent_openings=["直接给结论：第一句就是读者最该知道的事"])
        b = self.voice.as_prompt_dict("run-abc", recent_openings=["直接给结论：第一句就是读者最该知道的事"])
        self.assertEqual(a, b)
        self.assertEqual(len(a["examples"]["good"]), 2)
        self.assertEqual(len(a["examples"]["bad"]), 2)
        self.assertEqual(len(a["opening_moves"]), 3)
        self.assertNotIn("直接给结论：第一句就是读者最该知道的事", a["opening_moves"])

    def test_voice_missing_file_uses_defaults(self) -> None:
        self.env.config.data["writer"]["voice_path"] = "config/nonexistent-voice.toml"
        v = load_voice(self.env.config)
        self.assertEqual(v.source, "defaults")
        self.assertGreaterEqual(len(v.cliches), 10)

    def test_a4_real_negatives_and_positives(self) -> None:
        by_id = {item["id"]: item for item in self.samples["negatives"]}
        for draft_id in (49, 50, 51):
            item = by_id[draft_id]
            content = DraftContent.from_dict(item["body_json"])
            report = lint_draft(content, self.voice, [])
            self.assertGreaterEqual(
                len(report.issues),
                3,
                f"draft #{draft_id} expected >=3 issues, got {report.issues}",
            )
            codes = {issue["code"] for issue in report.issues}
            snippets = {issue["snippet"] for issue in report.issues}
            if draft_id in (49, 51):
                self.assertIn("cliche", codes)
                self.assertIn("看到外媒报道", snippets)
            if draft_id == 49:
                self.assertIn("summary_close", codes)

        for item in self.samples["positives"]:
            content = DraftContent.from_dict(item["body_json"])
            report = lint_draft(content, self.voice, [])
            self.assertLessEqual(
                report.score,
                self.voice.max_score,
                f"{item['id']} score={report.score} issues={report.issues}",
            )

    def test_a4_recent_title_skeleton_and_opening(self) -> None:
        by_id = {item["id"]: item for item in self.samples["negatives"]}
        d49 = DraftContent.from_dict(by_id[49]["body_json"])
        d50 = by_id[50]
        self.assertEqual(title_skeleton(d49.title), title_skeleton(d50["title"]))
        recent = [
            RecentPost(
                title=d50["title"],
                opening=d50["body_json"]["caption"][0],
                cover_headline=d50["body_json"]["storyboard"]["cover"]["headline"],
            )
        ]
        report = lint_draft(d49, self.voice, recent)
        codes = {issue["code"] for issue in report.issues}
        self.assertIn("recent_title_skeleton", codes)
        self.assertIn("recent_opening", codes)
        self.assertIn("question_title_overuse", codes)

    def test_quote_exemption_and_rhythm_rules(self) -> None:
        content = DraftContent.from_dict(
            {
                "title": "新发布来了",
                "caption": [
                    "他说“看到外媒报道，真是新纪元”，这句话我记下来了。",
                    "其实不是效率提高，而是成本降低。",
                    "其实不是界面好看，而是速度变快。",
                ],
                "storyboard": {
                    "cover": {"layout": "cover", "headline": "实测数据对比"},
                    "cards": [
                        {
                            "layout": "quote",
                            "headline": "原话引用",
                            "subhead": "看到外媒报道真的挺有意思",
                            "attribution": "据官方采访",
                        }
                    ],
                },
            }
        )
        report = lint_draft(content, self.voice, [])
        codes = {issue["code"] for issue in report.issues}
        self.assertNotIn("cliche", codes)
        self.assertIn("not_but", codes)


if __name__ == "__main__":
    unittest.main()

"""Bounded Editor/Critic tests.

The critic is a quality layer: it can force one rewrite, but it must never
bypass deterministic validation or the human approval gate.
"""

from __future__ import annotations

import json
import unittest

from bot.adapters.llm import FakeLLM
from bot.adapters.types import LLMResponse, TokenUsage
from bot.services.drafts import DraftError
from tests.helpers import cleanup, make_draft, new_env


def _response(data: dict) -> LLMResponse:
    return LLMResponse(
        data=data,
        raw_text=json.dumps(data, ensure_ascii=False),
        usage=TokenUsage(),
        latency_ms=0,
        model="scripted-critic",
        provider="fake",
    )


class ScriptedCriticLLM(FakeLLM):
    """Fake writer plus a scripted list of critic verdicts."""

    def __init__(self, config, clock, reviews):
        super().__init__(config, clock)
        self.reviews = list(reviews)
        self.purposes: list[str] = []

    def complete_json(self, *, purpose: str, payload: dict, max_output_tokens: int = 1800):
        self.purposes.append(purpose)
        if purpose == "draft_review":
            if not self.reviews:
                raise AssertionError("critic called more times than scripted")
            return _response(self.reviews.pop(0))
        if purpose == "revise":
            data = self._draft(payload)
            # Make the accepted revision observably different without inventing facts.
            caption = list(data.get("caption") or [])
            if caption:
                caption[0] = str(caption[0]) + "（已按审稿意见修订）"
                data["caption"] = caption
            return _response(data)
        return super().complete_json(
            purpose=purpose, payload=payload, max_output_tokens=max_output_tokens
        )


def _pass() -> dict:
    return {"verdict": "pass", "summary": "ok", "problems": [], "confidence": 0.9}


def _revise() -> dict:
    return {
        "verdict": "revise",
        "summary": "one claim needs tightening",
        "problems": [
            {
                "kind": "unsupported_claim",
                "evidence_ids": ["1"],
                "instruction": "删除没有证据支持的数字，并重写对应句子",
            }
        ],
        "confidence": 0.8,
    }


class CriticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = new_env("critic", config_kwargs={"max_drafts": 10})
        self.env.config.data["agent"]["critic_enabled"] = True

    def tearDown(self) -> None:
        cleanup(self.env)

    def _actions(self) -> list[str]:
        rows = self.env.db.fetchall(
            "SELECT action FROM audit_log WHERE action LIKE 'draft.critic%'"
        )
        return [str(row["action"]) for row in rows]

    def test_pass_creates_draft_without_revision(self) -> None:
        self.env.llm = ScriptedCriticLLM(self.env.config, self.env.clock, [_pass()])
        draft = make_draft(self.env)
        self.assertIsNotNone(draft)
        self.assertEqual(self.env.llm.purposes.count("draft_review"), 1)
        self.assertNotIn("revise", self.env.llm.purposes)
        self.assertIn("draft.critic_review", self._actions())
        self.assertNotIn("draft.critic_revised", self._actions())

    def test_revise_then_pass_creates_one_draft(self) -> None:
        self.env.llm = ScriptedCriticLLM(
            self.env.config, self.env.clock, [_revise(), _pass()]
        )
        draft = make_draft(self.env)
        self.assertIsNotNone(draft)
        self.assertEqual(self.env.llm.purposes.count("draft_review"), 2)
        self.assertEqual(self.env.llm.purposes.count("revise"), 1)
        self.assertIn("draft.critic_revised", self._actions())
        self.assertIn("draft.critic_review", self._actions())

    def test_reject_blocks_before_any_draft_row(self) -> None:
        self.env.llm = ScriptedCriticLLM(
            self.env.config,
            self.env.clock,
            [
                {
                    "verdict": "reject",
                    "summary": "no supported comparison",
                    "problems": [
                        {
                            "kind": "evidence_gap",
                            "evidence_ids": [],
                            "instruction": "补充可直接比较的证据后再写",
                        }
                    ],
                    "confidence": 0.95,
                }
            ],
        )
        with self.assertRaises(DraftError):
            make_draft(self.env)
        row = self.env.db.fetchone("SELECT COUNT(*) AS n FROM drafts")
        self.assertEqual(int(row["n"]), 0)
        self.assertEqual(self.env.llm.purposes.count("draft_review"), 1)
        self.assertNotIn("revise", self.env.llm.purposes)

    def test_malformed_review_forces_revision_not_silent_pass(self) -> None:
        self.env.llm = ScriptedCriticLLM(
            self.env.config,
            self.env.clock,
            [{"verdict": "maybe", "summary": "malformed"}, _pass()],
        )
        draft = make_draft(self.env)
        self.assertIsNotNone(draft)
        self.assertEqual(self.env.llm.purposes.count("draft_review"), 2)
        self.assertEqual(self.env.llm.purposes.count("revise"), 1)
        self.assertIn("draft.critic_revised", self._actions())

    def test_revision_budget_exhaustion_blocks_draft(self) -> None:
        self.env.config.data["agent"]["max_critic_revisions"] = 1
        self.env.llm = ScriptedCriticLLM(
            self.env.config, self.env.clock, [_revise(), _revise()]
        )
        with self.assertRaises(DraftError):
            make_draft(self.env)
        row = self.env.db.fetchone("SELECT COUNT(*) AS n FROM drafts")
        self.assertEqual(int(row["n"]), 0)
        self.assertEqual(self.env.llm.purposes.count("revise"), 1)

    def test_disabled_critic_skips_review_call(self) -> None:
        self.env.config.data["agent"]["critic_enabled"] = False
        self.env.llm = ScriptedCriticLLM(self.env.config, self.env.clock, [])
        draft = make_draft(self.env)
        self.assertIsNotNone(draft)
        self.assertNotIn("draft_review", self.env.llm.purposes)


if __name__ == "__main__":
    unittest.main()

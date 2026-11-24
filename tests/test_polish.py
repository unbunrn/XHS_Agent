"""Offline tests for AI-style linting, optional polish pass, and layout QA."""

from __future__ import annotations

from types import SimpleNamespace
import unittest

from bot.budget import BudgetGuard
from bot.content import DraftContent
from bot.services.creator_agent.contracts import CreativeBrief
from bot.services.creator_agent.live import LiveRuntime
from bot.services.creator_agent.store import AgentStore
from bot.visual.selector import load_recent, record_recent
from bot.visual.similarity import dhash
from tests.helpers import cleanup, new_env


class _FakeSource:
    def search_candidates(self):
        body = "Qwen4-Max 在行业基准上取得 52.69 分，推理延迟下降 40%。官方公开了测试报告。"
        return ([{"url": "https://example.com/qwen", "title": "Qwen4-Max 发布", "content": body}], {})

    def fetch_content(self, url):
        return {
            "content": "Qwen4-Max 在行业基准上取得 52.69 分，推理延迟下降 40%。官方公开了测试报告。"
        }


class _ScriptedPolishLLM:
    def __init__(self, *, draft_data: dict, polish_data: dict, base_llm) -> None:
        self.draft_data = draft_data
        self.polish_data = polish_data
        self.base_llm = base_llm
        self.purposes: list[str] = []

    def complete_json(self, *, purpose, payload, max_output_tokens=1800, before_attempt=None):
        if before_attempt is not None:
            before_attempt()
        self.purposes.append(purpose)
        if purpose == "draft":
            return SimpleNamespace(data=dict(self.draft_data), provider="fake", model="scripted")
        if purpose == "polish":
            return SimpleNamespace(data=dict(self.polish_data), provider="fake", model="scripted")
        return self.base_llm.complete_json(
            purpose=purpose,
            payload=payload,
            max_output_tokens=max_output_tokens,
            before_attempt=before_attempt,
        )


def _high_ai_draft_payload() -> dict:
    return {
        "title": "为什么这款工具引发热议？",
        "caption": [
            "看到外媒报道，Qwen4-Max 基准拿了 52.69 分，这事挺有意思。",
            "值得一提的是，它不只是跑分好看，而是延迟确实变低了。",
            "总的来说，这次更新让开发者看到了无限可能，值得关注！",
        ],
        "tags": ["AI工具", "大模型"],
        "editorial_plan": {
            "audience_need": "关注大模型跑分与延迟变化的开发者",
            "angle": "客观拆解基准得分与延迟降幅，提醒按场景试用",
            "takeaway": "基准 52.69 分且延迟降 40%，适合高交互任务关注",
            "caption_image_division": "balanced",
            "strategy": "balanced",
            "rationale": "正文做场景判断，图片直接突出基准分与延迟数据",
            "visual_approach": "封面引出话题，数据卡集中展示核心指标",
            "mode": "balanced",
            "why": "核心看 52.69 基准分与 40% 延迟变化",
            "visual_theme": "editorial",
            "opening_move": "直接给结论",
        },
        "storyboard": {
            "cover": {
                "layout": "cover",
                "headline": "基准拿到 52.69 分",
                "subhead": "延迟下降 40%",
                "evidence_ids": ["M1"],
            },
            "cards": [
                {
                    "layout": "stat",
                    "headline": "实测数据对比",
                    "stats": [{"value": "52.69", "unit": "分", "label": "基准测试"}],
                    "evidence_ids": ["M1"],
                }
            ],
        },
        "claims": [
            {"type": "fact", "text": "Qwen4-Max 在行业基准取得 52.69 分。", "evidence_ids": ["M1"], "core": True},
            {"type": "fact", "text": "推理延迟下降 40%。", "evidence_ids": ["M1"], "core": False},
        ],
    }


def _clean_draft_payload() -> dict:
    return {
        "title": "基准 52.69 分，延迟降了 40%",
        "caption": [
            "直接看数字：Qwen4-Max 行业基准 52.69 分。",
            "推理延迟下降了 40%。做实时交互的值得测一下，写周报的先不用跟。",
        ],
        "tags": ["AI工具", "大模型"],
        "editorial_plan": {
            "audience_need": "想快速判断 Qwen4-Max 是否值得测试的开发者",
            "angle": "直接列出实测分数与延迟降幅，给出适用人群区分",
            "takeaway": "52.69 分与 -40% 延迟决定谁值得测、谁不用跟",
            "caption_image_division": "balanced",
            "strategy": "balanced",
            "rationale": "正文点明适用范围，图片用数据卡直出关键数值",
            "visual_approach": "封面突出两组数字对比，正文卡拆解指标",
            "mode": "balanced",
            "why": "直接展示核心基准与延迟数字",
            "visual_theme": "swiss",
            "opening_move": "一个数字",
        },
        "storyboard": {
            "cover": {
                "layout": "cover",
                "headline": "52.69 分\n延迟降 40%",
                "subhead": "官方测试报告核心数",
                "evidence_ids": ["M1"],
            },
            "cards": [
                {
                    "layout": "stat",
                    "headline": "关键变化",
                    "stats": [{"value": "52.69", "unit": "分", "label": "行业基准"}],
                    "evidence_ids": ["M1"],
                }
            ],
        },
        "claims": [
            {"type": "fact", "text": "Qwen4-Max 取得 52.69 分。", "evidence_ids": ["M1"], "core": True},
        ],
    }


class PolishAndQATests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = new_env("polish-qa")
        self.env.config.data.setdefault("creator_agent", {})["enabled"] = True
        self.brief = CreativeBrief(
            run_key="ca-polish-test",
            account_id="xhs-main",
            origin="manual",
            topic="Qwen4-Max 基准测试",
            instruction="客观讲清数字",
        )
        self.store = AgentStore(self.env.db, self.env.clock)
        self.store.ensure_run(self.brief)

    def tearDown(self) -> None:
        cleanup(self.env)

    def _runtime_with_llm(self, llm) -> LiveRuntime:
        return LiveRuntime(
            db=self.env.db,
            config=self.env.config,
            clock=self.env.clock,
            http=self.env.http,
            budget=BudgetGuard(self.env.db, self.env.config, self.env.clock),
            llm=llm,
            store=self.store,
            run_key=self.brief.run_key,
            search_factory=lambda q, limit, mode: _FakeSource(),
        )

    def test_a5_polish_invented_number_falls_back_to_original_draft(self) -> None:
        scripted = _ScriptedPolishLLM(
            draft_data=_high_ai_draft_payload(),
            polish_data={
                "title": "实测快了 99.9%",
                "caption": ["直接说结论：基准 52.69 分，吞吐翻了 99.9%。"],
            },
            base_llm=self.env.llm,
        )
        runtime = self._runtime_with_llm(scripted)
        materials = runtime.search("Qwen4-Max", limit=2)
        artifact = runtime.draft_artifact(
            brief=self.brief,
            material_ids=[m.material_id for m in materials],
        )
        self.assertIn("draft", scripted.purposes)
        self.assertIn("polish", scripted.purposes)
        # Reverted to original draft before invented 99.9%
        self.assertEqual(artifact.title, "为什么这款工具引发热议？")
        self.assertNotIn("99.9%", artifact.body)
        notes = artifact.content.get("uncertainties") or []
        self.assertTrue(any("style_lint(" in str(n) for n in notes), notes)

    def test_valid_polish_is_adopted_preserving_facts(self) -> None:
        scripted = _ScriptedPolishLLM(
            draft_data=_high_ai_draft_payload(),
            polish_data={
                "title": "基准 52.69 分：延迟降了 40%",
                "caption": [
                    "Qwen4-Max 行业基准拿到 52.69 分。",
                    "推理延迟下降 40%。对长上下文响应敏感的场景建议先测一轮。",
                ],
                "storyboard_patch": {
                    "cover": {"subhead": "核心只看两个数：52.69 分和 -40% 延迟"},
                },
            },
            base_llm=self.env.llm,
        )
        runtime = self._runtime_with_llm(scripted)
        materials = runtime.search("Qwen4-Max", limit=2)
        artifact = runtime.draft_artifact(
            brief=self.brief,
            material_ids=[m.material_id for m in materials],
        )
        self.assertEqual(scripted.purposes, ["draft", "polish"])
        self.assertEqual(artifact.title, "基准 52.69 分：延迟降了 40%")
        self.assertIn("52.69", artifact.body)
        self.assertEqual(
            artifact.content["storyboard"]["cover"]["subhead"],
            "核心只看两个数：52.69 分和 -40% 延迟",
        )
        notes = artifact.content.get("uncertainties") or []
        self.assertFalse(any("style_lint(" in str(n) for n in notes), notes)

    def test_clean_draft_skips_polish_call(self) -> None:
        scripted = _ScriptedPolishLLM(
            draft_data=_clean_draft_payload(),
            polish_data={"title": "不应被使用", "caption": ["不应出现"]},
            base_llm=self.env.llm,
        )
        runtime = self._runtime_with_llm(scripted)
        materials = runtime.search("Qwen4-Max", limit=2)
        artifact = runtime.draft_artifact(
            brief=self.brief,
            material_ids=[m.material_id for m in materials],
        )
        self.assertEqual(scripted.purposes, ["draft"])
        self.assertEqual(artifact.title, "基准 52.69 分，延迟降了 40%")

    def test_inspect_layout_flags_similar_cover_dhash(self) -> None:
        scripted = _ScriptedPolishLLM(
            draft_data=_clean_draft_payload(),
            polish_data={},
            base_llm=self.env.llm,
        )
        runtime = self._runtime_with_llm(scripted)
        materials = runtime.search("Qwen4-Max", limit=2)
        artifact = runtime.draft_artifact(
            brief=self.brief,
            material_ids=[m.material_id for m in materials],
        )
        artifact = runtime.render_images(brief=self.brief, artifact=artifact)
        cover_hash = dhash(artifact.images[0]["path"])
        record_recent(
            self.env.db,
            {
                "run_key": "prior-post-99",
                "theme": "swiss",
                "palette": "signal",
                "cover_variant": "headline",
                "cover_dhash": cover_hash,
            },
            clock=self.env.clock,
        )
        issues = runtime.inspect_layout(artifact)
        self.assertTrue(
            any("cover too similar to prior-post-99" in msg for msg in issues),
            issues,
        )

    def test_submit_records_recent_visual_entry(self) -> None:
        scripted = _ScriptedPolishLLM(
            draft_data=_clean_draft_payload(),
            polish_data={},
            base_llm=self.env.llm,
        )
        runtime = self._runtime_with_llm(scripted)
        materials = runtime.search("Qwen4-Max", limit=2)
        artifact = runtime.draft_artifact(
            brief=self.brief,
            material_ids=[m.material_id for m in materials],
        )
        artifact = runtime.render_images(brief=self.brief, artifact=artifact)
        res = runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertTrue(res.ok, res.error)
        recent = load_recent(self.env.db)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["run_key"], self.brief.run_key)
        self.assertIsInstance(recent[0]["cover_dhash"], int)


if __name__ == "__main__":
    unittest.main()

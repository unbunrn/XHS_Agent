"""Focused offline tests for the owner-intent editorial upgrade.

These tests use only local fixtures, the deterministic fake writer and temporary
directories under var/tests.  They never read secrets/live data or call a network
service, and they do not claim live-model quality.
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from bot.adapters.llm import FakeLLM
from bot.adapters.types import BudgetExceeded, LLMOutputError, LLMResponse, TokenUsage
from bot.content import (
    DraftContent,
    EditorialPlan,
    ImageCard,
    ImageColumn,
    ImageStoryboard,
    validate_draft_payload,
)
from bot.db import json_loads
from bot.editorial import (
    EditorialProfileError,
    duplication_report,
    infer_revision_request,
    load_editorial_profile,
    save_editorial_profile,
)
from bot.render import render_draft
from bot.services import research
from bot.services.approvals import get_approval, verify_approval_artifact
from bot.services.research import build_writer_payload
from bot.timeutil import to_iso
from bot.worker import Worker
from tests.helpers import cleanup, make_draft, new_env


OWNER = "owner-open-id"
FEEDBACK = "图片和正文重复了，要么图片精简，正文详写，要么图片详写，正文只做介绍衬托图片"


def _response(data: dict) -> LLMResponse:
    return LLMResponse(
        data=data,
        raw_text=json.dumps(data, ensure_ascii=False),
        usage=TokenUsage(),
        latency_ms=0,
        model="fake-test-double",
        provider="fake",
    )


def _queue_revision(env, code: str, instruction: str, message_id: str) -> dict:
    from bot.services.approvals import handle_inbound_message

    return handle_inbound_message(
        env.db,
        env.config,
        env.clock,
        message_id=message_id,
        sender_id=OWNER,
        chat_id=OWNER,
        chat_type="p2p",
        text=f"修改 {code} {instruction}",
        channel="test",
    )


def _draft_content(env, draft_id: int) -> DraftContent:
    row = env.db.fetchone("SELECT * FROM drafts WHERE id=?", (draft_id,))
    assert row is not None
    return DraftContent.from_dict(json_loads(row["body_json"], {}) or {})


def _make_item(title: str, summary: str) -> dict:
    return {
        "title": title,
        "url": f"https://example.com/{abs(hash(title)) % 10_000_000}",
        "summary": summary,
        "content": summary,
        "author": "Test Fixture",
        "published_offset_hours": -2,
    }


class EditorialAutonomyTests(unittest.TestCase):
    def test_distinct_content_gets_distinct_plans_layouts_and_page_counts(self) -> None:
        # A one-line item can legitimately be a single cover.  A comparison and
        # a layered-architecture item should choose different plans instead of a
        # fixed card template.
        simple = new_env(
            "auto-simple",
            items=[
                _make_item(
                    "AI 工具一句话说明",
                    "AI 工具免费开源，材料只有一句话，读者可以在来源里核对。",
                )
            ],
        )
        comparison = new_env(
            "auto-compare",
            items=[
                _make_item(
                    "AI 工具方案对比",
                    "AI 工具方案 A 与方案 B 在免费额度、成本和步骤上不同。",
                )
            ],
        )
        diagram = new_env(
            "auto-diagram",
            items=[
                _make_item(
                    "AI 智能体记忆架构",
                    "AI 智能体记忆架构采用开源方案，材料描述记忆模块、分层结构和写入机制。",
                )
            ],
        )
        try:
            simple_draft = make_draft(simple)
            compare_draft = make_draft(comparison)
            diagram_draft = make_draft(diagram)
            simple_content = _draft_content(simple, simple_draft["draft"]["id"])
            compare_content = _draft_content(comparison, compare_draft["draft"]["id"])
            diagram_content = _draft_content(diagram, diagram_draft["draft"]["id"])

            for content in (simple_content, compare_content, diagram_content):
                self.assertIsNotNone(content.storyboard)
                self.assertIsNotNone(content.editorial_plan)
                plan = content.editorial_plan
                assert plan is not None
                self.assertIn(plan.strategy, {"image_led", "caption_led", "balanced"})
                for value in (
                    plan.audience_need,
                    plan.angle,
                    plan.takeaway,
                    plan.caption_image_division,
                    plan.rationale,
                    plan.visual_approach,
                ):
                    self.assertTrue(value.strip())

            self.assertEqual(simple_content.editorial_plan.strategy, "image_led")
            self.assertEqual(diagram_content.editorial_plan.strategy, "caption_led")
            self.assertEqual(len(simple_draft["images"]), 1)
            self.assertEqual(
                len(simple_draft["images"]),
                len(simple_content.storyboard.all_cards()),
            )
            self.assertEqual(
                [card.layout for card in compare_content.storyboard.all_cards()],
                ["cover", "comparison"],
            )
            self.assertEqual(
                [card.layout for card in diagram_content.storyboard.all_cards()],
                ["cover", "diagram"],
            )
            self.assertEqual(len(compare_draft["images"]), 2)
            self.assertEqual(len(diagram_draft["images"]), 2)
            self.assertGreater(len(compare_draft["images"]), len(simple_draft["images"]))
            self.assertNotEqual(
                simple_content.editorial_plan.visual_approach,
                compare_content.editorial_plan.visual_approach,
            )
        finally:
            cleanup(simple)
            cleanup(comparison)
            cleanup(diagram)

    def test_duplication_check_allows_shared_takeaway_but_not_copied_paragraph(self) -> None:
        paragraph = "这是正文里足够长的一整段说明，用来判断图片有没有整段照搬正文内容。"
        shared = DraftContent(
            title="测试",
            sections=[],
            claims=[],
            tags=[],
            uncertainties=[],
            sources=[],
            caption=[paragraph],
            storyboard=ImageStoryboard(
                cover=ImageCard(layout="cover", headline="一个封面", evidence_ids=["e1"]),
                cards=[
                    ImageCard(
                        layout="keypoints",
                        headline="可以带走的结论",
                        points=["先核对来源"],
                        evidence_ids=["e1"],
                    )
                ],
            ),
        )
        copied = copy.deepcopy(shared)
        assert copied.storyboard is not None
        copied.storyboard.cards[0].points = [paragraph]
        self.assertFalse(duplication_report(shared)["heavy_duplication"])
        copied_report = duplication_report(copied)
        self.assertTrue(copied_report["heavy_duplication"])
        self.assertGreaterEqual(copied_report["paragraph_duplication_count"], 1)

    def test_revision_contract_keeps_clause_local_artifact_intent(self) -> None:
        empty = DraftContent(
            title="测试",
            sections=[],
            claims=[],
            tags=[],
            uncertainties=[],
            sources=[],
        )
        request = infer_revision_request("图片精简，正文详写", empty)
        self.assertIn("shorten", request.artifact_intents("images"))
        self.assertNotIn("expand", request.artifact_intents("images"))
        self.assertIn("expand", request.artifact_intents("caption"))
        self.assertNotIn("shorten", request.artifact_intents("caption"))
        # An either/or instruction keeps both directions, so acceptance does
        # not force the model to satisfy contradictory branches at once.
        either = infer_revision_request(FEEDBACK, empty)
        self.assertIn("shorten", either.artifact_intents("images"))
        self.assertIn("expand", either.artifact_intents("images"))
        self.assertIn("shorten", either.artifact_intents("caption"))
        self.assertIn("expand", either.artifact_intents("caption"))

    def test_old_section_only_draft_still_deserializes_and_renders(self) -> None:
        data = {
            "title": "旧稿兼容",
            "sections": [
                {"key": "what", "heading": "发生了什么", "paragraphs": ["旧稿正文。"]},
                {"key": "audience", "heading": "对谁有用", "paragraphs": ["旧读者。"]},
                {"key": "how", "heading": "具体怎么用", "paragraphs": ["旧用法。"]},
                {"key": "limits", "heading": "限制与不确定性", "paragraphs": ["旧限制。"]},
            ],
            "claims": [],
            "tags": ["旧稿"],
            "uncertainties": [],
            "sources": [
                {
                    "evidence_id": "e1",
                    "name": "旧来源",
                    "url": "https://example.com/old",
                    "published_at": None,
                    "date_precision": "unknown",
                    "fetched_at": None,
                }
            ],
            "generator": "legacy",
            "model": "legacy",
        }
        content = DraftContent.from_dict(data)
        self.assertIsNone(content.storyboard)
        self.assertIsNone(content.editorial_plan)
        body = content.render_body()
        self.assertIn("【发生了什么】", body)
        self.assertIn("旧稿正文", body)
        env = new_env("legacy-render")
        try:
            images = render_draft(
                content,
                out_dir=env.root / "legacy",
                config=env.config,
                draft_key="legacy",
            )
            self.assertGreaterEqual(len(images), 1)
        finally:
            cleanup(env)

    def test_all_supported_storyboard_layouts_render_locally(self) -> None:
        env = new_env("layout-vocabulary")
        try:
            env.config.data["render"]["max_cards"] = 8
            storyboard = ImageStoryboard(
                cover=ImageCard(
                    layout="cover",
                    headline="视觉词汇测试",
                    subhead="本地文字与几何排版",
                    evidence_ids=["e1"],
                ),
                cards=[
                    ImageCard(
                        layout="comparison",
                        headline="两边并列",
                        columns=[
                            ImageColumn(label="A", text="第一种说法"),
                            ImageColumn(label="B", text="第二种说法"),
                        ],
                        evidence_ids=["e1"],
                    ),
                    ImageCard(
                        layout="process",
                        headline="按来源步骤",
                        points=["第一步：核对", "第二步：验证"],
                        evidence_ids=["e1"],
                    ),
                    ImageCard(
                        layout="keypoints",
                        headline="要点",
                        points=["只放可核对的信息", "允许简短结论"],
                        evidence_ids=["e1"],
                    ),
                    ImageCard(
                        layout="diagram",
                        headline="分层关系",
                        points=["材料层", "解释层"],
                        evidence_ids=["e1"],
                    ),
                    ImageCard(
                        layout="argument",
                        headline="主张",
                        points=["依据一", "依据二"],
                        evidence_ids=["e1"],
                    ),
                    ImageCard(
                        layout="annotated",
                        headline="来源注解",
                        subhead="来源里的一段话",
                        points=["注解一", "注解二"],
                        evidence_ids=["e1"],
                    ),
                ],
            )
            content = DraftContent(
                title="布局测试",
                sections=[],
                claims=[],
                tags=[],
                uncertainties=[],
                sources=[],
                caption=["这是正文，不会逐段出现在图片里。"],
                editorial_plan=EditorialPlan(
                    audience_need="看不同视觉词汇",
                    angle="逐种渲染",
                    takeaway="一张封面加六种内容布局",
                    caption_image_division="正文和图片各自独立",
                    rationale="验证所有本地几何布局都能生成 PNG",
                    strategy="balanced",
                    visual_approach="七张卡覆盖全部布局",
                ),
                storyboard=storyboard,
            )
            images = render_draft(
                content,
                out_dir=env.root / "layouts",
                config=env.config,
                draft_key="layouts",
            )
            self.assertEqual(len(images), 7)
            self.assertTrue(all(image.path.exists() for image in images))
        finally:
            cleanup(env)

    def test_image_factual_number_and_process_sequence_are_evidence_checked(self) -> None:
        evidence = {
            "e1": {
                "id": "e1",
                "quote": "官方说明该功能处理速度提升 20%。",
                "url": "https://example.com/source",
                "source_name": "Example",
                "published_at": None,
                "date_precision": "unknown",
                "fetched_at": None,
                "trust_tier": 2,
                "vendor_claim": True,
            }
        }
        payload = {
            "title": "速度变化",
            "caption": ["公开材料提到一次速度变化。", "以上为公开资料整理，未做个人实测。"],
            "editorial_plan": {
                "audience_need": "想知道速度变化是否可信",
                "angle": "只看可核对数字",
                "takeaway": "数字必须回来源核对",
                "caption_image_division": "正文说明边界，图片只放一个数字",
                "rationale": "这条材料的核心是一个数字，适合用一页图片突出",
                "strategy": "image_led",
                "visual_approach": "一张要点卡",
            },
            "storyboard": {
                "cover": {
                    "layout": "cover",
                    "headline": "速度变化",
                    "evidence_ids": ["e1"],
                },
                "cards": [
                    {
                        "layout": "keypoints",
                        "headline": "关键数字",
                        "points": ["提升 300%"],
                        "evidence_ids": ["e1"],
                    }
                ],
            },
            "claims": [
                {
                    "text": "官方称：处理速度提升 20%。",
                    "type": "vendor_claim",
                    "evidence_ids": ["e1"],
                }
            ],
            "tags": ["测试"],
            "uncertainties": [],
        }
        env = new_env("image-number")
        try:
            with self.assertRaises(LLMOutputError):
                validate_draft_payload(
                    payload,
                    evidence_by_id=evidence,
                    config=env.config,
                    require_storyboard=True,
                )
            no_evidence = copy.deepcopy(payload)
            no_evidence["storyboard"]["cards"][0]["points"] = ["处理速度有变化"]
            no_evidence["storyboard"]["cards"][0]["evidence_ids"] = []
            with self.assertRaises(LLMOutputError):
                validate_draft_payload(
                    no_evidence,
                    evidence_by_id=evidence,
                    config=env.config,
                    require_storyboard=True,
                )
            process = copy.deepcopy(payload)
            process["storyboard"]["cards"][0] = {
                "layout": "process",
                "headline": "模块关系",
                "points": ["模块 A 负责写入", "模块 B 负责读取"],
                "evidence_ids": ["e1"],
            }
            with self.assertRaises(LLMOutputError):
                validate_draft_payload(
                    process,
                    evidence_by_id=evidence,
                    config=env.config,
                    require_storyboard=True,
                )
        finally:
            cleanup(env)

    def test_unchanged_revision_is_rejected_without_review_or_success_notice(self) -> None:
        class UnchangedLLM(FakeLLM):
            def __init__(self, config, clock):
                super().__init__(config, clock)
                self.calls: list[str] = []

            def complete_json(self, *, purpose, payload, max_output_tokens=1800):
                self.calls.append(purpose)
                if purpose == "revise":
                    return _response(copy.deepcopy(payload["current_draft"]))
                return super().complete_json(
                    purpose=purpose, payload=payload, max_output_tokens=max_output_tokens
                )

        env = new_env("revision-unchanged")
        try:
            draft = make_draft(env)
            code = draft["approval_code"]
            queued = _queue_revision(env, code, "请精简正文", "unchanged-1")
            self.assertEqual(queued["outcome"], "revision_queued")
            worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            llm = UnchangedLLM(env.config, env.clock)
            worker.llm = llm
            tick = worker.tick()
            self.assertTrue(
                any(job.get("revision_unsatisfied") for job in tick["jobs"]), tick
            )
            self.assertEqual(llm.calls, ["revise"])
            self.assertEqual(get_approval(env.db, code=code)["status"], "revision_pending")
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM drafts"), 1)
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM approvals"), 1)
            notice = env.db.fetchone(
                "SELECT payload_json FROM notifications WHERE dedupe_key='revision-outcome:1'"
            )
            text = json_loads(notice["payload_json"], {})["text"]
            self.assertIn("修改未满足", text)
            self.assertIn("未生成可审批的新版本", text)
            self.assertNotIn("修改完成", text)
        finally:
            cleanup(env)

    def test_self_reported_review_success_cannot_override_acceptance_contract(self) -> None:
        class SelfReportLLM(FakeLLM):
            def __init__(self, config, clock):
                super().__init__(config, clock)
                self.review_calls = 0

            def complete_json(self, *, purpose, payload, max_output_tokens=1800):
                if purpose == "review_revision":
                    self.review_calls += 1
                    return _response(
                        {
                            "satisfied": True,
                            "change_summary": "模型自称完成",
                            "problems": [],
                            "remaining_duplication": False,
                            "invented_experience_or_engagement": False,
                        }
                    )
                return super().complete_json(
                    purpose=purpose, payload=payload, max_output_tokens=max_output_tokens
                )

        env = new_env("revision-self-report")
        try:
            draft = make_draft(env)
            code = draft["approval_code"]
            _queue_revision(env, code, "请把图片和正文重新组织，不要重复", "self-report-1")
            worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            llm = SelfReportLLM(env.config, env.clock)
            worker.llm = llm
            tick = worker.tick()
            self.assertTrue(
                any(job.get("revision_unsatisfied") for job in tick["jobs"]), tick
            )
            self.assertEqual(llm.review_calls, 1)
            self.assertEqual(get_approval(env.db, code=code)["status"], "revision_pending")
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM drafts"), 1)
            notice = env.db.fetchone(
                "SELECT payload_json FROM notifications WHERE dedupe_key='revision-outcome:1'"
            )
            text = json_loads(notice["payload_json"], {})["text"]
            self.assertIn("修改未满足", text)
        finally:
            cleanup(env)

    def test_budget_exhaustion_in_review_leaves_old_artifact_frozen(self) -> None:
        class BudgetReviewLLM(FakeLLM):
            def __init__(self, config, clock):
                super().__init__(config, clock)
                self.review_calls = 0

            def complete_json(self, *, purpose, payload, max_output_tokens=1800):
                if purpose == "review_revision":
                    self.review_calls += 1
                    raise BudgetExceeded("daily revision review budget exhausted")
                return super().complete_json(
                    purpose=purpose, payload=payload, max_output_tokens=max_output_tokens
                )

        env = new_env("revision-budget")
        try:
            draft = make_draft(env)
            code = draft["approval_code"]
            _queue_revision(env, code, "请精简正文", "budget-1")
            worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            llm = BudgetReviewLLM(env.config, env.clock)
            worker.llm = llm
            tick = worker.tick()
            self.assertTrue(
                any(job.get("revision_failed") for job in tick["jobs"]), tick
            )
            self.assertEqual(llm.review_calls, 1)
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM drafts"), 1)
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM approvals"), 1)
            self.assertEqual(get_approval(env.db, code=code)["status"], "revision_pending")
        finally:
            cleanup(env)

    def test_owner_feedback_changes_split_and_binds_new_storyboard_images(self) -> None:
        env = new_env("revision-feedback")
        try:
            draft = make_draft(env)
            code = draft["approval_code"]
            old_approval = get_approval(env.db, code=code)
            old_draft = env.db.fetchone(
                "SELECT * FROM drafts WHERE id=?", (draft["draft"]["id"],)
            )
            old_body_json = old_draft["body_json"]
            old_image_bytes = [
                Path(image["path"]).read_bytes() for image in draft["images"]
            ]

            queued = _queue_revision(env, code, FEEDBACK, "feedback-1")
            self.assertEqual(queued["outcome"], "revision_queued")
            worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            tick = worker.tick()
            self.assertFalse(
                any("revision_unsatisfied" in job for job in tick["jobs"]), tick
            )

            old_approval_after = get_approval(env.db, code=code)
            self.assertEqual(old_approval_after["status"], "superseded")
            self.assertEqual(old_approval_after["fingerprint"], old_approval["fingerprint"])
            old_draft_after = env.db.fetchone(
                "SELECT * FROM drafts WHERE id=?", (draft["draft"]["id"],)
            )
            self.assertEqual(old_draft_after["status"], "superseded")
            self.assertEqual(old_draft_after["body_json"], old_body_json)
            self.assertEqual(
                [Path(image["path"]).read_bytes() for image in draft["images"]],
                old_image_bytes,
            )

            new_approval = env.db.fetchone(
                "SELECT * FROM approvals WHERE status='pending' ORDER BY id DESC LIMIT 1"
            )
            self.assertIsNotNone(new_approval)
            new_draft = env.db.fetchone(
                "SELECT * FROM drafts WHERE id=?", (new_approval["draft_id"],)
            )
            new_content = DraftContent.from_dict(json_loads(new_draft["body_json"], {}))
            self.assertIsNotNone(new_content.storyboard)
            self.assertIsNotNone(new_content.editorial_plan)
            self.assertEqual(
                len(env.db.fetchall(
                    "SELECT * FROM draft_images WHERE draft_id=? ORDER BY ordinal",
                    (new_draft["id"],),
                )),
                len(new_content.storyboard.all_cards()),
            )
            self.assertNotEqual(new_approval["images_sha256"], old_approval["images_sha256"])
            ok, reason, _ = verify_approval_artifact(env.db, new_approval)
            self.assertTrue(ok, reason)

            # Tampering with a stored PNG invalidates the new approval too.
            first_image = env.db.fetchone(
                "SELECT path FROM draft_images WHERE draft_id=? ORDER BY ordinal LIMIT 1",
                (new_draft["id"],),
            )
            path = Path(first_image["path"])
            path.write_bytes(path.read_bytes() + b"tampered")
            ok, reason, _ = verify_approval_artifact(env.db, new_approval)
            self.assertFalse(ok)
            self.assertIn("image", reason)

            notice = env.db.fetchone(
                "SELECT payload_json FROM notifications WHERE dedupe_key='revision-outcome:1'"
            )
            text = json_loads(notice["payload_json"], {})["text"]
            self.assertIn("修改完成", text)
            self.assertIn("实际修改", text)
            self.assertIn("正文已修改", text)
            self.assertIn("图片 storyboard 已修改", text)
        finally:
            cleanup(env)

    def test_revision_moves_topic_request_latest_result_transactionally(self) -> None:
        env = new_env("revision-topic-link")
        try:
            draft = make_draft(env)
            code = draft["approval_code"]
            now = to_iso(env.clock.now())
            with env.db.tx() as conn:
                cur = conn.execute(
                    "INSERT INTO topic_requests(code, message_id, sender_id, raw_text, command, "
                    "topic, instruction, status, day_key, created_at, updated_at, event_id, draft_id, "
                    "approval_id, result_code, finished_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'completed', '2026-11-05', ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "XT-ABC123",
                        "topic-msg-1",
                        OWNER,
                        "选题 测试主题",
                        "选题",
                        "测试主题",
                        "测试主题",
                        now,
                        now,
                        draft["draft"]["event_id"],
                        draft["draft"]["id"],
                        draft["approval_id"],
                        code,
                        now,
                    ),
                )
                request_id = int(cur.lastrowid)
                conn.execute(
                    "UPDATE drafts SET topic_request_id=? WHERE id=?",
                    (request_id, draft["draft"]["id"]),
                )
            _queue_revision(env, code, "请精简正文", "topic-revision-1")
            worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            tick = worker.tick()
            self.assertFalse(
                any("revision_unsatisfied" in job for job in tick["jobs"]), tick
            )
            request = env.db.fetchone(
                "SELECT * FROM topic_requests WHERE id=?", (request_id,)
            )
            new_approval = env.db.fetchone(
                "SELECT * FROM approvals WHERE status='pending' ORDER BY id DESC LIMIT 1"
            )
            self.assertEqual(request["draft_id"], new_approval["draft_id"])
            self.assertEqual(request["approval_id"], new_approval["id"])
            self.assertEqual(request["result_code"], new_approval["code"])
            new_draft = env.db.fetchone(
                "SELECT * FROM drafts WHERE id=?", (new_approval["draft_id"],)
            )
            self.assertEqual(new_draft["topic_request_id"], request_id)
            old_draft = env.db.fetchone(
                "SELECT * FROM drafts WHERE id=?", (draft["draft"]["id"],)
            )
            self.assertIsNone(old_draft["topic_request_id"])
        finally:
            cleanup(env)

    def test_editorial_profile_is_durable_but_per_post_instructions_are_not(self) -> None:
        env = new_env("editorial-profile")
        try:
            draft = make_draft(env)
            with self.assertRaises(EditorialProfileError):
                save_editorial_profile(
                    env.config,
                    {"default_angle": "不能隐式保存"},
                    explicit_owner_request=False,
                )
            saved = save_editorial_profile(
                env.config,
                {"default_angle": "按选题自行决定，不套固定模板"},
                explicit_owner_request=True,
            )
            # Profile text is NFKC-normalised, so the full-width comma becomes
            # an ASCII comma; the meaning is preserved.
            self.assertEqual(saved.default_angle, "按选题自行决定,不套固定模板")
            loaded = load_editorial_profile(env.config)
            self.assertEqual(loaded.default_angle, "按选题自行决定,不套固定模板")
            profile_path = env.config.resolve_path("config/editorial-profile.toml")
            self.assertTrue(profile_path.exists())
            self.assertIn("不设固定长度比例", profile_path.read_text(encoding="utf-8"))

            event = env.db.fetchone(
                "SELECT * FROM events WHERE id=?", (draft["draft"]["event_id"],)
            )
            evidence = research.research_event(env.db, int(event["id"]), env.clock)
            instruction = "这一篇请只针对当前事件写短一点，不要保存成长期偏好"
            payload = build_writer_payload(
                env.db,
                env.config,
                event,
                evidence,
                clock=env.clock,
                instruction=instruction,
            )
            self.assertEqual(
                payload["editorial_profile"]["default_angle"],
                "按选题自行决定,不套固定模板",
            )
            self.assertNotIn(instruction, profile_path.read_text(encoding="utf-8"))
        finally:
            cleanup(env)


if __name__ == "__main__":
    unittest.main()

"""Offline tests for the V2 live adapter and its Feishu entry point."""

from __future__ import annotations

from pathlib import Path
import json
import shutil
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from PIL import Image

from bot.adapters.llm import DeepSeekLLM, _RetryableHTTP
from bot.budget import BudgetExceeded, BudgetGuard
from bot.render import RenderedImage
from bot.security import sha256_file
from bot.services.approvals import handle_inbound_message
from bot.services.creator_agent.context import AgentContext
from bot.services.creator_agent.contracts import ClaimAssessment, CreativeBrief, Material
from bot.services.creator_agent.entry import (
    brief_from_text,
    enqueue_brief,
    run_job,
)
from bot.services.creator_agent.live import DraftValidationError, LiveRuntime, _quote_for_claim
from bot.services.creator_agent.registry import ToolRegistry, ToolSpec
from bot.services.creator_agent.runner import AgentResult, AgentRunner
from bot.services.creator_agent.store import AgentStore
from bot.services.creator_agent.tools import register_tools
from tests.helpers import cleanup, new_env


class FakeSearchSource:
    def search_candidates(self):
        body = "Claude Code 的 fable 别名解析为 Fable 5.2。官方文档说明如何选择模型。"
        return ([{"url": "https://example.com/docs", "title": "Example docs", "content": body}], {})

    def fetch_content(self, url):
        return {"content": "Claude Code 的 fable 别名解析为 Fable 5.2。官方文档说明如何选择模型。"}


class RevisionDecisions:
    """Agent decisions that exercise the real revision tool path."""

    def __init__(self, base_llm, parent_version):
        self.base_llm = base_llm
        self.parent_version = int(parent_version)
        self.decisions = [
            {"action": "revise_artifact", "args": {
                "instruction": "调整配图排版，保留第二张图",
                "material_ids": [],
                "revision_plan": {"preserve": ["image:2"]},
            }},
            {"action": "render_images", "args": {}},
            {"action": "review_artifact", "args": {}},
            {"action": "submit_for_approval", "args": {}},
        ]
        self.observations = []

    def complete_json(self, *, purpose, payload, max_output_tokens=1800, before_attempt=None):
        if before_attempt is not None:
            before_attempt()
        if purpose == "agent_decide":
            self.observations.append(dict(payload))
            if not self.decisions:
                raise RuntimeError("revision script exhausted")
            if self.decisions[0]["action"] == "revise_artifact":
                self.decisions[0]["args"]["material_ids"] = [
                    m["material_id"] for m in (payload.get("materials") or [])
                ]
            return SimpleNamespace(
                data=self.decisions.pop(0), provider="fake", model="scripted",
            )
        return self.base_llm.complete_json(
            purpose=purpose, payload=payload,
            max_output_tokens=max_output_tokens, before_attempt=before_attempt,
        )


class LiveRuntimeTests(TestCase):
    def setUp(self):
        self.env = new_env("v2live")
        self.env.config.data.setdefault("creator_agent", {})["enabled"] = True
        self.brief = CreativeBrief(
            run_key="ca-live-test", account_id="xhs-main", origin="manual",
            topic="Claude Code 选模型", instruction="写一篇",
        )
        self.store = AgentStore(self.env.db, self.env.clock)
        self.store.ensure_run(self.brief)
        self.runtime = LiveRuntime(
            db=self.env.db, config=self.env.config, clock=self.env.clock,
            http=self.env.http,
            budget=BudgetGuard(self.env.db, self.env.config, self.env.clock),
            llm=self.env.llm, store=self.store, run_key=self.brief.run_key,
            search_factory=lambda q, limit, mode: FakeSearchSource(),
        )

    def tearDown(self):
        cleanup(self.env)

    def test_real_approval_bridge_creates_pending_approval_without_publishing(self):
        materials = self.runtime.search("Claude", limit=2)
        self.assertEqual(len(materials), 1)
        artifact = self.runtime.draft_artifact(
            brief=self.brief, material_ids=[m.material_id for m in materials]
        )
        artifact = self.runtime.render_images(brief=self.brief, artifact=artifact)
        review = self.runtime.review_artifact(
            brief=self.brief, artifact=artifact, materials=materials
        )
        self.assertEqual(review.verdict, "pass", review)
        result = self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.terminal, "submit")
        approval = self.env.db.fetchone("SELECT * FROM approvals")
        self.assertIsNotNone(approval)
        self.assertEqual(approval["status"], "pending")
        self.assertEqual(approval["code"], result.data["approval_code"])
        draft = self.env.db.fetchone("SELECT * FROM drafts WHERE id=?", (result.data["draft_id"],))
        self.assertEqual(draft["status"], "review_pending")
        self.assertEqual(self.env.db.fetchone("SELECT COUNT(*) AS n FROM publish_attempts")["n"], 0)

    def _ready_artifact(self):
        materials = self.runtime.search("Claude", limit=2)
        artifact = self.runtime.draft_artifact(
            brief=self.brief, material_ids=[m.material_id for m in materials]
        )
        artifact = self.runtime.render_images(brief=self.brief, artifact=artifact)
        return materials, artifact

    def _add_second_image(self, artifact):
        second = dict(artifact.images[0])
        second["ordinal"] = 2
        second_path = self.env.root / "second-image.png"
        shutil.copy2(second["path"], second_path)
        second["path"] = str(second_path)
        second["sha256"] = sha256_file(second_path)
        artifact.images.append(second)
        return second

    def test_claim_quote_includes_numbers_after_navigation_header(self):
        body = "导航\n" * 200 + (
            "Qwen4-Max在Work360行业基准上取得52.69分，前代Qwen3.8-Max为47.74分。"
            "单个任务的平均费用从26.58元降至11.16元，降幅58%。"
        )
        quote = _quote_for_claim(body, "Work360 52.69 47.74 26.58 11.16 58%")
        self.assertIn("52.69", quote)
        self.assertIn("47.74", quote)
        self.assertIn("26.58", quote)
        self.assertIn("11.16", quote)

    def test_claim_number_absent_from_rewritten_body_becomes_uncertain(self):
        self.runtime._bodies["M1"] = "正文已经删掉那组签名数字。"
        content = SimpleNamespace(claims=[
            SimpleNamespace(text="公开信累计超过10万个签名，含5248名员工", evidence_ids=["M1"], core=False)
        ])
        claims = self.runtime._claims_from_content(content, ["M1"])
        self.assertEqual(claims[0].status, "uncertain")
        self.assertEqual(claims[0].quote, "")

    def test_claim_quote_uses_full_body_after_short_excerpt(self):
        self.runtime._bodies["M1"] = "前文没有数字。" * 200 + "解码速度超过150tps。"
        self.runtime._materials["M1"] = Material(
            material_id="M1", url="https://example.com", title="Example",
            content_hash="x", quote="前文没有数字。",
        )
        content = SimpleNamespace(claims=[
            SimpleNamespace(text="解码速度超过150 tps。", evidence_ids=["M1"], core=False)
        ])
        claims = self.runtime._claims_from_content(content, ["M1"])
        self.assertEqual(claims[0].status, "supported")
        self.assertIn("150", claims[0].quote)

    def test_public_body_omits_source_urls_and_report_boilerplate(self):
        materials, artifact = self._ready_artifact()
        self.assertNotIn("http://", artifact.body)
        self.assertNotIn("https://", artifact.body)
        self.assertNotIn("【来源】", artifact.body)
        self.assertNotIn("公开资料整理", artifact.body)

        result = self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertTrue(result.ok, result.error)
        draft = self.env.db.fetchone(
            "SELECT * FROM drafts WHERE id=?", (result.data["draft_id"],)
        )
        body = str(draft["body"])
        self.assertNotIn("https://", body)
        self.assertNotIn("【来源】", body)
        meta = json.loads(draft["body_json"])
        self.assertTrue(meta.get("sources"), "sources must remain in private metadata")

    def test_submit_is_idempotent_and_creates_manual_lineage(self):
        materials, artifact = self._ready_artifact()
        first = self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertTrue(first.ok, first.error)
        second = self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertTrue(second.ok, second.error)
        self.assertEqual(first.data["approval_code"], second.data["approval_code"])
        self.assertEqual(
            self.env.db.fetchone("SELECT COUNT(*) AS n FROM approvals")["n"], 1
        )
        draft = self.env.db.fetchone(
            "SELECT * FROM drafts WHERE id=?", (first.data["draft_id"],)
        )
        self.assertIsNotNone(draft["topic_request_id"])
        self.assertEqual(
            self.env.db.fetchone("SELECT COUNT(*) AS n FROM topic_requests")["n"], 1
        )
        # A submitted artifact's content is immutable; a conflicting save fails.
        artifact.title = "changed after submit"
        with self.assertRaises(RuntimeError):
            self.store.save_artifact(artifact)

    def test_submit_rejects_changed_submission_package(self):
        materials, artifact = self._ready_artifact()
        self._add_second_image(artifact)
        first = self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertTrue(first.ok, first.error)
        # A changed image manifest must never reuse the old approval.
        artifact.images[0]["sha256"] = "0" * 64
        changed = self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertFalse(changed.ok)
        self.assertIn("package changed", changed.error)
        self.assertEqual(
            self.env.db.fetchone("SELECT COUNT(*) AS n FROM approvals")["n"], 1
        )

    def test_submit_rejects_changed_body(self):
        materials, artifact = self._ready_artifact()
        first = self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertTrue(first.ok, first.error)
        artifact.content["body"] = str(artifact.content.get("body", "")) + " tampered"
        changed = self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertFalse(changed.ok)
        self.assertIn("package changed", changed.error)
        self.assertEqual(
            self.env.db.fetchone("SELECT COUNT(*) AS n FROM approvals")["n"], 1
        )

    def test_submit_rejects_reordered_images(self):
        materials, artifact = self._ready_artifact()
        self._add_second_image(artifact)
        first = self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertTrue(first.ok, first.error)
        artifact.images[0], artifact.images[1] = artifact.images[1], artifact.images[0]
        changed = self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertFalse(changed.ok)
        self.assertIn("package changed", changed.error)
        self.assertEqual(
            self.env.db.fetchone("SELECT COUNT(*) AS n FROM approvals")["n"], 1
        )

    def test_idempotent_submit_does_not_duplicate_approval_notification(self):
        materials, artifact = self._ready_artifact()
        first = self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertTrue(first.ok, first.error)
        second = self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertTrue(second.ok, second.error)
        self.assertEqual(
            self.env.db.fetchone("SELECT COUNT(*) AS n FROM approvals")["n"], 1
        )
        self.assertEqual(
            self.env.db.fetchone(
                "SELECT COUNT(*) AS n FROM notifications WHERE kind='approval'"
            )["n"],
            1,
        )

    def test_writer_uses_last_allowed_call_then_blocks_next(self):
        materials, artifact = self._ready_artifact()
        ids = [m.material_id for m in materials]
        self.runtime.model_calls = self.runtime.model_limit - 1
        first = self.runtime.draft_artifact(brief=self.brief, material_ids=ids)
        self.assertIsNotNone(first)
        self.assertEqual(self.runtime.model_calls, self.runtime.model_limit)
        with self.assertRaises(BudgetExceeded):
            self.runtime.draft_artifact(brief=self.brief, material_ids=ids)
        self.assertEqual(self.runtime.model_calls, self.runtime.model_limit)

    def test_writer_reservation_refuses_before_provider_call_at_limit(self):
        materials, artifact = self._ready_artifact()
        self.runtime.model_calls = self.runtime.model_limit
        with self.assertRaises(BudgetExceeded):
            self.runtime.draft_artifact(
                brief=self.brief,
                material_ids=[m.material_id for m in materials],
            )
        self.assertEqual(self.runtime.model_calls, self.runtime.model_limit)

    def test_transport_attempts_each_reserve_before_http(self):
        self.env.config.secrets.deepseek_api_key = "offline-test-key"
        self.env.config.data.setdefault("llm", {})["max_retries"] = 1
        llm = DeepSeekLLM(
            self.env.config, self.env.clock,
            BudgetGuard(self.env.db, self.env.config, self.env.clock),
            sleep=lambda _: None,
        )
        seen = {"reserve": 0, "post": 0}

        def before_attempt():
            seen["reserve"] += 1
            if seen["reserve"] > 1:
                raise BudgetExceeded("creator agent model call limit reached")

        def post(body, key):
            seen["post"] += 1
            raise _RetryableHTTP("simulated HTTP 500")

        with patch.object(llm, "_post", side_effect=post):
            with self.assertRaises(BudgetExceeded):
                llm.complete_json(
                    purpose="draft", payload={"topic": "x"},
                    before_attempt=before_attempt,
                )
        self.assertEqual(seen["reserve"], 2)
        self.assertEqual(seen["post"], 1)

    def test_materials_survive_runtime_restart(self):
        materials = self.runtime.search("Claude", limit=2)
        self.assertEqual(len(materials), 1)
        restarted = LiveRuntime(
            db=self.env.db, config=self.env.config, clock=self.env.clock,
            http=self.env.http,
            budget=BudgetGuard(self.env.db, self.env.config, self.env.clock),
            llm=self.env.llm, store=self.store, run_key=self.brief.run_key,
            search_factory=lambda q, limit, mode: FakeSearchSource(),
        )
        self.assertIn("M1", restarted._materials)
        self.assertIn("fable", restarted._bodies["M1"])
        self.assertEqual(
            restarted.store.material_body(self.brief.run_key, "M1"),
            restarted._bodies["M1"],
        )

    def test_v2_revision_binds_previous_artifact_and_preserves_images(self):
        materials, artifact = self._ready_artifact()
        first = self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertTrue(first.ok, first.error)
        old_approval = self.env.db.fetchone("SELECT * FROM approvals")
        self.assertEqual(old_approval["status"], "pending")
        old_hashes = [image.get("sha256") for image in artifact.images]

        accepted = handle_inbound_message(
            self.env.db, self.env.config, self.env.clock,
            message_id="v2-revision-1", sender_id="owner-open-id",
            chat_id="owner-open-id", chat_type="p2p",
            text="v2 修改 调整配图排版，保留第一张图",
        )
        self.assertTrue(accepted["ok"], accepted)
        self.assertEqual(accepted["outcome"], "creator_revision_queued")
        brief = accepted["detail"]["brief"]
        self.assertEqual(brief["origin"], "revision")
        self.assertEqual(brief["base_artifact_version"], artifact.version)
        self.assertEqual(brief["generation"], 2)
        self.assertEqual(brief["must_keep"], ["image:1"])
        refreshed = self.env.db.fetchone("SELECT * FROM approvals WHERE id=?", (old_approval["id"],))
        self.assertEqual(refreshed["status"], "revision_pending")
        revision_brief = CreativeBrief(**brief)
        self.store.ensure_run(revision_brief)
        parent_materials = self.runtime.load_parent_artifact(artifact)
        self.assertTrue(parent_materials)
        revised = self.runtime.revise_artifact(
            brief=revision_brief, base_artifact=artifact,
            instruction="调整配图排版，保留第二张图",
            material_ids=[m.material_id for m in parent_materials],
        )
        self.assertEqual([image.get("sha256") for image in revised.images], old_hashes)

    def test_revision_writer_gets_larger_output_budget(self):
        materials, artifact = self._ready_artifact()
        brief = CreativeBrief(
            run_key="ca-revision-budget", account_id=self.brief.account_id,
            origin="revision", topic=self.brief.topic, instruction="调整配图排版",
            generation=2, base_artifact_version=artifact.version,
            parent_run_key=self.brief.run_key,
        )
        self.store.ensure_run(brief)
        seen: list[int] = []
        original = self.env.llm.complete_json

        def capture(*, purpose, payload, max_output_tokens=1800, before_attempt=None):
            seen.append(max_output_tokens)
            return original(
                purpose=purpose, payload=payload,
                max_output_tokens=max_output_tokens, before_attempt=before_attempt,
            )

        with patch.object(self.env.llm, "complete_json", side_effect=capture):
            self.runtime.revise_artifact(
                brief=brief, base_artifact=artifact, instruction="调整配图排版",
                material_ids=[m.material_id for m in materials],
            )
        self.assertEqual(seen, [2600])

    def test_revision_inherits_parent_run_materials_with_original_ids(self):
        materials, artifact = self._ready_artifact()
        loaded = self.runtime.load_parent_artifact(artifact)
        self.assertEqual(
            [m.material_id for m in loaded],
            [m.material_id for m in materials],
        )
        for material in loaded:
            self.assertEqual(
                self.runtime._bodies[material.material_id],
                self.store.material_body(artifact.run_key, material.material_id),
            )

    def test_revision_evidence_keeps_full_body_past_900_chars(self):
        body = "导航。" + ("前文" * 500) + "单次前台输出最多 65,536 tokens。"
        self.runtime._materials["M1"] = Material(
            material_id="M1", url="https://example.com/long", title="Long",
            content_hash="h", quote="导航。",
        )
        self.runtime._bodies["M1"] = body
        evidence, _ids = self.runtime._evidence(["M1"])
        self.assertGreater(len(evidence[0]["quote"]), 900)
        self.assertIn("65,536", evidence[0]["quote"])
        claims = self.runtime._claims_from_content(
            SimpleNamespace(
                claims=[
                    SimpleNamespace(
                        text="单次前台输出最多 65,536 tokens",
                        evidence_ids=["M1"],
                        core=False,
                    )
                ]
            ),
            ["M1"],
        )
        self.assertEqual(claims[0].status, "supported")

    def test_revision_keeps_number_supported_by_long_parent_body(self):
        materials, artifact = self._ready_artifact()
        material_id = materials[0].material_id
        number = "单次前台输出最多 65,536 tokens"
        body = "导航。" + ("前文" * 500) + number + "。" + self.runtime._bodies[material_id]
        self.runtime._bodies[material_id] = body
        artifact.content["caption"] = [number]
        artifact.content["claims"] = [
            {"text": number, "type": "fact", "evidence_ids": [material_id]}
        ]
        brief = CreativeBrief(
            run_key="ca-revision-long-number", account_id=self.brief.account_id,
            origin="revision", topic=self.brief.topic, instruction="调整配图排版",
            generation=2, base_artifact_version=artifact.version,
            parent_run_key=self.brief.run_key,
        )
        self.store.ensure_run(brief)
        revised = self.runtime.revise_artifact(
            brief=brief, base_artifact=artifact, instruction="调整配图排版",
            material_ids=[material_id],
        )
        self.assertIn("65,536", "\n".join(revised.content["caption"]))

    def test_registry_preserves_structured_issue_data(self):
        def fail(args, context):
            raise DraftValidationError(
                "bad draft",
                issues=[{"field": "caption", "type": "numeric", "snippet": "65,536"}],
                candidate={"title": "candidate"},
            )

        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="fail_revision", description="fail", schema={}, handler=fail,
        ))
        result = registry.execute("fail_revision", {}, None)
        self.assertFalse(result.ok)
        self.assertIn("DraftValidationError", result.error)
        self.assertEqual(result.data["issues"][0]["type"], "numeric")
        self.assertEqual(result.data["candidate"]["title"], "candidate")

    def test_revision_rejects_in_flight_publication(self):
        _materials, artifact = self._ready_artifact()
        first = self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertTrue(first.ok, first.error)
        approval = self.env.db.fetchone("SELECT * FROM approvals ORDER BY id DESC LIMIT 1")
        candidate = {
            "run_key": self.brief.run_key, "version": artifact.version,
            "draft_id": approval["draft_id"], "generation": 1,
            "brief_json": json.dumps({"topic": self.brief.topic}, ensure_ascii=False),
            "account_id": approval["account_id"], "approval_id": approval["id"],
            "approval_code": approval["code"], "approval_status": "pending",
            "active_publication": 1,
        }
        with patch(
            "bot.services.creator_agent.entry._revision_candidates",
            return_value=[candidate],
        ):
            accepted = handle_inbound_message(
                self.env.db, self.env.config, self.env.clock,
                message_id="modify-in-flight-1", sender_id="owner-open-id",
                chat_id="owner-open-id", chat_type="p2p",
                text=f"修改 {approval['code']} 标题改短一点",
            )
        self.assertFalse(accepted["ok"])
        self.assertEqual(accepted["outcome"], "revision_in_flight")
        note = self.env.db.fetchone(
            "SELECT payload_json FROM notifications WHERE dedupe_key=?",
            ("inbound:modify-in-flight-1",),
        )
        self.assertIsNotNone(note)
        self.assertIn("不能就地换稿", note["payload_json"])

    def test_modify_xh_code_uses_creator_revision_and_queues_ack(self):
        _materials, artifact = self._ready_artifact()
        submitted = self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertTrue(submitted.ok, submitted.error)
        code = submitted.data["approval_code"]
        accepted = handle_inbound_message(
            self.env.db, self.env.config, self.env.clock,
            message_id="modify-xh-1", sender_id="owner-open-id",
            chat_id="owner-open-id", chat_type="p2p",
            text=f"修改 {code} 标题改短一点",
        )
        self.assertTrue(accepted["ok"], accepted)
        self.assertEqual(accepted["outcome"], "creator_revision_queued")
        row = self.env.db.fetchone(
            "SELECT status FROM approvals WHERE code=?", (code,)
        )
        self.assertEqual(row["status"], "revision_pending")
        self.assertEqual(
            self.env.db.fetchone(
                "SELECT COUNT(*) AS n FROM jobs WHERE kind='revise'"
            )["n"],
            0,
        )
        note = self.env.db.fetchone(
            "SELECT payload_json FROM notifications WHERE dedupe_key=?",
            ("inbound:modify-xh-1",),
        )
        self.assertIsNotNone(note)
        self.assertIn(code, note["payload_json"])
        self.assertIn("重新送审", note["payload_json"])

    def test_second_revision_waits_for_the_first_queued_revision(self):
        _materials, artifact = self._ready_artifact()
        submitted = self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertTrue(submitted.ok, submitted.error)
        code = submitted.data["approval_code"]
        first = handle_inbound_message(
            self.env.db, self.env.config, self.env.clock,
            message_id="modify-race-1", sender_id="owner-open-id",
            chat_id="owner-open-id", chat_type="p2p",
            text=f"修改 {code} 调整配图排版",
        )
        self.assertTrue(first["ok"], first)
        second = handle_inbound_message(
            self.env.db, self.env.config, self.env.clock,
            message_id="modify-race-2", sender_id="owner-open-id",
            chat_id="owner-open-id", chat_type="p2p",
            text=f"修改 {code} 标题改短一点",
        )
        self.assertFalse(second["ok"])
        self.assertEqual(second["outcome"], "revision_already_running")
        self.assertEqual(
            self.env.db.fetchone(
                "SELECT COUNT(*) AS n FROM jobs WHERE kind='creator_agent_run'"
            )["n"],
            1,
        )
        note = self.env.db.fetchone(
            "SELECT payload_json FROM notifications WHERE dedupe_key=?",
            ("inbound:modify-race-2",),
        )
        self.assertIsNotNone(note)
        self.assertIn("已有一轮修改正在进行", note["payload_json"])

    def test_modify_without_code_asks_which_work_when_ambiguous(self):
        _materials, artifact = self._ready_artifact()
        submitted = self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertTrue(submitted.ok, submitted.error)
        first = self.env.db.fetchone("SELECT * FROM approvals ORDER BY id DESC LIMIT 1")
        second_code = "XH-AMBIG2"
        candidates = [
            {
                "run_key": self.brief.run_key, "version": artifact.version,
                "draft_id": first["draft_id"], "generation": 1,
                "brief_json": json.dumps({"topic": "第一篇"}, ensure_ascii=False),
                "account_id": first["account_id"], "approval_id": first["id"],
                "approval_code": first["code"], "approval_status": "pending",
                "active_publication": None,
            },
            {
                "run_key": "ca-ambiguity-second", "version": 1,
                "draft_id": first["draft_id"] + 1, "generation": 1,
                "brief_json": json.dumps({"topic": "第二篇"}, ensure_ascii=False),
                "account_id": first["account_id"], "approval_id": first["id"] + 1,
                "approval_code": second_code, "approval_status": "pending",
                "active_publication": None,
            },
        ]
        with patch(
            "bot.services.creator_agent.entry._revision_candidates",
            return_value=candidates,
        ):
            accepted = handle_inbound_message(
                self.env.db, self.env.config, self.env.clock,
                message_id="modify-ambiguous-1", sender_id="owner-open-id",
                chat_id="owner-open-id", chat_type="p2p",
                text="修改 标题改短一点",
            )
        self.assertFalse(accepted["ok"])
        self.assertEqual(accepted["outcome"], "revision_ambiguous")
        note = self.env.db.fetchone(
            "SELECT payload_json FROM notifications WHERE dedupe_key=?",
            ("inbound:modify-ambiguous-1",),
        )
        self.assertIsNotNone(note)
        self.assertIn(second_code, note["payload_json"])
        self.assertIn(first["code"], note["payload_json"])

    def test_revision_without_visible_change_returns_structured_issue(self):
        materials, artifact = self._ready_artifact()
        brief = CreativeBrief(
            run_key="ca-revision-noop", account_id=self.brief.account_id,
            origin="revision", topic=self.brief.topic, instruction="保持原样",
            generation=2, base_artifact_version=artifact.version,
            parent_run_key=self.brief.run_key,
        )
        self.store.ensure_run(brief)
        with self.assertRaises(DraftValidationError) as caught:
            self.runtime.revise_artifact(
                brief=brief, base_artifact=artifact, instruction="保持原样",
                material_ids=[m.material_id for m in materials],
            )
        self.assertEqual(caught.exception.issues[0]["type"], "no_change")
        self.assertEqual(
            self.env.db.fetchone(
                "SELECT COUNT(*) AS n FROM creator_agent_artifacts WHERE run_key=?",
                (brief.run_key,),
            )["n"],
            0,
        )

    def test_title_only_revision_keeps_caption_and_storyboard(self):
        materials, artifact = self._ready_artifact()
        artifact.title = "这是一条很长很长需要被缩短的小红书标题"
        artifact.content["title"] = artifact.title
        brief = CreativeBrief(
            run_key="ca-revision-title", account_id=self.brief.account_id,
            origin="revision", topic=self.brief.topic, instruction="标题改短一点",
            generation=2, base_artifact_version=artifact.version,
            parent_run_key=self.brief.run_key,
        )
        self.store.ensure_run(brief)
        revised = self.runtime.revise_artifact(
            brief=brief, base_artifact=artifact, instruction="标题改短一点",
            material_ids=[m.material_id for m in materials],
        )
        self.assertNotEqual(revised.title, artifact.title)
        self.assertEqual(revised.content["caption"], artifact.content["caption"])
        self.assertEqual(revised.content["storyboard"], artifact.content["storyboard"])

    def test_lowercase_command_prefix_is_stripped(self):
        brief = brief_from_text(
            account_id="owner-open-id",
            message_id="lowercase-v2",
            text="v2 选题 DeepSeek 开放 App 能力 API，扩展调用另收费",
        )
        self.assertEqual(brief.topic, "DeepSeek 开放 App 能力 API，扩展调用另收费")
        self.assertNotIn("v2", brief.topic.lower())

    def test_text_only_revision_keeps_previous_images_for_submit(self):
        materials, artifact = self._ready_artifact()
        self._add_second_image(artifact)
        submitted = self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertTrue(submitted.ok, submitted.error)
        accepted = handle_inbound_message(
            self.env.db, self.env.config, self.env.clock,
            message_id="v2-revision-no-must-keep", sender_id="owner-open-id",
            chat_id="owner-open-id", chat_type="p2p",
            text="V2 修改 调整配图排版",
        )
        self.assertTrue(accepted["ok"], accepted)
        revision_brief = CreativeBrief(**accepted["detail"]["brief"])
        self.store.ensure_run(revision_brief)
        revised = self.runtime.revise_artifact(
            brief=revision_brief, base_artifact=artifact,
            instruction="调整配图排版",
            material_ids=[m.material_id for m in self.runtime.load_parent_artifact(artifact)],
        )
        self.assertEqual(len(revised.images), 2)
        self.assertEqual([img.get("preserved") for img in revised.images], [False, False])

    def test_failed_run_queues_one_owner_visible_outcome(self):
        brief = CreativeBrief(
            run_key="ca-failed-outcome", account_id="owner-open-id",
            origin="manual", topic="失败通知测试",
        )
        enqueue_brief(self.env.db, self.env.clock, brief)
        job = self.env.db.fetchone(
            "SELECT * FROM jobs WHERE kind='creator_agent_run' ORDER BY id DESC LIMIT 1"
        )
        result = run_job(
            self.env.db, self.env.config, self.env.clock, job,
            runner_factory=lambda _brief: type(
                "FailingRunner", (), {
                    "run": lambda _self, b: AgentResult(
                        run_key=b.run_key, status="failed", steps=8,
                        error="step limit reached",
                    )
                }
            )(),
        )
        self.assertEqual(result.status, "failed")
        rows = self.env.db.fetchall(
            "SELECT * FROM notifications WHERE dedupe_key=?",
            (f"creator-outcome:{brief.run_key}:failed",),
        )
        self.assertEqual(len(rows), 1)
        self.assertIn("step limit reached", rows[0]["payload_json"])

    def test_submit_rejects_unsupported_number(self):
        materials, artifact = self._ready_artifact()
        material = Material(
            material_id="M-number", url="https://example.com/numbers", title="numbers",
            role="official", content_hash="h", quote="效率提升 10 percent",
        )
        self.runtime._materials["M-number"] = material
        artifact.claims = [
            ClaimAssessment(
                claim_id="C-number", text="效率提升 999 percent", status="supported",
                material_id="M-number", quote="效率提升 10 percent",
            )
        ]
        result = self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertFalse(result.ok)
        self.assertIn("number", result.error.lower())
        self.assertEqual(
            self.env.db.fetchone("SELECT COUNT(*) AS n FROM approvals")["n"], 0
        )

    def test_submit_resumes_existing_draft_without_creating_another(self):
        materials, artifact = self._ready_artifact()
        first = self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertTrue(first.ok, first.error)
        # Simulate a crash after the draft pointer was saved but before the
        # approval existed.  A retry must finalize that same draft.
        with self.env.db.tx() as conn:
            conn.execute("DELETE FROM approvals WHERE draft_id=?", (first.data["draft_id"],))
        retry = self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertTrue(retry.ok, retry.error)
        self.assertEqual(first.data["draft_id"], retry.data["draft_id"])
        self.assertEqual(
            self.env.db.fetchone("SELECT COUNT(*) AS n FROM drafts")["n"], 1
        )
        self.assertEqual(
            self.env.db.fetchone("SELECT COUNT(*) AS n FROM approvals")["n"], 1
        )
        lineage = self.env.db.fetchone(
            "SELECT draft_id, approval_id, result_code FROM topic_requests "
            "WHERE message_id LIKE 'v2-submit:%'"
        )
        self.assertEqual(lineage["draft_id"], first.data["draft_id"])
        self.assertEqual(lineage["result_code"], retry.data["approval_code"])
        self.assertIsNotNone(lineage["approval_id"])

    def test_stale_generation_cannot_resume_an_old_draft(self):
        materials, artifact = self._ready_artifact()
        first = self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertTrue(first.ok, first.error)
        with self.env.db.tx() as conn:
            conn.execute("DELETE FROM approvals WHERE draft_id=?", (first.data["draft_id"],))
            conn.execute(
                "UPDATE creator_agent_runs SET generation=2 WHERE run_key=?",
                (self.brief.run_key,),
            )
        stale = self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertFalse(stale.ok)
        self.assertIn("stale generation", stale.error)
        self.assertEqual(
            self.env.db.fetchone("SELECT COUNT(*) AS n FROM approvals")["n"], 0
        )

    def test_submit_recovers_after_finalize_crash_without_duplicate_draft(self):
        materials, artifact = self._ready_artifact()
        with patch(
            "bot.services.creator_agent.live._finalize_with_approval",
            side_effect=RuntimeError("simulated approval crash"),
        ):
            with self.assertRaises(RuntimeError):
                self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertEqual(
            self.env.db.fetchone("SELECT COUNT(*) AS n FROM drafts")["n"], 1
        )
        self.assertEqual(
            self.env.db.fetchone("SELECT COUNT(*) AS n FROM approvals")["n"], 0
        )
        retry = self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertTrue(retry.ok, retry.error)
        self.assertEqual(
            self.env.db.fetchone("SELECT COUNT(*) AS n FROM drafts")["n"], 1
        )
        self.assertEqual(
            self.env.db.fetchone("SELECT COUNT(*) AS n FROM approvals")["n"], 1
        )

    def test_approval_created_then_crash_is_idempotently_reused(self):
        materials, artifact = self._ready_artifact()
        from bot.services.creator_agent import live as live_module

        original = live_module._finalize_with_approval

        def finalize_then_crash(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("simulated crash after approval insert")

        with patch.object(live_module, "_finalize_with_approval", side_effect=finalize_then_crash):
            with self.assertRaises(RuntimeError):
                self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertEqual(
            self.env.db.fetchone("SELECT COUNT(*) AS n FROM approvals")["n"], 1
        )
        retry = self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertTrue(retry.ok, retry.error)
        self.assertEqual(
            self.env.db.fetchone("SELECT COUNT(*) AS n FROM drafts")["n"], 1
        )
        self.assertEqual(
            self.env.db.fetchone("SELECT COUNT(*) AS n FROM approvals")["n"], 1
        )
        lineage = self.env.db.fetchone(
            "SELECT draft_id, approval_id FROM topic_requests WHERE message_id LIKE 'v2-submit:%'"
        )
        self.assertEqual(lineage["draft_id"], retry.data["draft_id"])
        self.assertIsNotNone(lineage["approval_id"])

    def test_pointer_crash_is_recoverable_from_submission_key(self):
        materials, artifact = self._ready_artifact()
        with patch.object(
            self.store, "save_artifact", side_effect=RuntimeError("simulated pointer crash")
        ):
            with self.assertRaises(RuntimeError):
                self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertEqual(
            self.env.db.fetchone("SELECT COUNT(*) AS n FROM drafts")["n"], 1
        )
        # Reload from the store: draft_id was never persisted.
        reloaded = self.store.artifact(self.brief.run_key, artifact.version)
        self.assertIsNotNone(reloaded)
        self.assertFalse(reloaded.draft_id)
        retry = self.runtime.submit_for_approval(brief=self.brief, artifact=reloaded)
        self.assertTrue(retry.ok, retry.error)
        self.assertEqual(
            self.env.db.fetchone("SELECT COUNT(*) AS n FROM drafts")["n"], 1
        )
        self.assertEqual(
            self.env.db.fetchone("SELECT COUNT(*) AS n FROM approvals")["n"], 1
        )

    def test_run_cancelled_after_precheck_is_rejected_inside_final_transaction(self):
        materials, artifact = self._ready_artifact()
        from bot.services.creator_agent import live as live_module

        original = live_module._persist_artifact_images

        def cancel_then_persist(draft, images):
            with self.env.db.tx() as conn:
                conn.execute(
                    "UPDATE creator_agent_runs SET status='cancelled' WHERE run_key=?",
                    (self.brief.run_key,),
                )
            return original(self.env.db, self.env.config, self.env.clock, draft, images)

        with patch.object(live_module, "_persist_artifact_images", side_effect=cancel_then_persist):
            with self.assertRaises(Exception) as ctx:
                self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertIn("cancelled", str(ctx.exception))
        self.assertEqual(
            self.env.db.fetchone("SELECT COUNT(*) AS n FROM approvals")["n"], 0
        )

    def test_render_rejects_missing_preserved_asset(self):
        materials, artifact = self._ready_artifact()
        second = self._add_second_image(artifact)
        second["preserved"] = True
        second["asset_id"] = "image:2"
        second["path"] = str(self.env.root / "does-not-exist.png")
        with self.assertRaises(RuntimeError) as ctx:
            self.runtime.render_images(brief=self.brief, artifact=artifact)
        self.assertIn("preserved image 2", str(ctx.exception))
        self.assertEqual(
            self.env.db.fetchone("SELECT COUNT(*) AS n FROM approvals")["n"], 0
        )

    def test_render_rejects_dropped_preserved_page(self):
        materials, artifact = self._ready_artifact()
        second = self._add_second_image(artifact)
        second["preserved"] = True
        second["asset_id"] = "image:2"

        def one_page(content, out_dir, draft_key):
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{draft_key}-01.png"
            Image.new("RGB", (108, 144), (1, 2, 3)).save(path)
            return [RenderedImage(ordinal=1, path=path, sha256=sha256_file(path), width=108, height=144)]

        self.runtime.renderer = one_page
        with self.assertRaises(RuntimeError) as ctx:
            self.runtime.render_images(brief=self.brief, artifact=artifact)
        self.assertIn("dropped preserved", str(ctx.exception))

    def test_restart_seeds_model_and_fetch_counters(self):
        self.store.update_run(
            self.brief.run_key,
            counters={"_runtime_model_calls": 4, "physical_fetches": 5},
        )
        restarted = LiveRuntime(
            db=self.env.db, config=self.env.config, clock=self.env.clock,
            http=self.env.http,
            budget=BudgetGuard(self.env.db, self.env.config, self.env.clock),
            llm=self.env.llm, store=self.store, run_key=self.brief.run_key,
            search_factory=lambda q, limit, mode: FakeSearchSource(),
        )
        self.assertEqual(restarted.model_calls, 4)
        self.assertEqual(restarted.fetch_quota.attempts, 5)
        restarted._reserve_model_call()
        self.assertEqual(restarted.model_calls, 5)

    def test_semantic_review_must_pass_before_submit(self):
        materials, artifact = self._ready_artifact()
        self.env.config.data.setdefault("creator_agent", {})["semantic_review"] = True
        original = self.env.llm.complete_json

        def fake_review(*, purpose, payload, max_output_tokens=1800):
            if purpose == "draft_review":
                return SimpleNamespace(
                    data={
                        "verdict": "revise",
                        "summary": "citation mismatch",
                        "problems": [{
                            "kind": "evidence",
                            "evidence_ids": ["M1"],
                            "instruction": "第 2 段数字与引文不一致，请修正",
                        }],
                        "confidence": 0.8,
                    },
                    provider="fake",
                    model="fake",
                )
            return original(purpose=purpose, payload=payload,
                            max_output_tokens=max_output_tokens)

        with patch.object(self.env.llm, "complete_json", side_effect=fake_review):
            review = self.runtime.review_artifact(
                brief=self.brief, artifact=artifact, materials=materials
            )
            self.assertEqual(review.verdict, "revise")
            result = self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertFalse(result.ok)
        self.assertIn("semantic review", result.error)
        self.assertEqual(
            self.env.db.fetchone("SELECT COUNT(*) AS n FROM approvals")["n"], 0
        )

    def test_revision_preserves_only_named_image_in_final_manifest(self):
        calls = {"n": 0}

        def render_two(content, out_dir, draft_key):
            calls["n"] += 1
            first_color = (240, 80, 80) if calls["n"] == 1 else (80, 220, 80)
            out_dir.mkdir(parents=True, exist_ok=True)
            out = []
            for ordinal, color in ((1, first_color), (2, (80, 120, 240))):
                path = out_dir / f"{draft_key}-{ordinal:02d}.png"
                Image.new("RGB", (108, 144), color).save(path)
                out.append(RenderedImage(
                    ordinal=ordinal, path=path, sha256=sha256_file(path),
                    width=108, height=144,
                ))
            return out

        materials, artifact = self._ready_artifact()
        self.runtime.renderer = render_two
        artifact = self.runtime.render_images(brief=self.brief, artifact=artifact)
        base_hashes = {image["ordinal"]: image["sha256"] for image in artifact.images}
        first = self.runtime.submit_for_approval(brief=self.brief, artifact=artifact)
        self.assertTrue(first.ok, first.error)

        accepted = handle_inbound_message(
            self.env.db, self.env.config, self.env.clock,
            message_id="v2-revision-image-2", sender_id="owner-open-id",
            chat_id="owner-open-id", chat_type="p2p",
            text="V2 修改 调整配图排版，改第一张，第二张原样保留",
        )
        self.assertTrue(accepted["ok"], accepted)
        revision_brief = CreativeBrief(**accepted["detail"]["brief"])
        self.store.ensure_run(revision_brief)
        revision_runtime = LiveRuntime(
            db=self.env.db, config=self.env.config, clock=self.env.clock,
            http=self.env.http,
            budget=BudgetGuard(self.env.db, self.env.config, self.env.clock),
            llm=self.env.llm, store=self.store, run_key=revision_brief.run_key,
            search_factory=lambda q, limit, mode: FakeSearchSource(),
        )
        revision_runtime.renderer = render_two
        revised = revision_runtime.revise_artifact(
            brief=revision_brief, base_artifact=artifact,
            instruction="调整配图排版，改第一张，第二张原样保留",
            material_ids=[m.material_id for m in revision_runtime.load_parent_artifact(artifact)],
        )
        revised = revision_runtime.render_images(brief=revision_brief, artifact=revised)
        submitted = revision_runtime.submit_for_approval(brief=revision_brief, artifact=revised)
        self.assertTrue(submitted.ok, submitted.error)
        rows = self.env.db.fetchall(
            "SELECT ordinal, sha256 FROM draft_images WHERE draft_id=? ORDER BY ordinal",
            (submitted.data["draft_id"],),
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["sha256"], base_hashes[2])
        self.assertNotEqual(rows[0]["sha256"], base_hashes[1])

    def test_inbound_worker_revision_uses_parent_and_preserves_image(self):
        render_count = {"n": 0}

        def render_two(content, out_dir, draft_key):
            render_count["n"] += 1
            out_dir.mkdir(parents=True, exist_ok=True)
            out = []
            first_color = (240, 80, 80) if render_count["n"] == 1 else (80, 220, 120)
            for ordinal, color in ((1, first_color), (2, (80, 120, 240))):
                path = out_dir / f"{draft_key}-{ordinal:02d}.png"
                Image.new("RGB", (108, 144), color).save(path)
                out.append(RenderedImage(
                    ordinal=ordinal, path=path, sha256=sha256_file(path),
                    width=108, height=144,
                ))
            return out

        self.runtime.renderer = render_two
        materials, parent = self._ready_artifact()
        parent_hashes = {image["ordinal"]: image["sha256"] for image in parent.images}
        first = self.runtime.submit_for_approval(brief=self.brief, artifact=parent)
        self.assertTrue(first.ok, first.error)

        accepted = handle_inbound_message(
            self.env.db, self.env.config, self.env.clock,
            message_id="v2-worker-revision-1", sender_id="owner-open-id",
            chat_id="owner-open-id", chat_type="p2p",
            text="V2 修改 调整配图排版，保留第二张图",
        )
        self.assertTrue(accepted["ok"], accepted)
        brief = CreativeBrief(**accepted["detail"]["brief"])
        self.store.ensure_run(brief)
        decision_llm = RevisionDecisions(self.env.llm, parent.version)

        def factory(run_brief):
            store = AgentStore(self.env.db, self.env.clock)
            runtime = LiveRuntime(
                db=self.env.db, config=self.env.config, clock=self.env.clock,
                http=self.env.http,
                budget=BudgetGuard(self.env.db, self.env.config, self.env.clock),
                llm=decision_llm, store=store, run_key=run_brief.run_key,
                search_factory=lambda q, limit, mode: FakeSearchSource(),
            )
            runtime.renderer = render_two
            base = store.artifact(
                str(run_brief.parent_run_key), int(run_brief.base_artifact_version)
            )
            parent_materials = runtime.load_parent_artifact(base)
            context = AgentContext(
                brief=run_brief, runtime=runtime, store=store, base_artifact=base
            )
            for material in parent_materials:
                context.add_material(material)
            registry = ToolRegistry()
            register_tools(registry, runtime)
            return AgentRunner(
                db=self.env.db, config=self.env.config, clock=self.env.clock,
                llm=decision_llm, registry=registry, context=context,
            )

        job = self.env.db.fetchone(
            "SELECT * FROM jobs WHERE id=?", (accepted["detail"]["job_id"],)
        )
        result = run_job(
            self.env.db, self.env.config, self.env.clock, job,
            runner_factory=factory,
        )
        self.assertEqual(result.status, "completed", result.error)
        observation = decision_llm.observations[0]
        self.assertEqual(observation["parent_artifact"]["version"], parent.version)
        self.assertTrue(observation["materials"])
        self.assertTrue(
            any(str(m["material_id"]).startswith("M") for m in observation["materials"])
        )

        approvals = self.env.db.fetchall(
            "SELECT status FROM approvals WHERE draft_id=? ORDER BY id",
            (first.data["draft_id"],),
        )
        self.assertEqual(approvals[0]["status"], "revision_pending")
        new_approval = self.env.db.fetchone(
            "SELECT * FROM approvals WHERE draft_id<>? ORDER BY id DESC LIMIT 1",
            (first.data["draft_id"],),
        )
        self.assertEqual(new_approval["status"], "pending")
        # The revision must preserve image 2 byte-for-byte in its final draft.
        revised_draft = self.env.db.fetchone(
            "SELECT id FROM drafts WHERE topic_request_id IS NOT NULL ORDER BY id DESC LIMIT 1"
        )
        self.assertIsNotNone(revised_draft)
        final = self.env.db.fetchall(
            "SELECT ordinal, sha256 FROM draft_images WHERE draft_id=? ORDER BY ordinal",
            (revised_draft["id"],),
        )
        self.assertEqual(len(final), 2)
        self.assertEqual(final[1]["sha256"], parent_hashes[2])
        self.assertNotEqual(final[0]["sha256"], parent_hashes[1])

    def test_v2_feishu_route_queues_one_job_and_dedupes(self):
        kwargs = dict(
            message_id="v2-route-1", sender_id="owner-open-id", chat_id="owner-open-id",
            chat_type="p2p", text="V2 选题 Claude Code 选模型",
        )
        first = handle_inbound_message(
            self.env.db, self.env.config, self.env.clock, **kwargs
        )
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["outcome"], "creator_agent_queued")
        second = handle_inbound_message(
            self.env.db, self.env.config, self.env.clock, **kwargs
        )
        self.assertTrue(second.get("duplicate"), second)
        jobs = self.env.db.fetchall(
            "SELECT * FROM jobs WHERE kind='creator_agent_run'"
        )
        self.assertEqual(len(jobs), 1)

    def test_plain_topic_routes_to_v2_without_legacy_request(self):
        result = handle_inbound_message(
            self.env.db,
            self.env.config,
            self.env.clock,
            message_id="v2-plain-topic-1",
            sender_id="owner-open-id",
            chat_id="owner-open-id",
            chat_type="p2p",
            text="选题 Claude Code 选模型",
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["outcome"], "creator_agent_queued")
        jobs = self.env.db.fetchall(
            "SELECT * FROM jobs WHERE kind='creator_agent_run'"
        )
        self.assertEqual(len(jobs), 1)
        legacy = self.env.db.fetchall(
            "SELECT * FROM topic_requests WHERE message_id='v2-plain-topic-1'"
        )
        self.assertEqual(legacy, [])

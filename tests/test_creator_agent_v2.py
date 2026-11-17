"""Offline acceptance tests for the model-driven Creator Agent V2 loop."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase

from bot.services.creator_agent.entry import accept_inbound, run_job, scheduled_tick
from bot.services.creator_agent.context import AgentContext
from bot.services.creator_agent.contracts import (
    ArtifactVersion,
    ClaimAssessment,
    CreativeBrief,
    Material,
    Review,
    ReviewIssue,
)
from bot.services.creator_agent.policy import claim_issues, submission_issues
from bot.services.creator_agent.registry import ToolRegistry, ToolResult
from bot.services.creator_agent.runner import AgentRunner
from bot.services.creator_agent.store import AgentStore
from bot.services.creator_agent.tools import register_tools
from tests.helpers import cleanup, new_env


class ScriptedLLM:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.calls = 0

    def complete_json(self, **kwargs):
        self.calls += 1
        if not self.decisions:
            raise RuntimeError("script exhausted")
        return SimpleNamespace(data=self.decisions.pop(0))


def mat(mid, *, role="official", content_type="body", title="", quote="", content_hash="h1"):
    return Material(
        material_id=mid,
        url=f"https://example.com/{mid}",
        title=title or mid,
        role=role,
        content_type=content_type,
        content_hash=content_hash,
        quote=quote or "Claude Code 的 fable 别名解析为 Fable 5.2。",
    )


class FakeRuntime:
    def __init__(self):
        self.materials = {
            "M1": mat("M1", title="Official docs", quote="Claude Code 的 fable 别名解析为 Fable 5.2。"),
            "M2": mat("M2", role="aggregator", content_type="summary", title="Aggregated", quote="摘要"),
        }
        self.reviewed = 0
        self.submitted = 0
        self.artifact = None

    def discover_topics(self, limit=10):
        return [{"topic": "Claude Code 选模型", "url": "https://example.com/discover"}]

    def search(self, query, limit=8, mode="news"):
        return [self.materials["M1"], self.materials["M2"]]

    def read_source(self, material_id=None, url=None):
        if material_id in self.materials:
            return self.materials[material_id]
        for m in self.materials.values():
            if m.url == url:
                return m
        raise KeyError(material_id or url)

    def inspect_claims(self, claims, material_ids):
        out = []
        for i, text in enumerate(claims):
            mid = material_ids[min(i, len(material_ids) - 1)] if material_ids else "M1"
            m = self.materials[mid]
            out.append(
                ClaimAssessment(
                    claim_id=f"C{i+1}",
                    text=str(text),
                    status="supported",
                    material_id=mid,
                    quote=m.quote,
                    rationale="quote matches",
                    expression=f"据{mid}",
                    core=i == 0,
                )
            )
        return out

    def recall_context(self, scope=""):
        return []

    def draft_artifact(self, *, brief, instruction="", material_ids=None, base_version=None):
        version = 1 if self.artifact is None else self.artifact.version + 1
        claim = ClaimAssessment(
            claim_id="C1",
            text="Fable 5.2 可由 fable 别名选择。",
            status="supported",
            material_id="M1",
            quote=self.materials["M1"].quote,
            rationale="official docs",
            expression="据官方文档",
            core=True,
        )
        self.artifact = ArtifactVersion(
            run_key=brief.run_key,
            version=version,
            title="Claude Code 选模型",
            body="Claude Code 的 fable 别名解析为 Fable 5.2。",
            tags=["AI"],
            claims=[claim],
            brief_hash=brief.brief_hash,
            parent_version=base_version,
        )
        return self.artifact

    def revise_artifact(self, *, brief, base_artifact, instruction="", revision_plan=None, material_ids=None):
        old_images = list((base_artifact.images if base_artifact else []))
        self.artifact = self.draft_artifact(
            brief=brief, instruction=instruction, material_ids=material_ids,
            base_version=base_artifact.version if base_artifact else None,
        )
        self.artifact.images = old_images
        return self.artifact

    def render_images(self, *, brief, artifact, layouts=None):
        if artifact is None:
            raise RuntimeError("no artifact")
        artifact.images = [
            {"id": "img1", "sha256": "abc123", "path": "var/drafts/run-1/01.png"},
            {"id": "img2", "sha256": "def456", "path": "var/drafts/run-1/02.png"},
        ]
        return artifact

    def inspect_layout(self, artifact):
        return [] if artifact and artifact.images else ["no images"]

    def review_artifact(self, *, brief, artifact, materials):
        self.reviewed += 1
        return Review(verdict="pass", issues=[], summary="ok")

    def submit_for_approval(self, *, brief, artifact):
        self.submitted += 1
        return ToolResult(ok=True, terminal="submit")

    def ask_owner(self, question):
        return None


def make_runner(env, runtime, decisions, *, limits=None, base_artifact=None, parent_materials=None):
    env.config.data.setdefault("creator_agent", {})
    env.config.data["creator_agent"].update(
        {"enabled": True, "max_steps": 8, "max_model_calls": 14, "max_no_progress_steps": 2}
    )
    if limits:
        env.config.data["creator_agent"].update(limits)
    brief = CreativeBrief(run_key="run-1", account_id="test-account", origin="manual", topic="Claude Code 选模型")
    store = AgentStore(env.db, env.clock)
    context = AgentContext(
        brief=brief, runtime=runtime, store=store, base_artifact=base_artifact
    )
    for material in parent_materials or []:
        context.add_material(material)
    registry = ToolRegistry()
    register_tools(registry, runtime)
    llm = ScriptedLLM(decisions)
    runner = AgentRunner(
        db=env.db, config=env.config, clock=env.clock, llm=llm, registry=registry, context=context
    )
    return runner, brief, context, store


class LoopTests(TestCase):
    def setUp(self):
        self.env = new_env("agent-v2-loop")
        self.runtime = FakeRuntime()

    def tearDown(self):
        cleanup(self.env)

    def test_observation_exposes_parent_artifact_and_parent_materials(self):
        parent = ArtifactVersion(
            run_key="parent-run",
            version=1,
            title="parent title",
            body="parent body",
            images=[{"id": "img1", "sha256": "abc", "path": "var/parent/01.png"}],
            claims=[
                ClaimAssessment(
                    claim_id="C1",
                    text="parent claim",
                    status="supported",
                    material_id="P51",
                    quote="quote",
                )
            ],
            brief_hash="h",
        )
        parent_material = Material(
            material_id="P51",
            url="https://example.com/parent",
            title="parent material",
            role="official",
            content_hash="h51",
            quote="quote",
        )
        runner, brief, _, _ = make_runner(
            self.env,
            self.runtime,
            [],
            base_artifact=parent,
            parent_materials=[parent_material],
        )
        observation = runner._observation(brief, [])
        self.assertEqual(observation["parent_artifact"]["version"], 1)
        self.assertEqual(observation["parent_artifact"]["images"][0]["asset_id"], "image:1")
        self.assertIsNone(observation["latest_artifact"])
        self.assertEqual(observation["materials"][0]["material_id"], "P51")
        self.assertEqual(observation["revision"]["instruction"], "")

    def test_dynamic_path_search_draft_render_review_submit(self):
        runner, brief, _, _ = make_runner(
            self.env,
            self.runtime,
            [
                {"action": "search", "args": {"query": "Claude Code models"}},
                {"action": "draft_artifact", "args": {"material_ids": ["M1"], "instruction": "用官方文档"}},
                {"action": "render_images", "args": {}},
                {"action": "review_artifact", "args": {}},
                {"action": "submit_for_approval", "args": {}},
            ],
        )
        result = runner.run(brief)
        self.assertEqual(result.status, "completed")
        self.assertIsNotNone(result.artifact)
        self.assertEqual(result.artifact.status, "submitted")
        self.assertEqual(self.runtime.submitted, 1)
        actions = [s["action"] for s in runner.store.steps(brief.run_key)]
        self.assertEqual(actions[:5], ["search", "draft_artifact", "render_images", "review_artifact", "submit_for_approval"])

    def test_revise_loop_is_blocked_until_render_after_no_image_review(self):
        class NoImageReviewRuntime(FakeRuntime):
            def review_artifact(self, *, brief, artifact, materials):
                self.reviewed += 1
                if self.reviewed == 1:
                    return Review(
                        verdict="revise",
                        issues=[
                            ReviewIssue(
                                issue_id="I1",
                                severity="blocking",
                                location="artifact",
                                evidence="artifact has no rendered images",
                                action="call render_images",
                            )
                        ],
                        summary="render first",
                    )
                return super().review_artifact(
                    brief=brief, artifact=artifact, materials=materials
                )

        runtime = NoImageReviewRuntime()
        runner, brief, _, _ = make_runner(
            self.env,
            runtime,
            [
                {"action": "draft_artifact", "args": {"material_ids": ["M1"]}},
                {"action": "review_artifact", "args": {}},
                {"action": "revise_artifact", "args": {"instruction": "改标题"}},
                {"action": "render_images", "args": {}},
                {"action": "review_artifact", "args": {}},
                {"action": "submit_for_approval", "args": {}},
            ],
        )
        result = runner.run(brief)
        self.assertEqual(result.status, "completed", result.error)
        actions = [(s["action"], s["status"]) for s in runner.store.steps(brief.run_key)]
        self.assertIn(("revise_artifact", "invalid"), actions)
        self.assertIn(("render_images", "ok"), actions)

    def test_consecutive_revise_without_review_is_blocked(self):
        runner, brief, _, _ = make_runner(
            self.env,
            self.runtime,
            [
                {"action": "draft_artifact", "args": {"material_ids": ["M1"]}},
                {"action": "revise_artifact", "args": {"instruction": "第一次修改"}},
                {"action": "revise_artifact", "args": {"instruction": "第二次修改"}},
                {"action": "render_images", "args": {}},
                {"action": "review_artifact", "args": {}},
                {"action": "submit_for_approval", "args": {}},
            ],
        )
        result = runner.run(brief)
        self.assertEqual(result.status, "completed", result.error)
        actions = [(s["action"], s["status"]) for s in runner.store.steps(brief.run_key)]
        self.assertEqual(actions.count(("revise_artifact", "invalid")), 1)

    def test_fabricated_material_id_never_creates_artifact(self):
        runner, brief, _, _ = make_runner(
            self.env,
            self.runtime,
            [
                {"action": "draft_artifact", "args": {"material_ids": ["M1"]}, "referenced_material_ids": ["M-ghost"]},
                {"action": "draft_artifact", "args": {"material_ids": ["M1"]}, "referenced_material_ids": ["M-ghost"]},
                {"action": "draft_artifact", "args": {"material_ids": ["M1"]}, "referenced_material_ids": ["M-ghost"]},
            ],
        )
        result = runner.run(brief)
        self.assertEqual(result.status, "failed")
        self.assertIsNone(self.runtime.artifact)

    def test_invalid_action_hits_invalid_decision_limit(self):
        runner, brief, _, _ = make_runner(
            self.env,
            self.runtime,
            [{"action": "not_a_tool", "args": {}} for _ in range(4)],
        )
        result = runner.run(brief)
        self.assertEqual(result.status, "failed")
        self.assertIn("invalid decision", result.error)

    def test_repeated_action_hits_no_progress_cap(self):
        runner, brief, _, _ = make_runner(
            self.env, self.runtime,
            [
                {"action": "search", "args": {"query": "same"}},
                {"action": "search", "args": {"query": "same"}},
                {"action": "search", "args": {"query": "same"}},
            ],
            limits={"max_no_progress_steps": 1},
        )
        result = runner.run(brief)
        self.assertEqual(result.status, "failed")
        self.assertIn("no progress", result.error)
        self.assertEqual(len(runner.store.steps(brief.run_key)), 2)

    def test_revision_preserves_image_hash(self):
        brief = CreativeBrief(run_key="run-r", account_id="test-account", origin="manual", topic="t")
        art = ArtifactVersion(
            run_key="run-r", version=1, title="t", body="body",
            images=[{"id": "img1", "sha256": "keepme"}], brief_hash=brief.brief_hash,
        )
        revised = self.runtime.revise_artifact(brief=brief, base_artifact=art, instruction="change title")
        self.assertEqual(revised.images[0]["sha256"], "keepme")


class PolicyTests(TestCase):
    def test_aggregator_summary_cannot_support_detail(self):
        m = mat("A", role="aggregator", content_type="summary", quote="摘要")
        claim = ClaimAssessment(claim_id="C1", text="100% 准确", status="supported", material_id="A", quote="摘要")
        issues = claim_issues([claim], {"A": m})
        self.assertTrue(any("aggregator" in i for i in issues))

    def test_unsupported_number_is_rejected(self):
        m = mat("M", quote="这个模型很快。")
        claim = ClaimAssessment(claim_id="C1", text="速度提升 100%", status="supported", material_id="M", quote="这个模型很快。")
        issues = claim_issues([claim], {"M": m})
        self.assertTrue(any("number" in i for i in issues))

    def test_core_contradiction_blocks_submission(self):
        m = mat("M")
        claim = ClaimAssessment(claim_id="C1", text="核心结论", status="contradicted", material_id="M", core=True)
        art = ArtifactVersion(run_key="r", version=1, title="t", body="b", images=[{"id": "x"}], claims=[claim])
        issues = submission_issues(art, {"M": m})
        self.assertTrue(any("core premise" in i for i in issues))

    def test_missing_quote_for_supported_claim(self):
        m = mat("M")
        claim = ClaimAssessment(claim_id="C1", text="claim", status="supported", material_id="M", quote="")
        issues = claim_issues([claim], {"M": m})
        self.assertTrue(any("no quote" in i for i in issues))


class SafetyTests(TestCase):
    def setUp(self):
        self.env = new_env("agent-v2-safety")
        self.runtime = FakeRuntime()

    def tearDown(self):
        cleanup(self.env)

    def test_decision_path_can_ask_owner_instead_of_submitting(self):
        runner, brief, _, _ = make_runner(
            self.env,
            self.runtime,
            [{"action": "ask_owner", "args": {"question": "请补充品牌语气"}}],
        )
        result = runner.run(brief)
        self.assertEqual(result.status, "needs_owner")
        self.assertIsNone(result.artifact)

    def test_preferences_are_scoped_to_account(self):
        store = AgentStore(self.env.db, self.env.clock)
        store.add_preference(account_id="a", scope="tone", text="少用感叹号", confidence=0.9)
        store.add_preference(account_id="b", scope="tone", text="可用感叹号", confidence=0.9)
        a = store.preferences(account_id="a", scope="tone")
        b = store.preferences(account_id="b", scope="tone")
        self.assertEqual([p["text"] for p in a], ["少用感叹号"])
        self.assertEqual([p["text"] for p in b], ["可用感叹号"])

    def test_search_budget_is_separate_from_model_budget(self):
        runner, brief, _, _ = make_runner(
            self.env,
            self.runtime,
            [
                {"action": "search", "args": {"query": "a"}},
                {"action": "search", "args": {"query": "b"}},
                {"action": "search", "args": {"query": "c"}},
                {"action": "search", "args": {"query": "d"}},
                {"action": "ask_owner", "args": {"question": "need help"}},
            ],
            limits={"max_searches": 1, "max_model_calls": 10},
        )
        result = runner.run(brief)
        self.assertEqual(result.status, "failed")
        self.assertIn("invalid decision", result.error)

    def test_outbox_is_idempotent_for_same_artifact_version(self):
        brief = CreativeBrief(run_key="outbox", account_id="a", origin="manual", topic="t")
        artifact = ArtifactVersion(
            run_key="outbox", version=1, title="t", body="body",
            images=[{"id": "img1", "sha256": "x"}], status="submitted",
            brief_hash=brief.brief_hash,
        )
        from bot.services.creator_agent.entry import queue_artifact_outbox
        from bot.services.creator_agent.runner import AgentResult

        first = queue_artifact_outbox(
            self.env.db, self.env.clock,
            result=AgentResult(run_key="outbox", status="completed", artifact=artifact),
        )
        second = queue_artifact_outbox(
            self.env.db, self.env.clock,
            result=AgentResult(run_key="outbox", status="completed", artifact=artifact),
        )
        self.assertEqual(first, second)
        row = self.env.db.fetchone("SELECT COUNT(*) AS n FROM notifications WHERE dedupe_key=?", ("creator-artifact:outbox:v1",))
        self.assertEqual(int(row["n"]), 1)


class StoreTests(TestCase):
    def setUp(self):
        self.env = new_env("agent-v2-store")

    def tearDown(self):
        cleanup(self.env)

    def test_migration_9_tables_and_run_roundtrip(self):
        row = self.env.db.fetchone("SELECT MAX(version) AS v FROM schema_migrations")
        self.assertEqual(int(row["v"]), 10)
        for table in ("creator_agent_runs", "creator_agent_steps", "creator_agent_artifacts", "creator_agent_preferences", "creator_agent_materials"):
            self.assertIsNotNone(self.env.db.fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)))
        store = AgentStore(self.env.db, self.env.clock)
        brief = CreativeBrief(run_key="r1", account_id="a", origin="manual", topic="t")
        store.ensure_run(brief)
        self.assertEqual(store.get_run("r1")["status"], "running")
        seq = store.append_step("r1", action="search", args={"q": "x"}, status="ok")
        self.assertEqual(seq, 1)

    def test_deferred_run_can_resume_only_with_explicit_flag(self):
        runtime = FakeRuntime()
        runner, brief, context, store = make_runner(
            self.env, runtime,
            [{"action": "ask_owner", "args": {"question": "which angle?"},
              "intent_summary": "ask", "expected_result": "owner reply"}],
            limits={"max_model_calls": 0},
        )
        deferred = runner.run(brief)
        self.assertEqual(deferred.status, "deferred")
        self.assertEqual(runner.llm.calls, 0)
        blocked = runner.run(brief)
        self.assertEqual(blocked.status, "deferred")

        self.env.config.data["creator_agent"]["max_model_calls"] = 2
        self.env.config.data["creator_agent"]["resume_deferred"] = True
        resumed = AgentRunner(
            db=self.env.db, config=self.env.config, clock=self.env.clock,
            llm=runner.llm, registry=runner.registry, context=context,
        ).run(brief)
        self.assertEqual(resumed.status, "needs_owner")

    def test_crash_resume_keeps_prior_steps_and_continues(self):
        store = AgentStore(self.env.db, self.env.clock)
        brief = CreativeBrief(run_key="resume", account_id="a", origin="manual", topic="t")
        store.ensure_run(brief)
        store.append_step("resume", action="search", args={"query": "x"}, status="ok")
        store.update_run("resume", counters={"steps": 1, "model_calls": 1}, checkpoint={"elapsed_s": 0})
        runner, _, _, _ = make_runner(
            self.env, FakeRuntime(),
            [
                {"action": "draft_artifact", "args": {"material_ids": ["M1"]}},
                {"action": "render_images", "args": {}},
                {"action": "submit_for_approval", "args": {}},
            ],
        )
        result = runner.run(brief)
        self.assertEqual(result.status, "completed")
        actions = [s["action"] for s in store.steps("resume")]
        self.assertEqual(actions, ["search", "draft_artifact", "render_images", "submit_for_approval"])


class FeishuChainTests(TestCase):
    def setUp(self):
        self.env = new_env("agent-v2-feishu")
        self.runtime = FakeRuntime()

    def tearDown(self):
        cleanup(self.env)

    def test_inbound_dedup_job_runner_artifact_outbox(self):
        accepted = accept_inbound(
            self.env.db,
            self.env.clock,
            account_id="test-account",
            message_id="om_test_1",
            text="选题 Claude Code 选模型",
        )
        self.assertTrue(accepted["ok"])
        self.assertFalse(accepted["duplicate"])
        duplicate = accept_inbound(
            self.env.db,
            self.env.clock,
            account_id="test-account",
            message_id="om_test_1",
            text="选题 Claude Code 选模型",
        )
        self.assertTrue(duplicate["duplicate"])
        job = self.env.db.fetchone("SELECT * FROM jobs WHERE id=?", (accepted["job_id"],))
        self.assertIsNotNone(job)
        self.assertEqual(job["kind"], "creator_agent_run")

        def factory(brief):
            runner, _, _, _ = make_runner(
                self.env, self.runtime,
                [
                    {"action": "search", "args": {"query": "Claude Code models"}},
                    {"action": "draft_artifact", "args": {"material_ids": ["M1"]}},
                    {"action": "render_images", "args": {}},
                    {"action": "submit_for_approval", "args": {}},
                ],
            )
            return runner

        result = run_job(self.env.db, self.env.config, self.env.clock, job, runner_factory=factory)
        self.assertEqual(result.status, "completed")
        note = self.env.db.fetchone(
            "SELECT * FROM notifications WHERE dedupe_key=?",
            (f"creator-artifact:{result.artifact.run_key}:v{result.artifact.version}",),
        )
        self.assertIsNotNone(note)
        self.assertIn("owner approval is required", note["payload_json"])
        row = self.env.db.fetchone("SELECT * FROM inbound_messages WHERE message_id='om_test_1'")
        self.assertEqual(row["outcome"], "accepted")

    def test_scheduler_creates_bounded_deduped_runs(self):
        discovered = [
            {"topic": "A", "url": "https://example.com/a"},
            {"topic": "B", "url": "https://example.com/b"},
            {"topic": "C", "url": "https://example.com/c"},
        ]
        ids = scheduled_tick(
            self.env.db, self.env.clock, account_id="test-account",
            discover=lambda: discovered, max_topics=2,
        )
        self.assertEqual(len(ids), 2)
        again = scheduled_tick(
            self.env.db, self.env.clock, account_id="test-account",
            discover=lambda: discovered, max_topics=2,
        )
        self.assertEqual(ids, again)
        count = self.env.db.fetchone("SELECT COUNT(*) AS n FROM jobs WHERE kind='creator_agent_run'")
        self.assertEqual(int(count["n"]), 2)


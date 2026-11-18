"""Round-2 recovery regressions.

These tests exercise the two failure modes from the independent round-2
review through the real Feishu command queue and the publication runtime.
They are intentionally small; the larger end-to-end scenario lives in the
delivery demo test.
"""
from __future__ import annotations

import json
from unittest import TestCase

from bot.services.approvals import handle_inbound_message
from bot.services.publication_agent.live import LivePublicationRuntime
from bot.adapters.types import BudgetExceeded
from bot.services.publication_agent.runner import PublicationRunner
from bot.worker import Worker
from tests.helpers import approve, cleanup, make_draft, new_env
from tests.test_publication_agent import FakePublisher, ScriptedLLM, _publication_config
from tests.test_publication_recovery_fences import FeedPublisher


class BudgetFailureLLM:
    def complete_json(self, *, purpose, payload, max_output_tokens=700, before_attempt=None):
        if before_attempt is not None:
            before_attempt()
        raise BudgetExceeded("test daily budget exhausted")


class MissingIdentityPublisher(FeedPublisher):
    def _identity_check(self):
        raise RuntimeError("identity endpoint unavailable")

    def lookup_published_notes_verified(self, *, limit=20):
        return {
            "ok": False,
            "read_ok": True,
            "identity_ok": False,
            "account_identity": None,
            "notes": list(self.notes),
            "reason": "identity unavailable",
        }


class Round2DiagnosisTests(TestCase):
    def test_diagnose_with_zero_budget_keeps_paused_task_paused(self):
        env = new_env("pub-round2-diagnose-paused")
        try:
            made = make_draft(env)
            _publication_config(env)
            approve(env, made["approval"]["code"])
            code = made["approval"]["code"]
            original = env.db.fetchone(
                "SELECT id FROM jobs WHERE dedupe_key LIKE 'publication:approval:%'"
            )
            self.assertIsNotNone(original)
            env.db.execute(
                "UPDATE jobs SET next_run_at='2030-01-01T00:00:00Z' WHERE id=?",
                (int(original["id"]),),
            )
            env.db.execute(
                "UPDATE publication_tasks SET state='paused', wait_reason='owner paused for review'"
            )
            env.config.data["publication_agent"]["max_model_calls"] = 0

            diagnostic = handle_inbound_message(
                env.db, env.config, env.clock,
                message_id="round2-diagnose-1", sender_id="owner-open-id",
                chat_id="owner-open-id", chat_type="p2p",
                text=f"检查原因 {code}", channel="test",
            )
            self.assertEqual(diagnostic["outcome"], "publication_status")
            self.assertIsNotNone(diagnostic["detail"].get("job_id"))

            worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            publisher = FakePublisher()
            worker.publisher = publisher
            worker.llm = ScriptedLLM([])
            worker.tick()

            task = env.db.fetchone("SELECT * FROM publication_tasks")
            self.assertEqual(task["state"], "paused")
            self.assertEqual(task["wait_reason"], "owner paused for review")
            self.assertIsNone(env.db.fetchone("SELECT * FROM publish_attempts"))
            self.assertEqual(publisher.calls, 0)
            notice = env.db.fetchone(
                "SELECT payload_json FROM notifications WHERE dedupe_key LIKE 'publication:%' ORDER BY id DESC LIMIT 1"
            )
            self.assertIsNotNone(notice)
            body = json.loads(notice["payload_json"])["text"]
            self.assertIn("诊断未完成", body)
            self.assertIn("暂停", body)
            self.assertNotIn("已发布", body)
        finally:
            cleanup(env)


class Round2EvidenceTests(TestCase):
    def _seed_unknown(self, env, made, task_id, baseline):
        with env.db.tx() as conn:
            conn.execute(
                "INSERT INTO publish_attempts(approval_id,draft_id,dedupe_key,platform,account_id,"
                "mode,state,publication_task_id,attempt_no,dispatch_state,created_at,started_at,updated_at,"
                "pre_send_baseline_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    made["approval"]["id"], made["draft"]["id"], f"round2:{task_id}",
                    "xiaohongshu", "test-account", "mock", "unknown", task_id, 1,
                    "unknown", "2026-11-05T09:00:00Z", "2026-11-05T09:00:00Z",
                    "2026-11-05T09:00:00Z", json.dumps(baseline),
                ),
            )
            conn.execute(
                "UPDATE publication_tasks SET state='waiting_owner', active_attempt_id=NULL WHERE id=?",
                (task_id,),
            )

    def test_missing_identity_does_not_claim_other_account_same_title(self):
        env = new_env("pub-round2-missing-identity")
        try:
            made = make_draft(env)
            _publication_config(env)
            approve(env, made["approval"]["code"])
            task = env.db.fetchone("SELECT * FROM publication_tasks")
            task_id = int(task["id"])
            self._seed_unknown(
                env, made, task_id,
                {"read_ok": True, "note_ids": [], "account_identity": None},
            )
            publisher = MissingIdentityPublisher([
                {"id": "old-other-account", "title": made["draft"]["title"]},
            ])
            runtime = LivePublicationRuntime(env.db, env.config, env.clock, publisher=publisher)
            report = runtime.check_result(runtime.store.get(task_id=task_id))
            self.assertNotEqual(report.get("state"), "published", report)
            self.assertEqual(env.db.scalar("SELECT state FROM publish_attempts"), "unknown")
            self.assertEqual(
                env.db.scalar("SELECT status FROM approvals WHERE id=?", (int(made["approval"]["id"]),)),
                "approved",
            )
        finally:
            cleanup(env)

    def test_old_attempt_without_identity_baseline_cannot_title_match(self):
        env = new_env("pub-round2-old-baseline")
        try:
            made = make_draft(env)
            _publication_config(env)
            approve(env, made["approval"]["code"])
            task = env.db.fetchone("SELECT * FROM publication_tasks")
            task_id = int(task["id"])
            self._seed_unknown(
                env, made, task_id,
                {"read_ok": True, "note_ids": ["old-note"], "account_identity": "red-id"},
            )
            publisher = FeedPublisher([
                {"id": "old-note", "title": "old title"},
                {"id": "new-note", "title": made["draft"]["title"]},
            ])
            runtime = LivePublicationRuntime(env.db, env.config, env.clock, publisher=publisher)
            report = runtime.check_result(runtime.store.get(task_id=task_id))
            self.assertNotEqual(report.get("state"), "published", report)
            self.assertEqual(env.db.scalar("SELECT state FROM publish_attempts"), "unknown")
        finally:
            cleanup(env)

    def test_repair_and_retry_on_unknown_queues_diagnosis_without_reset(self):
        env = new_env("pub-round2-repair-unknown")
        try:
            made = make_draft(env)
            _publication_config(env)
            approve(env, made["approval"]["code"])
            task = env.db.fetchone("SELECT * FROM publication_tasks")
            task_id = int(task["id"])
            self._seed_unknown(
                env, made, task_id,
                {"read_ok": True, "identity_ok": True, "note_ids": [], "account_identity": "red-id"},
            )
            result = handle_inbound_message(
                env.db, env.config, env.clock,
                message_id="round2-repair-unknown",
                sender_id="owner-open-id", chat_id="owner-open-id", chat_type="p2p",
                text=f"修复并重试 {made['approval']['code']}", channel="test",
            )
            self.assertTrue(result.get("ok"), result)
            self.assertEqual(result.get("outcome"), "unknown_requires_reconciliation")
            self.assertIsNotNone(result.get("detail", {}).get("job_id"))
            task = env.db.fetchone("SELECT * FROM publication_tasks WHERE id=?", (task_id,))
            self.assertEqual(task["state"], "waiting_owner")
            self.assertEqual(env.db.scalar("SELECT state FROM publish_attempts"), "unknown")
            publisher = FakePublisher()
            worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            worker.publisher = publisher
            worker.llm = ScriptedLLM([])
            worker.tick()
            self.assertEqual(publisher.calls, 0)
            self.assertEqual(env.db.scalar("SELECT state FROM publish_attempts"), "unknown")
        finally:
            cleanup(env)

    def test_same_account_new_note_still_publishes(self):
        env = new_env("pub-round2-same-account")
        try:
            made = make_draft(env)
            _publication_config(env)
            approve(env, made["approval"]["code"])
            task = env.db.fetchone("SELECT * FROM publication_tasks")
            task_id = int(task["id"])
            self._seed_unknown(
                env, made, task_id,
                {"read_ok": True, "identity_ok": True, "note_ids": ["old-note"], "account_identity": "red-id"},
            )
            publisher = FeedPublisher([
                {"id": "old-note", "title": "old title"},
                {"id": "new-note", "title": made["draft"]["title"]},
            ])
            runtime = LivePublicationRuntime(env.db, env.config, env.clock, publisher=publisher)
            report = runtime.check_result(runtime.store.get(task_id=task_id))
            self.assertEqual(report.get("state"), "published", report)
            self.assertEqual(env.db.scalar("SELECT external_post_id FROM publish_attempts"), "new-note")
            self.assertEqual(
                env.db.scalar("SELECT status FROM approvals WHERE id=?", (int(made["approval"]["id"]),)),
                "published",
            )
        finally:
            cleanup(env)

class Round2DiagnosisExtraTests(TestCase):
    def _setup(self, prefix):
        env = new_env(prefix)
        made = make_draft(env)
        _publication_config(env)
        approve(env, made["approval"]["code"])
        original = env.db.fetchone(
            "SELECT id FROM jobs WHERE dedupe_key LIKE 'publication:approval:%'"
        )
        self.assertIsNotNone(original)
        env.db.execute(
            "UPDATE jobs SET next_run_at='2030-01-01T00:00:00Z' WHERE id=?",
            (int(original["id"]),),
        )
        env.db.execute(
            "UPDATE publication_tasks SET state='paused', wait_reason='owner paused for review'"
        )
        code = made["approval"]["code"]
        diagnostic = handle_inbound_message(
            env.db, env.config, env.clock,
            message_id=f"{prefix}-diagnose", sender_id="owner-open-id",
            chat_id="owner-open-id", chat_type="p2p",
            text=f"检查原因 {code}", channel="test",
        )
        self.assertEqual(diagnostic["outcome"], "publication_status")
        return env, made

    def _run(self, env, llm, publisher=None):
        worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
        worker.publisher = publisher or FakePublisher()
        worker.llm = llm
        worker.tick()
        task = env.db.fetchone("SELECT * FROM publication_tasks")
        self.assertEqual(task["state"], "paused")
        self.assertEqual(task["wait_reason"], "owner paused for review")
        self.assertIsNone(env.db.fetchone("SELECT * FROM publish_attempts"))
        notice = env.db.fetchone(
            "SELECT payload_json FROM notifications WHERE dedupe_key LIKE 'publication:%' ORDER BY id DESC LIMIT 1"
        )
        self.assertIsNotNone(notice)
        body = json.loads(notice["payload_json"])["text"]
        self.assertIn("诊断未完成", body)
        self.assertIn("暂停", body)
        return body

    def test_diagnosis_budget_exception_does_not_rewrite_paused_task(self):
        env, made = self._setup("pub-round2-budget-exc")
        try:
            self._run(env, BudgetFailureLLM())
        finally:
            cleanup(env)

    def test_diagnosis_invalid_actions_do_not_rewrite_paused_task(self):
        env, made = self._setup("pub-round2-invalid")
        try:
            env.config.data["publication_agent"]["max_invalid_decisions"] = 1
            self._run(env, ScriptedLLM([{"action": "not_a_tool", "args": {}}]))
        finally:
            cleanup(env)

    def test_diagnosis_existing_cancel_request_stays_paused(self):
        env, made = self._setup("pub-round2-cancel")
        try:
            env.db.execute("UPDATE publication_tasks SET cancel_requested=1")
            self._run(env, ScriptedLLM([]))
        finally:
            cleanup(env)

    def test_diagnosis_check_result_is_preview_only(self):
        env, made = self._setup("pub-round2-preview")
        try:
            task = env.db.fetchone("SELECT * FROM publication_tasks")
            task_id = int(task["id"])
            baseline = {
                "read_ok": True, "identity_ok": True,
                "note_ids": [], "account_identity": "red-id",
            }
            with env.db.tx() as conn:
                conn.execute(
                    "INSERT INTO publish_attempts(approval_id,draft_id,dedupe_key,platform,account_id,"
                    "mode,state,publication_task_id,attempt_no,dispatch_state,created_at,started_at,updated_at,"
                    "pre_send_baseline_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        made["approval"]["id"], made["draft"]["id"], "round2-preview",
                        "xiaohongshu", "test-account", "mock", "unknown", task_id, 1,
                        "unknown", "2026-11-05T09:00:00Z", "2026-11-05T09:00:00Z",
                        "2026-11-05T09:00:00Z", json.dumps(baseline),
                    ),
                )
                conn.execute(
                    "UPDATE publication_tasks SET active_attempt_id=NULL WHERE id=?", (task_id,)
                )
            publisher = FeedPublisher([
                {"id": "new-note", "title": made["draft"]["title"]},
            ])
            worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            worker.publisher = publisher
            worker.llm = ScriptedLLM([
                {"action": "check_publish_result", "args": {}},
                {"action": "inspect_task", "args": {}},
            ])
            worker.tick()
            task = env.db.fetchone("SELECT * FROM publication_tasks")
            self.assertEqual(task["state"], "paused")
            self.assertEqual(task["wait_reason"], "owner paused for review")
            self.assertEqual(env.db.scalar("SELECT state FROM publish_attempts"), "unknown")
            self.assertEqual(
                env.db.scalar("SELECT status FROM approvals WHERE id=?", (int(made["approval"]["id"]),)),
                "approved",
            )
        finally:
            cleanup(env)

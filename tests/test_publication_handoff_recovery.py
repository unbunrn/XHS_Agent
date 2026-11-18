"""Regression tests for the two publication-recovery handoff counterexamples."""
from __future__ import annotations

import json
import os
from unittest import TestCase
from unittest.mock import patch

from bot.services.approvals import handle_inbound_message
from bot.services.publication_agent.live import LivePublicationRuntime
from bot.adapters.publishers import XiaohongshuPublisher
from bot.worker import Worker
from tests.helpers import approve, cleanup, make_draft, new_env
from tests.test_publication_agent import FakePublisher, ScriptedLLM, _publication_config
from tests.test_publication_recovery_fences import FeedPublisher
from tests.test_xhs_identity import FakeBridgeHttp, _me_payload


def _live_adapter(env, bridge: FakeBridgeHttp) -> XiaohongshuPublisher:
    env.config.data["app"]["mode"] = "live"
    env.config.data["publisher"]["provider"] = "xiaohongshu"
    env.config.data["publisher"]["live_enabled"] = True
    env.config.data["publisher"]["xiaohongshu"]["expected_red_id"] = "red-123"
    env.config.data["account"]["enabled"] = True
    env.config.data["account"]["status"] = "verified"
    env.config.secrets.xiaohongshu_token = "test-token"
    with patch.dict(os.environ, {"BOT_ALLOW_REAL_PUBLISH": "1"}):
        publisher = XiaohongshuPublisher(env.config, env.clock, allow_unarmed=True)
    publisher.http = bridge
    publisher.armed = True
    publisher.read_only = False
    # The synthetic fixture draft is not a live artifact; this only bypasses
    # the fixture guard, not the adapter's 2xx/feed reconciliation logic.
    publisher.mode = "mock"
    return publisher


class EvidenceRulesTests(TestCase):
    def _task(self, env):
        made = make_draft(env)
        _publication_config(env)
        approve(env, made["approval"]["code"])
        task = env.db.fetchone("SELECT * FROM publication_tasks")
        return made, int(task["id"])

    def _seed_unknown(self, env, made, task_id, baseline):
        with env.db.tx() as conn:
            conn.execute(
                "INSERT INTO publish_attempts(approval_id,draft_id,dedupe_key,platform,account_id,"
                "mode,state,publication_task_id,attempt_no,dispatch_state,created_at,started_at,updated_at,"
                "pre_send_baseline_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    made["approval"]["id"], made["draft"]["id"], f"evidence:{task_id}",
                    "xiaohongshu", "test-account", "mock", "unknown", task_id, 1,
                    "unknown", "2026-11-05T09:00:00Z", "2026-11-05T09:00:00Z",
                    "2026-11-05T09:00:00Z", json.dumps(baseline),
                ),
            )
            conn.execute(
                "UPDATE publication_tasks SET state='waiting_owner', active_attempt_id=NULL WHERE id=?",
                (task_id,),
            )

    def test_unique_new_note_after_baseline_is_published(self):
        env = new_env("pub-evidence-new")
        try:
            made, task_id = self._task(env)
            self._seed_unknown(env, made, task_id, {"read_ok": True, "identity_ok": True, "note_ids": ["old-note"], "account_identity": "red-id"})
            publisher = FeedPublisher([
                {"id": "new-note", "title": made["draft"]["title"], "url": "https://xhs.example/new"}
            ])
            runtime = LivePublicationRuntime(env.db, env.config, env.clock, publisher=publisher)
            report = runtime.check_result(runtime.store.get(task_id=task_id))
            self.assertEqual(report["state"], "published")
            self.assertEqual(publisher.calls, 0)
            self.assertEqual(env.db.scalar("SELECT external_post_id FROM publish_attempts"), "new-note")
        finally:
            cleanup(env)

    def test_two_new_same_title_candidates_stay_unknown(self):
        env = new_env("pub-evidence-ambiguous")
        try:
            made, task_id = self._task(env)
            self._seed_unknown(env, made, task_id, {"read_ok": True, "identity_ok": True, "note_ids": [], "account_identity": "red-id"})
            title = made["draft"]["title"]
            publisher = FeedPublisher([
                {"id": "new-a", "title": title},
                {"id": "new-b", "title": title},
            ])
            runtime = LivePublicationRuntime(env.db, env.config, env.clock, publisher=publisher)
            report = runtime.check_result(runtime.store.get(task_id=task_id))
            self.assertNotEqual(report["state"], "published")
            self.assertEqual(env.db.scalar("SELECT state FROM publish_attempts"), "unknown")
        finally:
            cleanup(env)

    def test_feed_read_failure_does_not_match_by_title(self):
        env = new_env("pub-evidence-readfail")
        try:
            made, task_id = self._task(env)
            self._seed_unknown(env, made, task_id, {"read_ok": False, "note_ids": []})
            publisher = FeedPublisher([{"id": "old-note", "title": made["draft"]["title"]}])
            runtime = LivePublicationRuntime(env.db, env.config, env.clock, publisher=publisher)
            report = runtime.check_result(runtime.store.get(task_id=task_id))
            self.assertNotEqual(report["state"], "published")
            self.assertEqual(env.db.scalar("SELECT state FROM publish_attempts"), "unknown")
        finally:
            cleanup(env)

    def test_account_switch_invalidates_preexisting_baseline(self):
        env = new_env("pub-evidence-account")
        try:
            made, task_id = self._task(env)
            self._seed_unknown(
                env, made, task_id,
                {"read_ok": True, "identity_ok": True, "note_ids": ["old-note"], "account_identity": "red-123"},
            )
            other = (
                200,
                {
                    "success": True,
                    "data": {"data": {
                        "userBasicInfo": {"redId": "red-999"},
                        "feeds": [{
                            "id": "new-note",
                            "noteCard": {"displayTitle": made["draft"]["title"]},
                        }],
                    }},
                },
            )
            publisher = _live_adapter(env, FakeBridgeHttp(me=other))
            runtime = LivePublicationRuntime(env.db, env.config, env.clock, publisher=publisher)
            report = runtime.check_result(runtime.store.get(task_id=task_id))
            self.assertNotEqual(report["state"], "published")
            self.assertEqual(env.db.scalar("SELECT state FROM publish_attempts"), "unknown")
        finally:
            cleanup(env)


class FeishuActualAdapterTests(TestCase):
    def test_real_inbound_approval_through_actual_adapter_posts_once(self):
        env = new_env("pub-handoff-feishu-live")
        try:
            made = make_draft(env)
            _publication_config(env)
            publisher = _live_adapter(env, FakeBridgeHttp())
            approved = handle_inbound_message(
                env.db, env.config, env.clock,
                message_id="handoff-feishu-approve",
                sender_id="owner-open-id", chat_id="owner-open-id", chat_type="p2p",
                text=f"批准 {made['approval']['code']}", channel="test",
            )
            self.assertEqual(approved["outcome"], "approved")

            worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            worker.publisher = publisher
            worker.llm = ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}])
            worker.tick()
            self.assertEqual(len(publisher.http.posts), 1)
            self.assertEqual(
                env.db.scalar("SELECT external_post_id FROM publish_attempts"), "note-1"
            )
            self.assertEqual(env.db.scalar("SELECT state FROM publication_tasks"), "published")
            self.assertEqual(env.db.scalar("SELECT status FROM approvals"), "published")
        finally:
            cleanup(env)


class DiagnosisIsolationTests(TestCase):
    def test_diagnose_schedule_resume_cannot_publish_a_normal_job(self):
        env = new_env("pub-handoff-diagnose")
        try:
            made = make_draft(env)
            _publication_config(env)
            approve(env, made["approval"]["code"])
            code = made["approval"]["code"]
            original = env.db.fetchone(
                "SELECT id FROM jobs WHERE dedupe_key LIKE 'publication:approval:%'"
            )
            self.assertIsNotNone(original)
            # Isolate the diagnostic-derived job from the already-authorized
            # normal publication job.
            env.db.execute(
                "UPDATE jobs SET next_run_at='2030-01-01T00:00:00Z' WHERE id=?",
                (int(original["id"]),),
            )
            diagnostic = handle_inbound_message(
                env.db, env.config, env.clock,
                message_id="handoff-diagnose-1", sender_id="owner-open-id",
                chat_id="owner-open-id", chat_type="p2p",
                text=f"检查原因 {code}", channel="test",
            )
            self.assertEqual(diagnostic["outcome"], "publication_status")
            self.assertIsNotNone(diagnostic["detail"].get("job_id"))

            worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            publisher = FakePublisher()
            worker.publisher = publisher
            worker.llm = ScriptedLLM([
                {"action": "schedule_resume", "args": {"delay_seconds": 10}},
                {"action": "publish_approved_artifact", "args": {}},
            ])
            first = worker.tick()
            self.assertTrue(first["jobs"], first)
            self.assertEqual(publisher.calls, 0, "diagnosis must not submit")

            env.clock.advance(seconds=11)
            for _ in range(4):
                worker.tick()
                env.clock.advance(seconds=60)
            self.assertEqual(publisher.calls, 0, "diagnosis-derived job must stay read-only")
            task = env.db.fetchone(
                "SELECT state FROM publication_tasks WHERE approval_id=?",
                (int(made["approval"]["id"]),),
            )
            self.assertNotEqual(task["state"], "published")
            attempt = env.db.fetchone("SELECT state FROM publish_attempts")
            self.assertIsNone(attempt)
        finally:
            cleanup(env)


class RestartReconciliationTests(TestCase):
    def test_preexisting_same_title_note_is_not_success_after_restart(self):
        env = new_env("pub-handoff-restart")
        try:
            made = make_draft(env)
            _publication_config(env)
            title = str(made["draft"]["title"])
            before = _me_payload(("old-note", title, "old-xsec"))
            bridge = FakeBridgeHttp(
                me=before,
                me_after=before,
                publish=(200, {"success": True, "data": {}}),
            )
            publisher = _live_adapter(env, bridge)
            with patch.dict(os.environ, {"BOT_ALLOW_REAL_PUBLISH": "1"}):
                approve(env, made["approval"]["code"])
                task = env.db.fetchone("SELECT * FROM publication_tasks")
                task_id = int(task["id"])
                first = LivePublicationRuntime(
                    env.db, env.config, env.clock, publisher=publisher,
                )
                frozen = first.store.get(task_id=task_id)
                result = first.publish(frozen, first.store.package(frozen))
            self.assertEqual(result.get("state"), "unknown", result)
            self.assertEqual(
                env.db.scalar("SELECT state FROM publish_attempts"), "unknown"
            )
            self.assertEqual(
                env.db.scalar("SELECT state FROM publication_tasks WHERE id=?", (task_id,)),
                "waiting_owner",
            )

            # A fresh runtime simulates a process restart.  The only note on
            # the account is the old same-title note that existed before POST.
            restarted = LivePublicationRuntime(
                env.db, env.config, env.clock, publisher=publisher,
            )
            fresh = restarted.store.get(task_id=task_id)
            report = restarted.check_result(fresh)
            self.assertNotEqual(report.get("state"), "published", report)
            self.assertEqual(
                env.db.scalar("SELECT state FROM publish_attempts"), "unknown"
            )
            self.assertEqual(
                env.db.scalar("SELECT state FROM publication_tasks WHERE id=?", (task_id,)),
                "waiting_owner",
            )
            self.assertNotEqual(
                env.db.scalar("SELECT status FROM approvals WHERE id=?", (int(made["approval"]["id"]),)),
                "published",
            )
        finally:
            cleanup(env)

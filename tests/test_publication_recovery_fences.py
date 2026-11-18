"""Counterexample tests for the publication recovery execution fence."""
from __future__ import annotations

import json
import os
from unittest import TestCase
from unittest.mock import patch

from bot.adapters.types import PublishOutcome
from bot.services.approvals import handle_inbound_message
from bot.services.publication_agent.live import LivePublicationRuntime
from bot.services.publication_agent.runner import PublicationRunner
from bot.services.publish import publish_approval, validate_publish
from bot.adapters.publishers import XiaohongshuPublisher
from tests.test_xhs_identity import FakeBridgeHttp, _me_payload
from tests.helpers import approve, cleanup, make_draft, new_env
from tests.test_publication_agent import FakePublisher, ScriptedLLM, _publication_config


class FeedPublisher(FakePublisher):
    def __init__(self, notes=None):
        super().__init__()
        self.notes = list(notes or [])

    def lookup_published_notes(self, *, since=None, limit=10):
        return list(self.notes)

    def lookup_published_notes_verified(self, *, limit=20):
        return {
            "ok": True,
            "read_ok": True,
            "identity_ok": True,
            "account_identity": "red-id",
            "notes": list(self.notes),
            "reason": "test verified feed",
        }


class PublicationFenceTests(TestCase):
    def _task(self, env):
        made = make_draft(env)
        _publication_config(env)
        approve(env, made["approval"]["code"])
        task = env.db.fetchone("SELECT * FROM publication_tasks")
        return made, int(task["id"])

    def test_paused_task_cannot_be_activated_by_an_old_job(self):
        env = new_env("pub-fence-pause")
        try:
            made, task_id = self._task(env)
            env.db.execute(
                "UPDATE publication_tasks SET state='paused', wait_reason='owner paused' WHERE id=?",
                (task_id,),
            )
            publisher = FakePublisher()
            runner = PublicationRunner(
                env.db, env.config, env.clock, ScriptedLLM([]),
                LivePublicationRuntime(env.db, env.config, env.clock, publisher=publisher),
            )
            result = runner.run(task_id)
            self.assertEqual(result.state, "paused")
            self.assertEqual(publisher.calls, 0)
            self.assertEqual(
                env.db.scalar("SELECT state FROM publication_tasks WHERE id=?", (task_id,)),
                "paused",
            )
        finally:
            cleanup(env)

    def test_cancel_after_precheck_blocks_the_claim(self):
        env = new_env("pub-fence-cancel")
        try:
            made, task_id = self._task(env)
            publisher = FakePublisher()
            approval = env.db.fetchone("SELECT * FROM approvals")
            ok, reason, draft, images = validate_publish(
                env.db, env.config, env.clock, approval, publisher, task_id=task_id,
            )
            self.assertTrue(ok, reason)
            self.assertIsNotNone(draft)
            env.db.execute(
                "UPDATE publication_tasks SET cancel_requested=1 WHERE id=?", (task_id,)
            )
            result = publish_approval(
                env.db, env.config, env.clock, int(approval["id"]),
                publisher=publisher, task_id=task_id, attempt_no=1, allow_reopen=True,
            )
            self.assertFalse(result.get("ok"), result)
            self.assertEqual(publisher.calls, 0)
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 0)
        finally:
            cleanup(env)

    def test_stale_generation_cannot_claim(self):
        env = new_env("pub-fence-generation")
        try:
            made, task_id = self._task(env)
            publisher = FakePublisher()
            approval = env.db.fetchone("SELECT * FROM approvals")
            result = publish_approval(
                env.db, env.config, env.clock, int(approval["id"]),
                publisher=publisher, task_id=task_id, attempt_no=1, allow_reopen=True,
                task_generation=99,
            )
            self.assertFalse(result.get("ok"), result)
            self.assertEqual(publisher.calls, 0)
        finally:
            cleanup(env)

    def test_unknown_cancel_does_not_finalize(self):
        env = new_env("pub-fence-unknown-cancel")
        try:
            made, task_id = self._task(env)
            # Simulate a claim that recorded an unresolved remote attempt.
            with env.db.tx() as conn:
                conn.execute(
                    "INSERT INTO publish_attempts(approval_id,draft_id,dedupe_key,platform,account_id,"
                    "mode,state,publication_task_id,attempt_no,dispatch_state,created_at,started_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        made["approval"]["id"], made["draft"]["id"], f"unknown:{task_id}",
                        "xiaohongshu", "test-account", "mock", "unknown", task_id, 1,
                        "unknown", "2026-11-05T09:00:00Z", "2026-11-05T09:00:00Z",
                        "2026-11-05T09:00:00Z",
                    ),
                )
                conn.execute(
                    "UPDATE publish_attempts SET pre_send_baseline_json=? "
                    "WHERE publication_task_id=?",
                    (json.dumps({"read_ok": True, "identity_ok": True, "note_ids": [], "account_identity": "red-id"}), task_id),
                )
                conn.execute(
                    "UPDATE publication_tasks SET state='waiting_owner', active_attempt_id=NULL WHERE id=?",
                    (task_id,),
                )
            result = handle_inbound_message(
                env.db, env.config, env.clock,
                message_id="cancel-unknown-1", sender_id="owner-open-id",
                chat_id="owner-open-id", chat_type="p2p",
                text=f"取消发布 {made['approval']['code']}", channel="test",
            )
            self.assertEqual(result["outcome"], "cancel_requested_in_flight")
            task = env.db.fetchone(
                "SELECT state, cancel_requested FROM publication_tasks WHERE id=?", (task_id,)
            )
            self.assertNotEqual(task["state"], "cancelled")
            self.assertEqual(task["cancel_requested"], 1)
            self.assertEqual(env.db.scalar("SELECT state FROM publish_attempts"), "unknown")
        finally:
            cleanup(env)

    def test_cancel_then_late_success_wins_without_a_second_post(self):
        env = new_env("pub-fence-cancel-late")
        try:
            made, task_id = self._task(env)
            with env.db.tx() as conn:
                conn.execute(
                    "INSERT INTO publish_attempts(approval_id,draft_id,dedupe_key,platform,account_id,"
                    "mode,state,publication_task_id,attempt_no,dispatch_state,created_at,started_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        made["approval"]["id"], made["draft"]["id"], f"cancel-late:{task_id}",
                        "xiaohongshu", "test-account", "mock", "unknown", task_id, 1,
                        "unknown", "2026-11-05T09:00:00Z", "2026-11-05T09:00:00Z",
                        "2026-11-05T09:00:00Z",
                    ),
                )
                conn.execute(
                    "UPDATE publish_attempts SET pre_send_baseline_json=? "
                    "WHERE publication_task_id=?",
                    (json.dumps({"read_ok": True, "identity_ok": True, "note_ids": [], "account_identity": "red-id"}), task_id),
                )
                conn.execute(
                    "UPDATE publication_tasks SET state='waiting_owner', active_attempt_id=NULL WHERE id=?",
                    (task_id,),
                )
            result = handle_inbound_message(
                env.db, env.config, env.clock,
                message_id="cancel-late-1", sender_id="owner-open-id",
                chat_id="owner-open-id", chat_type="p2p",
                text=f"取消发布 {made['approval']['code']}", channel="test",
            )
            self.assertEqual(result["outcome"], "cancel_requested_in_flight")
            publisher = FeedPublisher([{"id": "note-late-1", "title": made["draft"]["title"]}])
            runner = PublicationRunner(
                env.db, env.config, env.clock, ScriptedLLM([]),
                LivePublicationRuntime(env.db, env.config, env.clock, publisher=publisher),
            )
            outcome = runner.run(task_id)
            self.assertEqual(outcome.state, "published")
            self.assertEqual(publisher.calls, 0)
            self.assertEqual(
                env.db.scalar("SELECT state FROM publication_tasks WHERE id=?", (task_id,)),
                "published",
            )
        finally:
            cleanup(env)

    def test_http_500_then_late_note_through_gate_and_agent_posts_once(self):
        env = new_env("pub-fence-500-late")
        try:
            made = make_draft(env)
            env.config.data["app"]["mode"] = "live"
            env.config.data["publisher"]["provider"] = "xiaohongshu"
            env.config.data["publisher"]["live_enabled"] = True
            env.config.data["publisher"]["xiaohongshu"]["expected_red_id"] = "red-123"
            env.config.data["account"]["enabled"] = True
            env.config.data["account"]["status"] = "verified"
            env.config.secrets.xiaohongshu_token = "test-token"
            _publication_config(env)
            with patch.dict(os.environ, {"BOT_ALLOW_REAL_PUBLISH": "1"}):
                approve(env, made["approval"]["code"])
                task = env.db.fetchone("SELECT * FROM publication_tasks")
                task_id = int(task["id"])
                before = _me_payload(("old-note", "旧笔记", "old-xsec"))
                bridge = FakeBridgeHttp(
                    me=before,
                    me_after=before,
                    publish=(500, {"success": False, "message": "发布未确认成功"}),
                )
                publisher = XiaohongshuPublisher(env.config, env.clock, allow_unarmed=True)
                publisher.http = bridge
                publisher.armed = True
                publisher.read_only = False
                # The synthetic fixture draft is not a live artifact; keep the
                # live fixture guard out of this adapter/gate/agent chain test.
                publisher.mode = "mock"
                runtime = LivePublicationRuntime(env.db, env.config, env.clock, publisher=publisher)
                with patch("bot.adapters.publishers.time.sleep"):
                    first = PublicationRunner(
                        env.db, env.config, env.clock,
                        ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}]),
                        runtime,
                    ).run(task_id)
                self.assertEqual(first.state, "waiting_owner")
                self.assertEqual(len(bridge.posts), 1)
                self.assertEqual(
                    env.db.scalar("SELECT state FROM publish_attempts"), "unknown"
                )
                bridge.me = _me_payload(
                    ("late-note", made["draft"]["title"], "late-xsec"),
                    ("old-note", "旧笔记", "old-xsec"),
                )
                second = PublicationRunner(
                    env.db, env.config, env.clock,
                    ScriptedLLM([{"action": "check_publish_result", "args": {}}]),
                    runtime,
                ).run(task_id)
            self.assertEqual(second.state, "published")
            self.assertEqual(len(bridge.posts), 1)
            self.assertEqual(
                env.db.scalar("SELECT external_post_id FROM publish_attempts"),
                "late-note",
            )
        finally:
            cleanup(env)

    def test_late_platform_evidence_promotes_unknown_without_resend(self):
        env = new_env("pub-fence-late")
        try:
            made, task_id = self._task(env)
            with env.db.tx() as conn:
                conn.execute(
                    "INSERT INTO publish_attempts(approval_id,draft_id,dedupe_key,platform,account_id,"
                    "mode,state,publication_task_id,attempt_no,dispatch_state,created_at,started_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        made["approval"]["id"], made["draft"]["id"], f"late:{task_id}",
                        "xiaohongshu", "test-account", "mock", "unknown", task_id, 1,
                        "unknown", "2026-11-05T09:00:00Z", "2026-11-05T09:00:00Z",
                        "2026-11-05T09:00:00Z",
                    ),
                )
                conn.execute(
                    "UPDATE publish_attempts SET pre_send_baseline_json=? "
                    "WHERE publication_task_id=?",
                    (json.dumps({"read_ok": True, "identity_ok": True, "note_ids": [], "account_identity": "red-id"}), task_id),
                )
                conn.execute(
                    "UPDATE publication_tasks SET state='waiting_owner', active_attempt_id=NULL WHERE id=?",
                    (task_id,),
                )
            publisher = FeedPublisher([
                {"id": "late-note", "title": made["draft"]["title"],
                 "url": "https://example.test/late-note"}
            ])
            runtime = LivePublicationRuntime(env.db, env.config, env.clock, publisher=publisher)
            task = runtime.store.get(task_id=task_id)
            report = runtime.check_result(task)
            self.assertEqual(report["state"], "published")
            self.assertEqual(publisher.calls, 0)
            self.assertEqual(env.db.scalar("SELECT state FROM publication_tasks"), "published")
            self.assertEqual(env.db.scalar("SELECT state FROM publish_attempts"), "published")
            self.assertEqual(env.db.scalar("SELECT external_post_id FROM publish_attempts"), "late-note")
        finally:
            cleanup(env)

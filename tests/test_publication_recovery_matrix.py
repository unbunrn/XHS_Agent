"""Failure-injection matrix for the publication recovery agent.

These tests use real application entry points where possible: approvals,
durable jobs, the worker handler and the publish gate.  They never call a real
bridge or platform.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase

from bot.adapters.types import PublishOutcome
from bot.services.approvals import handle_inbound_message
from bot.services.publication_agent.diagnostics import restore_same_bytes
from bot.services.publication_agent.entry import create_task_for_approval
from bot.services.publication_agent.live import LivePublicationRuntime
from bot.services.publication_agent.runner import PublicationRunner
from bot.services.publication_agent.store import PublicationStore
from tests.helpers import approve, cleanup, make_draft, new_env
from tests.test_publication_agent import FakePublisher, ScriptedLLM, _publication_config


class LoginFailPublisher(FakePublisher):
    def _login_check(self):
        return (False, "creator session expired", False)


class PublicationRecoveryMatrixTests(TestCase):
    def _task(self, env):
        made = make_draft(env)
        _publication_config(env)
        approve(env, made["approval"]["code"])
        task = env.db.fetchone("SELECT * FROM publication_tasks")
        return made, int(task["id"])

    def test_login_owner_request_enters_waiting_login(self):
        env = new_env("pub-matrix-login")
        try:
            made, task_id = self._task(env)
            runtime = LivePublicationRuntime(env.db, env.config, env.clock, publisher=LoginFailPublisher())
            llm = ScriptedLLM([
                {"action": "check_login", "args": {}},
                {"action": "request_owner_action", "args": {"question": "请重新登录", "kind": "login"}},
            ])
            runner = PublicationRunner(env.db, env.config, env.clock, llm, runtime)
            result = runner.run(task_id)
            self.assertEqual(result.state, "waiting_login")
            self.assertEqual(
                env.db.scalar("SELECT state FROM publication_tasks WHERE id=?", (task_id,)),
                "waiting_login",
            )
            self.assertGreaterEqual(
                env.db.scalar("SELECT COUNT(*) FROM notifications"),
                1,
            )
        finally:
            cleanup(env)

    def test_restore_rejects_destination_outside_approved_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside.png"
            package = {"images": [{"ordinal": 1, "path": str(outside), "sha256": "0" * 64}]}
            with self.assertRaises(ValueError):
                restore_same_bytes(package, [], roots=[str(Path(tmp) / "drafts")])

    def test_unknown_owner_confirmation_creates_second_attempt(self):
        env = new_env("pub-matrix-confirm")
        try:
            made, task_id = self._task(env)
            code = made["approval"]["code"]
            runtime = LivePublicationRuntime(
                env.db, env.config, env.clock,
                publisher=FakePublisher(PublishOutcome(state="unknown", detail="unknown")),
            )
            runner = PublicationRunner(
                env.db, env.config, env.clock,
                ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}]),
                runtime,
            )
            self.assertEqual(runner.run(task_id).state, "waiting_owner")
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 1)

            reply = handle_inbound_message(
                env.db, env.config, env.clock,
                message_id="confirm-not-published-1", sender_id="owner-open-id",
                chat_id="owner-open-id", chat_type="p2p",
                text=f"确认未发布并重试 {code}", channel="test",
            )
            self.assertEqual(reply["outcome"], "retry_queued")
            self.assertEqual(
                env.db.scalar("SELECT state FROM publication_tasks WHERE id=?", (task_id,)),
                "ready",
            )

            runtime2 = LivePublicationRuntime(env.db, env.config, env.clock, publisher=FakePublisher())
            runner2 = PublicationRunner(
                env.db, env.config, env.clock,
                ScriptedLLM([{"action": "publish_approved_artifact", "args": {"retry_basis": "owner confirmed"}}]),
                runtime2,
            )
            result = runner2.run(task_id)
            self.assertEqual(result.state, "published")
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 2)
        finally:
            cleanup(env)

    def test_cancelled_task_does_not_call_publisher(self):
        env = new_env("pub-matrix-cancel")
        try:
            made, task_id = self._task(env)
            code = made["approval"]["code"]
            cancelled = handle_inbound_message(
                env.db, env.config, env.clock,
                message_id="cancel-before-send-1", sender_id="owner-open-id",
                chat_id="owner-open-id", chat_type="p2p",
                text=f"取消发布 {code}", channel="test",
            )
            self.assertEqual(cancelled["outcome"], "cancelled")
            publisher = FakePublisher()
            runtime = LivePublicationRuntime(env.db, env.config, env.clock, publisher=publisher)
            runner = PublicationRunner(
                env.db, env.config, env.clock,
                ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}]),
                runtime,
            )
            result = runner.run(task_id)
            self.assertEqual(result.state, "cancelled")
            self.assertEqual(publisher.calls, 0)
        finally:
            cleanup(env)

    def test_expired_approval_blocks_before_publisher(self):
        env = new_env("pub-matrix-expired")
        try:
            made, task_id = self._task(env)
            env.db.execute(
                "UPDATE approvals SET expires_at='2000-01-01T00:00:00Z' WHERE id=?",
                (made["approval"]["id"],),
            )
            publisher = FakePublisher()
            runtime = LivePublicationRuntime(env.db, env.config, env.clock, publisher=publisher)
            runner = PublicationRunner(
                env.db, env.config, env.clock,
                ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}]),
                runtime,
            )
            result = runner.run(task_id)
            self.assertEqual(result.state, "waiting_owner")
            self.assertEqual(publisher.calls, 0)
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 0)
        finally:
            cleanup(env)

    def test_duplicate_creation_returns_one_task_and_one_job(self):
        env = new_env("pub-matrix-duplicate")
        try:
            made, _ = self._task(env)
            task2 = create_task_for_approval(
                env.db, env.config, env.clock, made["approval"]["id"]
            )
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM publication_tasks"), 1)
            self.assertEqual(
                env.db.scalar("SELECT COUNT(*) FROM jobs WHERE kind='publication_agent'"), 1
            )
            self.assertEqual(task2["task"]["approval_id"], made["approval"]["id"])
        finally:
            cleanup(env)

    def test_notifications_are_deduplicated(self):
        env = new_env("pub-matrix-notify")
        try:
            _, task_id = self._task(env)
            runtime = LivePublicationRuntime(env.db, env.config, env.clock, publisher=FakePublisher())
            task = runtime.store.get(task_id=task_id)
            runtime.notify(task, "same status")
            runtime.notify(task, "same status")
            self.assertEqual(
                env.db.scalar(
                    "SELECT COUNT(*) FROM notifications "
                    "WHERE kind='outcome' AND payload_json LIKE ?",
                    ("%same status%",),
                ),
                1,
            )
        finally:
            cleanup(env)

    def test_model_budget_stops_before_extra_decision(self):
        env = new_env("pub-matrix-budget")
        try:
            _, task_id = self._task(env)
            env.config.data["publication_agent"]["max_model_calls"] = 1
            publisher = FakePublisher()
            runtime = LivePublicationRuntime(env.db, env.config, env.clock, publisher=publisher)
            llm = ScriptedLLM([
                {"action": "inspect_task", "args": {}},
                {"action": "inspect_task", "args": {}},
            ])
            result = PublicationRunner(env.db, env.config, env.clock, llm, runtime).run(task_id)
            self.assertEqual(result.state, "waiting_owner")
            self.assertEqual(len(llm.decisions), 1)
            self.assertEqual(publisher.calls, 0)
        finally:
            cleanup(env)

    def test_invalid_actions_never_publish(self):
        env = new_env("pub-matrix-invalid")
        try:
            _, task_id = self._task(env)
            publisher = FakePublisher()
            runtime = LivePublicationRuntime(env.db, env.config, env.clock, publisher=publisher)
            llm = ScriptedLLM([
                {"action": "delete_everything", "args": {}},
                {"action": "delete_everything", "args": {}},
            ])
            result = PublicationRunner(env.db, env.config, env.clock, llm, runtime).run(task_id)
            self.assertEqual(result.state, "waiting_owner")
            self.assertEqual(publisher.calls, 0)
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 0)
        finally:
            cleanup(env)

    def test_crash_after_claim_does_not_resend(self):
        env = new_env("pub-matrix-crash")
        try:
            made, task_id = self._task(env)
            publisher = FakePublisher()
            runtime = LivePublicationRuntime(env.db, env.config, env.clock, publisher=publisher)
            with env.db.tx() as conn:
                conn.execute(
                    "INSERT INTO publish_attempts(approval_id,draft_id,dedupe_key,platform,account_id,"
                    "mode,state,publication_task_id,attempt_no,dispatch_state,created_at,started_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        made["approval"]["id"], made["draft"]["id"], f"crash:{task_id}", "xiaohongshu",
                        "test-account", "mock", "publishing", task_id, 1, "not_dispatched",
                        "2026-11-05T09:00:00Z", "2026-11-05T09:00:00Z", "2026-11-05T09:00:00Z",
                    ),
                )
                attempt_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                conn.execute(
                    "UPDATE publication_tasks SET state='submitting', active_attempt_id=? WHERE id=?",
                    (attempt_id, task_id),
                )
            task = runtime.store.get(task_id=task_id)
            report = runtime.check_result(task)
            self.assertEqual(report["state"], "publishing")
            self.assertEqual(publisher.calls, 0)
            fresh = runtime.store.get(task_id=task_id)
            self.assertEqual(fresh.state, "waiting_owner")
            self.assertIn("check the platform", fresh.wait_reason or "")
        finally:
            cleanup(env)

    def test_login_recovery_publishes_original_package(self):
        env = new_env("pub-matrix-login-recover")
        try:
            made, task_id = self._task(env)
            code = made["approval"]["code"]
            runtime = LivePublicationRuntime(env.db, env.config, env.clock, publisher=LoginFailPublisher())
            runner = PublicationRunner(
                env.db, env.config, env.clock,
                ScriptedLLM([
                    {"action": "check_login", "args": {}},
                    {"action": "request_owner_action", "args": {"question": "请重新登录", "kind": "login"}},
                ]),
                runtime,
            )
            runner.run(task_id)
            env.db.execute(
                "UPDATE publication_tasks SET state='waiting_login', wait_reason='login expired' WHERE id=?",
                (task_id,),
            )
            reply = handle_inbound_message(
                env.db, env.config, env.clock,
                message_id="login-ok-1", sender_id="owner-open-id",
                chat_id="owner-open-id", chat_type="p2p",
                text=f"已登录 {code}", channel="test",
            )
            self.assertEqual(reply["outcome"], "login_check_queued")
            publisher = FakePublisher()
            runtime2 = LivePublicationRuntime(env.db, env.config, env.clock, publisher=publisher)
            runner2 = PublicationRunner(
                env.db, env.config, env.clock,
                ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}]),
                runtime2,
            )
            result = runner2.run(task_id)
            self.assertEqual(result.state, "published")
            self.assertEqual(publisher.calls, 1)
        finally:
            cleanup(env)

    def test_late_published_result_wins_without_resend(self):
        env = new_env("pub-matrix-late")
        try:
            made, task_id = self._task(env)
            publisher = FakePublisher(PublishOutcome(state="unknown", detail="timeout"))
            runtime = LivePublicationRuntime(env.db, env.config, env.clock, publisher=publisher)
            runner = PublicationRunner(
                env.db, env.config, env.clock,
                ScriptedLLM([
                    {"action": "publish_approved_artifact", "args": {}},
                    {"action": "request_owner_action", "args": {"question": "发布结果未知", "kind": "unknown"}},
                ]),
                runtime,
            )
            runner.run(task_id)
            attempt = env.db.fetchone("SELECT id FROM publish_attempts ORDER BY id DESC LIMIT 1")
            env.db.execute(
                "UPDATE publish_attempts SET state='published', external_post_id='note-late', "
                "external_url='https://example.test/note-late', finished_at='2026-11-05T09:01:00Z' "
                "WHERE id=?",
                (attempt["id"],),
            )
            runtime2 = LivePublicationRuntime(env.db, env.config, env.clock, publisher=publisher)
            runner2 = PublicationRunner(
                env.db, env.config, env.clock,
                ScriptedLLM([{"action": "check_publish_result", "args": {}}]),
                runtime2,
            )
            result = runner2.run(task_id)
            self.assertEqual(result.state, "published")
            self.assertEqual(publisher.calls, 1)
        finally:
            cleanup(env)

    def test_inflight_cancel_never_calls_publisher(self):
        env = new_env("pub-matrix-inflight-cancel")
        try:
            made, task_id = self._task(env)
            publisher = FakePublisher()
            runtime = LivePublicationRuntime(env.db, env.config, env.clock, publisher=publisher)
            with env.db.tx() as conn:
                conn.execute(
                    "UPDATE publication_tasks SET state='submitting', cancel_requested=1 WHERE id=?",
                    (task_id,),
                )
            runner = PublicationRunner(
                env.db, env.config, env.clock,
                ScriptedLLM([
                    {"action": "publish_approved_artifact", "args": {}},
                    {"action": "request_owner_action", "args": {"question": "已请求取消", "kind": "cancel"}},
                ]),
                runtime,
            )
            runner.run(task_id)
            self.assertEqual(publisher.calls, 0)
        finally:
            cleanup(env)

    def test_second_runner_cannot_acquire_live_lease(self):
        env = new_env("pub-matrix-lease")
        try:
            _, task_id = self._task(env)
            store = PublicationStore(env.db, env.clock)
            self.assertTrue(store.acquire_lease(task_id, "other-worker", seconds=120))
            publisher = FakePublisher()
            runtime = LivePublicationRuntime(env.db, env.config, env.clock, publisher=publisher)
            runner = PublicationRunner(
                env.db, env.config, env.clock, ScriptedLLM([
                    {"action": "publish_approved_artifact", "args": {}},
                ]), runtime,
            )
            result = runner.run(task_id)
            self.assertEqual(result.state, "busy")
            self.assertEqual(publisher.calls, 0)
        finally:
            cleanup(env)

    def test_approval_for_other_account_never_reaches_publisher(self):
        env = new_env("pub-matrix-wrong-account")
        try:
            made, task_id = self._task(env)
            env.db.execute(
                "UPDATE approvals SET account_id='other-account' WHERE id=?",
                (made["approval"]["id"],),
            )
            publisher = FakePublisher()
            runtime = LivePublicationRuntime(env.db, env.config, env.clock, publisher=publisher)
            runner = PublicationRunner(
                env.db, env.config, env.clock,
                ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}]),
                runtime,
            )
            result = runner.run(task_id)
            self.assertEqual(result.state, "waiting_owner")
            self.assertEqual(publisher.calls, 0)
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 0)
        finally:
            cleanup(env)

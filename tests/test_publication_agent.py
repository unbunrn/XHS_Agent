"""Offline tests for the publication recovery agent foundation."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import TestCase

from bot.adapters.types import PublishOutcome
from bot.services.approvals import handle_inbound_message
from bot.services.publication_agent.live import LivePublicationRuntime
from bot.services.publication_agent.runner import PublicationRunner
from bot.services.publication_agent.store import PublicationStore
from tests.helpers import approve, cleanup, make_draft, new_env


class ScriptedLLM:
    def __init__(self, decisions):
        self.decisions = list(decisions)

    def complete_json(self, *, purpose, payload, max_output_tokens=700, before_attempt=None):
        if before_attempt is not None:
            before_attempt()
        assert purpose == "publication_decide"
        assert self.decisions
        return SimpleNamespace(data=self.decisions.pop(0), provider="fake", model="scripted")


class FakePublisher:
    name = "fake-publisher"
    mode = "mock"

    def __init__(self, outcome=None):
        self.outcome = outcome or PublishOutcome(
            state="published",
            detail="published by fake",
            external_post_id="note-fake-1",
            external_url="https://www.xiaohongshu.com/explore/note-fake-1",
            receipt={"verified_from_feed": True},
        )
        self.calls = 0

    def health(self):
        return {"ok": True, "version": "fake"}

    def _login_check(self):
        return (True, "logged in", True)

    def _identity_check(self):
        return (True, "identity ok", "red-id")

    def publish(self, request):
        self.calls += 1
        return self.outcome


def _publication_config(env):
    env.config.data["publication_agent"] = {
        "enabled": True,
        "max_steps": 6,
        "max_model_calls": 10,
        "max_invalid_decisions": 2,
        "max_retries": 2,
        "retry_base_seconds": 10,
        "retry_max_seconds": 60,
        "login_wait_seconds": 300,
        "owner_wait_seconds": 3600,
        "task_ttl_seconds": 86400,
    }


class LoggedOutPublisher(FakePublisher):
    def _login_check(self):
        return (False, "creator center login expired", False)

    def _identity_check(self):
        return (True, "identity ok", "red-id")


class PublicationAgentTests(TestCase):
    def test_approval_creates_durable_task_and_job(self):
        env = new_env("publication-create")
        try:
            made = make_draft(env)
            _publication_config(env)
            result = approve(env, made["approval"]["code"])
            self.assertEqual(result["outcome"], "approved")
            task = env.db.fetchone("SELECT * FROM publication_tasks")
            self.assertIsNotNone(task)
            self.assertEqual(task["approval_id"], made["approval"]["id"])
            self.assertEqual(task["state"], "ready")
            task_job = env.db.fetchone(
                "SELECT * FROM jobs WHERE kind='publication_agent' ORDER BY id DESC LIMIT 1"
            )
            self.assertIsNotNone(task_job)
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM jobs WHERE kind='publish'"), 0)
        finally:
            cleanup(env)

    def test_runner_publishes_once_and_marks_task_published(self):
        env = new_env("publication-run")
        try:
            made = make_draft(env)
            _publication_config(env)
            approve(env, made["approval"]["code"])
            task_row = env.db.fetchone("SELECT * FROM publication_tasks")
            task_id = int(task_row["id"])
            runtime = LivePublicationRuntime(
                env.db, env.config, env.clock, publisher=FakePublisher()
            )
            runner = PublicationRunner(
                env.db,
                env.config,
                env.clock,
                ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}]),
                runtime,
            )
            result = runner.run(task_id)
            self.assertEqual(result.state, "published")
            task = env.db.fetchone("SELECT * FROM publication_tasks WHERE id=?", (task_id,))
            self.assertEqual(task["state"], "published")
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 1)
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM publish_attempts WHERE state='published'"), 1)
        finally:
            cleanup(env)

    def test_unknown_result_waits_for_owner_and_is_not_retried(self):
        env = new_env("publication-unknown")
        try:
            made = make_draft(env)
            _publication_config(env)
            approve(env, made["approval"]["code"])
            task_id = int(env.db.fetchone("SELECT id FROM publication_tasks")["id"])
            runtime = LivePublicationRuntime(
                env.db,
                env.config,
                env.clock,
                publisher=FakePublisher(PublishOutcome(state="unknown", detail="unknown")),
            )
            runner = PublicationRunner(
                env.db,
                env.config,
                env.clock,
                ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}]),
                runtime,
            )
            result = runner.run(task_id)
            self.assertEqual(result.state, "waiting_owner")
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 1)
            # One approval job plus the automatic reconciliation seed. The
            # unknown result must not create a second publish attempt.
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM jobs WHERE kind='publication_agent'"), 2)
            self.assertEqual(
                env.db.scalar(
                    "SELECT COUNT(*) FROM jobs WHERE kind='publication_agent' "
                    "AND state='pending' "
                    "AND COALESCE(json_extract(payload_json, '$.reconcile_only'), 0)=1"
                ),
                1,
            )
        finally:
            cleanup(env)

    def test_failed_send_is_retried_by_durable_job(self):
        env = new_env("publication-retry")
        try:
            made = make_draft(env)
            _publication_config(env)
            approve(env, made["approval"]["code"])
            task_id = int(env.db.fetchone("SELECT id FROM publication_tasks")["id"])
            runtime = LivePublicationRuntime(
                env.db,
                env.config,
                env.clock,
                publisher=FakePublisher(PublishOutcome(state="failed", detail="transient", failure_class="not_dispatched", retryable=True)),
            )
            runner = PublicationRunner(
                env.db, env.config, env.clock,
                ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}]),
                runtime,
            )
            result = runner.run(task_id)
            self.assertEqual(result.state, "retry_wait")
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 1)
            self.assertEqual(env.db.scalar("SELECT retry_count FROM publication_tasks"), 1)
            self.assertGreaterEqual(
                env.db.scalar("SELECT COUNT(*) FROM jobs WHERE kind='publication_agent'"), 2
            )
        finally:
            cleanup(env)

    def test_worker_handler_runs_publication_job(self):
        env = new_env("publication-worker")
        try:
            made = make_draft(env)
            _publication_config(env)
            approve(env, made["approval"]["code"])
            task_id = int(env.db.fetchone("SELECT id FROM publication_tasks")["id"])
            from bot.worker import Worker

            worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            worker.llm = ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}])
            worker.publisher = FakePublisher()
            result = worker._handle_publication_agent(
                {"payload_json": json.dumps({"task_id": task_id})}
            )
            self.assertEqual(result["state"], "published")
            self.assertEqual(
                env.db.scalar("SELECT state FROM publication_tasks WHERE id=?", (task_id,)),
                "published",
            )
        finally:
            cleanup(env)

    def test_owner_status_and_cancel_commands(self):
        env = new_env("publication-command")
        try:
            made = make_draft(env)
            _publication_config(env)
            approve(env, made["approval"]["code"])
            code = made["approval"]["code"]
            status = handle_inbound_message(
                env.db, env.config, env.clock,
                message_id="cmd-status-1", sender_id="owner-open-id",
                chat_id="owner-open-id", chat_type="p2p",
                text=f"发布状态 {code}", channel="test",
            )
            self.assertEqual(status["outcome"], "publication_status")
            self.assertTrue(status["detail"]["ok"])
            cancel = handle_inbound_message(
                env.db, env.config, env.clock,
                message_id="cmd-cancel-1", sender_id="owner-open-id",
                chat_id="owner-open-id", chat_type="p2p",
                text=f"取消发布 {code}", channel="test",
            )
            self.assertEqual(cancel["outcome"], "cancelled")
            self.assertEqual(
                env.db.scalar("SELECT state FROM publication_tasks"), "cancelled"
            )
        finally:
            cleanup(env)


    def test_real_inbound_approval_to_worker_publishes_once(self):
        env = new_env("publication-inbound-e2e")
        try:
            made = make_draft(env)
            _publication_config(env)
            approved = handle_inbound_message(
                env.db, env.config, env.clock,
                message_id="pub-e2e-approve-1",
                sender_id="owner-open-id",
                chat_id="owner-open-id",
                chat_type="p2p",
                text=f"批准 {made['approval']['code']}",
                channel="test",
            )
            self.assertEqual(approved["outcome"], "approved")
            self.assertEqual(
                env.db.scalar("SELECT COUNT(*) FROM jobs WHERE kind='publication_agent'"), 1
            )
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM jobs WHERE kind='publish'"), 0)
            from bot.worker import Worker

            worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            worker.publisher = FakePublisher()
            worker.llm = ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}])
            summary = worker.tick()
            self.assertTrue(any(job.get("kind") == "publication_agent" for job in summary["jobs"]))
            self.assertEqual(worker.publisher.calls, 1)
            self.assertEqual(
                env.db.scalar("SELECT state FROM publication_tasks"), "published"
            )
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 1)
        finally:
            cleanup(env)

    def test_real_inbound_unknown_then_owner_confirmation_publishes_second_attempt(self):
        env = new_env("publication-inbound-unknown")
        try:
            made = make_draft(env)
            _publication_config(env)
            code = made["approval"]["code"]
            approved = handle_inbound_message(
                env.db, env.config, env.clock,
                message_id="pub-e2e-unknown-approve",
                sender_id="owner-open-id", chat_id="owner-open-id", chat_type="p2p",
                text=f"批准 {code}", channel="test",
            )
            self.assertEqual(approved["outcome"], "approved")
            from bot.worker import Worker

            first = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            first.publisher = FakePublisher(PublishOutcome(state="unknown", detail="unknown"))
            first.llm = ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}])
            first.tick()
            self.assertEqual(env.db.scalar("SELECT state FROM publication_tasks"), "waiting_owner")
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 1)

            confirmed = handle_inbound_message(
                env.db, env.config, env.clock,
                message_id="pub-e2e-unknown-confirm",
                sender_id="owner-open-id", chat_id="owner-open-id", chat_type="p2p",
                text=f"确认未发布并重试 {code}", channel="test",
            )
            self.assertEqual(confirmed["outcome"], "retry_queued")
            second = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            second.publisher = FakePublisher()
            second.llm = ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}])
            second.tick()
            self.assertEqual(env.db.scalar("SELECT state FROM publication_tasks"), "published")
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 2)
            self.assertEqual(second.publisher.calls, 1)
        finally:
            cleanup(env)

    def test_real_inbound_login_recovery_publishes_after_owner_ready(self):
        env = new_env("publication-inbound-login")
        try:
            made = make_draft(env)
            _publication_config(env)
            code = made["approval"]["code"]
            approved = handle_inbound_message(
                env.db, env.config, env.clock,
                message_id="pub-e2e-login-approve",
                sender_id="owner-open-id", chat_id="owner-open-id", chat_type="p2p",
                text=f"批准 {code}", channel="test",
            )
            self.assertEqual(approved["outcome"], "approved")
            from bot.worker import Worker

            first = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            first.publisher = LoggedOutPublisher()
            first.llm = ScriptedLLM([
                {"action": "check_login", "args": {}},
                {"action": "request_owner_action", "args": {"kind": "login", "question": "请重新扫码登录"}},
            ])
            first.tick()
            self.assertEqual(env.db.scalar("SELECT state FROM publication_tasks"), "waiting_login")
            self.assertEqual(first.publisher.calls, 0)

            logged_in = handle_inbound_message(
                env.db, env.config, env.clock,
                message_id="pub-e2e-login-ready",
                sender_id="owner-open-id", chat_id="owner-open-id", chat_type="p2p",
                text=f"已登录 {code}", channel="test",
            )
            self.assertEqual(logged_in["outcome"], "login_check_queued")

            second = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            second.publisher = FakePublisher()
            second.llm = ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}])
            second.tick()
            self.assertEqual(env.db.scalar("SELECT state FROM publication_tasks"), "published")
            self.assertEqual(second.publisher.calls, 1)
        finally:
            cleanup(env)

    def test_notification_failure_does_not_republish_or_duplicate_receipt(self):
        env = new_env("publication-notify-failure")
        try:
            made = make_draft(env)
            _publication_config(env)
            approve(env, made["approval"]["code"])
            task_id = int(env.db.fetchone("SELECT id FROM publication_tasks")["id"])
            from bot.worker import Worker

            class FailOnce:
                def __init__(self):
                    self.calls = 0
                def send_text(self, text):
                    self.calls += 1
                    if self.calls == 1:
                        return SimpleNamespace(ok=False, detail="temporary feishu outage", channel="test", message_id="")
                    return SimpleNamespace(ok=True, detail="sent", channel="test", message_id="msg-ok")

            worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            worker.publisher = FakePublisher()
            worker.llm = ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}])
            worker.notifier = FailOnce()
            result = worker._handle_publication_agent({"payload_json": json.dumps({"task_id": task_id})})
            self.assertEqual(result["state"], "published")
            self.assertEqual(worker.publisher.calls, 1)
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 1)
            # The failed result notification stays retryable; a later delivery must not publish again.
            from bot.services.notifications import deliver_pending
            delivered = deliver_pending(env.db, env.config, env.clock, worker.notifier)
            self.assertTrue(any(item.get("ok") for item in delivered))
            self.assertEqual(worker.publisher.calls, 1)
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 1)
        finally:
            cleanup(env)

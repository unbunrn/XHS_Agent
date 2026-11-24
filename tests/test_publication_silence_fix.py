"""Reproduce and fence the XH-HMP6VB silent-stop and silent-command bug.

The production incident: eight read-only tool calls exhausted the step limit,
the task moved to waiting_owner, but no Feishu message was queued.  Later,
"发布状态" and "检查原因" wrote an inbound row but still queued no reply.

These tests use the real inbound handler, SQLite queue, runner and notification
table; only the model and publisher are scripted.
"""
from __future__ import annotations

import json
from unittest import TestCase

from bot.services.approvals import handle_inbound_message
from bot.services.publication_agent.live import LivePublicationRuntime
from bot.services.publication_agent.runner import PublicationRunner
from tests.helpers import approve, cleanup, make_draft, new_env
from tests.test_publication_agent import FakePublisher, ScriptedLLM, _publication_config


READ_ONLY_DECISIONS = [
    {"action": "inspect_task", "args": {}},
    {"action": "check_bridge_health", "args": {}},
    {"action": "verify_assets", "args": {}},
    {"action": "inspect_failure", "args": {}},
    {"action": "inspect_task", "args": {}},
    {"action": "check_publish_result", "args": {}},
    {"action": "check_bridge_health", "args": {}},
    {"action": "verify_assets", "args": {}},
]


def _task_id(env) -> int:
    return int(env.db.fetchone("SELECT id FROM publication_tasks")["id"])


def _clear_notifications(env) -> None:
    env.db.execute("DELETE FROM notifications")


def _status(env, message_id: str, code: str):
    return handle_inbound_message(
        env.db, env.config, env.clock,
        message_id=message_id,
        sender_id="owner-open-id",
        chat_id="owner-open-id",
        chat_type="p2p",
        text=f"发布状态 {code}",
        channel="test",
    )


def _latest_outcome(env):
    return env.db.fetchone(
        "SELECT * FROM notifications WHERE kind='outcome' ORDER BY id DESC LIMIT 1"
    )


class SilentStopReproductionTests(TestCase):
    def test_step_limit_queues_visible_stop_receipt(self):
        env = new_env("silence-step-limit")
        try:
            made = make_draft(env)
            _publication_config(env)
            env.config.data["publication_agent"]["max_steps"] = 8
            env.config.data["publication_agent"]["max_model_calls"] = 20
            approve(env, made["approval"]["code"])
            _clear_notifications(env)

            runtime = LivePublicationRuntime(
                env.db, env.config, env.clock, publisher=FakePublisher()
            )
            runner = PublicationRunner(
                env.db, env.config, env.clock,
                ScriptedLLM(list(READ_ONLY_DECISIONS)), runtime,
            )
            result = runner.run(_task_id(env))

            self.assertEqual(result.state, "waiting_owner")
            notice = _latest_outcome(env)
            self.assertIsNotNone(
                notice,
                "step-limit stop must queue a visible receipt, not only wait_reason",
            )
            body = json.loads(notice["payload_json"])["text"]
            self.assertIn(made["approval"]["code"], body)
            self.assertIn("尚未提交", body)
            self.assertIn("修复并重试", body)
            self.assertNotIn("已发布", body)
        finally:
            cleanup(env)

    def test_waiting_status_command_queues_visible_reply(self):
        env = new_env("silence-status")
        try:
            made = make_draft(env)
            _publication_config(env)
            approve(env, made["approval"]["code"])
            task_id = _task_id(env)
            env.db.execute(
                "UPDATE publication_tasks SET state='waiting_owner', "
                "wait_reason='publication agent step limit reached' WHERE id=?",
                (task_id,),
            )
            _clear_notifications(env)

            result = _status(env, "silence-status-1", made["approval"]["code"])

            self.assertEqual(result["outcome"], "publication_status")
            notice = _latest_outcome(env)
            self.assertIsNotNone(notice, "owner status command must queue a Feishu reply")
            body = json.loads(notice["payload_json"])["text"]
            self.assertIn(made["approval"]["code"], body)
            self.assertIn("尚未提交", body)
            self.assertIn("等待", body)
            self.assertNotIn("PB-", body)
        finally:
            cleanup(env)

    def test_published_status_reports_note_id_and_ignores_stale_wait_reason(self):
        env = new_env("silence-published")
        try:
            made = make_draft(env)
            _publication_config(env)
            approve(env, made["approval"]["code"])
            runtime = LivePublicationRuntime(
                env.db, env.config, env.clock, publisher=FakePublisher()
            )
            runner = PublicationRunner(
                env.db, env.config, env.clock,
                ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}]),
                runtime,
            )
            self.assertEqual(runner.run(_task_id(env)).state, "published")
            # This is exactly what the live task 2 looked like after recovery.
            env.db.execute(
                "UPDATE publication_tasks SET wait_reason='owner requested repair/retry'"
            )
            _clear_notifications(env)

            result = _status(env, "silence-published-1", made["approval"]["code"])

            self.assertEqual(result["outcome"], "publication_status")
            body = json.loads(_latest_outcome(env)["payload_json"])["text"]
            self.assertIn("已发布", body)
            self.assertIn("note-fake-1", body)
            self.assertNotIn("owner requested repair/retry", body)
            self.assertNotIn("等待重试", body)
        finally:
            cleanup(env)

    def test_duplicate_status_callback_queues_only_one_reply(self):
        env = new_env("silence-status-duplicate")
        try:
            made = make_draft(env)
            _publication_config(env)
            approve(env, made["approval"]["code"])
            _clear_notifications(env)

            first = _status(env, "silence-status-dup", made["approval"]["code"])
            second = _status(env, "silence-status-dup", made["approval"]["code"])

            self.assertEqual(first["outcome"], "publication_status")
            self.assertTrue(second.get("duplicate"))
            self.assertEqual(
                env.db.scalar(
                    "SELECT COUNT(*) FROM notifications WHERE dedupe_key=?",
                    ("inbound:silence-status-dup",),
                ),
                1,
            )
        finally:
            cleanup(env)

    def test_check_reason_acknowledges_before_diagnosis(self):
        env = new_env("silence-check-reason")
        try:
            made = make_draft(env)
            _publication_config(env)
            approve(env, made["approval"]["code"])
            _clear_notifications(env)

            result = handle_inbound_message(
                env.db, env.config, env.clock,
                message_id="silence-check-1",
                sender_id="owner-open-id", chat_id="owner-open-id", chat_type="p2p",
                text=f"检查原因 {made['approval']['code']}", channel="test",
            )

            self.assertEqual(result["outcome"], "publication_status")
            notice = _latest_outcome(env)
            self.assertIsNotNone(notice)
            body = json.loads(notice["payload_json"])["text"]
            self.assertIn(made["approval"]["code"], body)
            self.assertIn("已收到", body)
            self.assertIn("只读检查", body)
            self.assertIn("不会发布", body)
        finally:
            cleanup(env)


class OwnerReplyScopeTests(TestCase):
    def test_group_chat_command_queues_no_reply(self):
        env = new_env("silence-group")
        try:
            made = make_draft(env)
            _publication_config(env)
            approve(env, made["approval"]["code"])
            _clear_notifications(env)

            result = handle_inbound_message(
                env.db, env.config, env.clock,
                message_id="silence-group-1",
                sender_id="owner-open-id", chat_id="group-1", chat_type="group",
                text=f"发布状态 {made['approval']['code']}", channel="test",
            )

            self.assertEqual(result["outcome"], "rejected_chat_type")
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM notifications"), 0)
        finally:
            cleanup(env)

    def test_non_owner_command_queues_no_reply(self):
        env = new_env("silence-non-owner")
        try:
            made = make_draft(env)
            _publication_config(env)
            approve(env, made["approval"]["code"])
            _clear_notifications(env)

            result = handle_inbound_message(
                env.db, env.config, env.clock,
                message_id="silence-non-owner-1",
                sender_id="stranger", chat_id="stranger", chat_type="p2p",
                text=f"发布状态 {made['approval']['code']}", channel="test",
            )

            self.assertEqual(result["outcome"], "rejected_sender")
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM notifications"), 0)
        finally:
            cleanup(env)


class StopReasonCoverageTests(TestCase):
    def _run_reads(self, env, decisions, *, max_steps):
        _publication_config(env)
        env.config.data["publication_agent"]["max_steps"] = max_steps
        env.config.data["publication_agent"]["max_model_calls"] = 20
        approve(env, make_draft(env)["approval"]["code"])
        _clear_notifications(env)
        publisher = FakePublisher()
        runtime = LivePublicationRuntime(
            env.db, env.config, env.clock, publisher=publisher
        )
        runner = PublicationRunner(
            env.db, env.config, env.clock, ScriptedLLM(list(decisions)), runtime
        )
        return runner, publisher

    def test_model_budget_stop_queues_receipt(self):
        env = new_env("silence-budget")
        try:
            runner, _ = self._run_reads(
                env, [{"action": "inspect_task", "args": {}}] * 2, max_steps=6
            )
            env.config.data["publication_agent"]["max_model_calls"] = 1

            result = runner.run(_task_id(env))

            self.assertEqual(result.state, "waiting_owner")
            body = json.loads(_latest_outcome(env)["payload_json"])["text"]
            self.assertIn("预算", body)
            self.assertIn("尚未提交", body)
        finally:
            cleanup(env)

    def test_invalid_decisions_stop_queues_receipt(self):
        env = new_env("silence-invalid")
        try:
            runner, _ = self._run_reads(env, [{}, {}], max_steps=6)

            result = runner.run(_task_id(env))

            self.assertEqual(result.state, "waiting_owner")
            body = json.loads(_latest_outcome(env)["payload_json"])["text"]
            self.assertIn("无效", body)
            self.assertIn("尚未提交", body)
        finally:
            cleanup(env)

    def test_repeated_read_only_check_stops_with_reason(self):
        env = new_env("silence-no-progress")
        try:
            runner, publisher = self._run_reads(
                env, [{"action": "inspect_task", "args": {}}] * 4, max_steps=6
            )

            result = runner.run(_task_id(env))

            self.assertEqual(result.state, "waiting_owner")
            self.assertEqual(publisher.calls, 0)
            body = json.loads(_latest_outcome(env)["payload_json"])["text"]
            self.assertIn("没有新发现", body)
            self.assertIn("尚未提交", body)
        finally:
            cleanup(env)

    def test_new_batch_steps_remaining_does_not_use_old_decisions(self):
        env = new_env("silence-budget-new-batch")
        try:
            runner, _ = self._run_reads(
                env, [{"action": "inspect_task", "args": {}}] * 2, max_steps=2
            )
            self.assertEqual(runner.run(_task_id(env)).state, "waiting_owner")

            task = runner.store.get(task_id=_task_id(env))
            package = runner.store.package(task)
            allowed = runner._allowed_actions(task, allow_publish=True, diagnose_only=False)
            before = runner._observation(task, package, allowed)
            self.assertEqual(len(before["checks"]), 1)
            self.assertEqual(before["history"][-1]["action"], "inspect_task")
            self.assertIsNotNone(before["last_observation"])
            self.assertNotIn(
                "model_decision",
                [row["action"] for row in before["history"]],
            )

            runner.store.batch_start(_task_id(env), force=True)
            observation = runner._observation(task, package, allowed)
            self.assertEqual(observation["budget"]["steps_remaining"], 2)
            self.assertEqual(observation["checks"], [])
        finally:
            cleanup(env)


class RecoveryAndStatusCoverageTests(TestCase):
    def test_diagnosis_completion_queues_receipt_without_state_change(self):
        env = new_env("silence-diagnosis-done")
        try:
            made = make_draft(env)
            _publication_config(env)
            env.config.data["publication_agent"]["max_steps"] = 1
            approve(env, made["approval"]["code"])
            task_id = _task_id(env)
            env.db.execute(
                "UPDATE publication_tasks SET state='waiting_owner', "
                "wait_reason='publication agent step limit reached' WHERE id=?",
                (task_id,),
            )
            _clear_notifications(env)
            runtime = LivePublicationRuntime(
                env.db, env.config, env.clock, publisher=FakePublisher()
            )
            runner = PublicationRunner(
                env.db, env.config, env.clock,
                ScriptedLLM([{"action": "inspect_task", "args": {}}]), runtime,
            )

            result = runner.run(task_id, diagnose_only=True)

            self.assertEqual(result.state, "waiting_owner")
            task = runner.store.get(task_id=task_id)
            self.assertEqual(task.state, "waiting_owner")
            body = json.loads(_latest_outcome(env)["payload_json"])["text"]
            self.assertIn("检查完成", body)
            self.assertIn("只读", body)
        finally:
            cleanup(env)

    def test_check_reason_on_published_reports_status_without_job(self):
        env = new_env("silence-published-diagnose")
        try:
            made = make_draft(env)
            _publication_config(env)
            approve(env, made["approval"]["code"])
            task_id = _task_id(env)
            env.db.execute(
                "UPDATE publication_tasks SET state='published', "
                "wait_reason='owner requested repair/retry' WHERE id=?",
                (task_id,),
            )
            env.db.execute(
                "INSERT INTO publish_attempts(approval_id,draft_id,dedupe_key,platform,"
                "account_id,mode,state,external_post_id,publication_task_id,attempt_no,"
                "dispatch_state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    made["approval"]["id"], made["draft"]["id"], "silence:published",
                    "xiaohongshu", "xhs-main", "live", "published",
                    "note-published-1", task_id, 1, "dispatched",
                    "2026-11-05T09:00:00Z", "2026-11-05T09:00:00Z",
                ),
            )
            _clear_notifications(env)
            jobs_before = env.db.scalar(
                "SELECT COUNT(*) FROM jobs WHERE kind='publication_agent'"
            )

            result = handle_inbound_message(
                env.db, env.config, env.clock,
                message_id="silence-published-diagnose-1",
                sender_id="owner-open-id", chat_id="owner-open-id",
                chat_type="p2p", text=f"检查原因 {made['approval']['code']}",
                channel="test",
            )

            self.assertEqual(result["outcome"], "publication_status")
            self.assertIsNone(result.get("job_id"))
            jobs_after = env.db.scalar(
                "SELECT COUNT(*) FROM jobs WHERE kind='publication_agent'"
            )
            self.assertEqual(jobs_after, jobs_before)
            body = json.loads(_latest_outcome(env)["payload_json"])["text"]
            self.assertIn("无需继续检查", body)
            self.assertIn("已发布", body)
            self.assertIn("note-published-1", body)
            self.assertNotIn("完成后会再发结果", body)
        finally:
            cleanup(env)

    def test_unknown_status_says_maybe_submitted(self):
        env = new_env("silence-unknown-status")
        try:
            made = make_draft(env)
            _publication_config(env)
            approve(env, made["approval"]["code"])
            task_id = _task_id(env)
            env.db.execute(
                "UPDATE publication_tasks SET state='waiting_owner', "
                "wait_reason='publish result unknown; reconcile required' WHERE id=?",
                (task_id,),
            )
            env.db.execute(
                "INSERT INTO publish_attempts(approval_id,draft_id,dedupe_key,platform,"
                "account_id,mode,state,publication_task_id,attempt_no,dispatch_state,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    made["approval"]["id"], made["draft"]["id"], "silence:unknown",
                    "xiaohongshu", "xhs-main", "live", "unknown", task_id, 1,
                    "unknown", "2026-11-05T09:00:00Z", "2026-11-05T09:00:00Z",
                ),
            )
            _clear_notifications(env)

            _status(env, "silence-unknown-1", made["approval"]["code"])

            body = json.loads(_latest_outcome(env)["payload_json"])["text"]
            self.assertIn("可能", body)
            self.assertIn("不要直接重发", body)
            self.assertNotIn("尚未提交", body)
        finally:
            cleanup(env)

    def test_processing_crash_replay_queues_one_fallback(self):
        env = new_env("silence-processing")
        try:
            made = make_draft(env)
            _publication_config(env)
            approve(env, made["approval"]["code"])
            _clear_notifications(env)
            env.db.execute(
                "INSERT INTO inbound_messages(message_id,channel,sender_id,chat_id,"
                "chat_type,text,received_at,outcome,detail) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "silence-processing-1", "test", "owner-open-id", "owner-open-id",
                    "p2p", f"发布状态 {made['approval']['code']}",
                    "2026-11-05T08:50:00Z", "processing", None,
                ),
            )

            result = _status(env, "silence-processing-1", made["approval"]["code"])

            self.assertTrue(result.get("duplicate"))
            self.assertEqual(
                env.db.scalar(
                    "SELECT COUNT(*) FROM notifications WHERE dedupe_key=?",
                    ("inbound:silence-processing-1",),
                ),
                1,
            )
            body = json.loads(_latest_outcome(env)["payload_json"])["text"]
            self.assertIn("处理中断", body)
            self.assertIn("重新发送", body)
        finally:
            cleanup(env)

    def test_pause_resume_cancel_are_explicitly_acknowledged(self):
        env = new_env("silence-lifecycle-acks")
        try:
            made = make_draft(env)
            _publication_config(env)
            approve(env, made["approval"]["code"])
            code = made["approval"]["code"]
            _clear_notifications(env)

            for index, (text, expected) in enumerate(
                [
                    (f"暂停发布 {code}", "已暂停"),
                    (f"继续发布 {code}", "已恢复"),
                    (f"取消发布 {code}", "已取消"),
                ],
                start=1,
            ):
                result = handle_inbound_message(
                    env.db, env.config, env.clock,
                    message_id=f"silence-lifecycle-{index}",
                    sender_id="owner-open-id", chat_id="owner-open-id",
                    chat_type="p2p", text=text, channel="test",
                )
                self.assertTrue(result["ok"])
                body = json.loads(_latest_outcome(env)["payload_json"])["text"]
                self.assertIn(expected, body)
                self.assertIn(code, body)
        finally:
            cleanup(env)

    def test_inflight_duplicate_is_not_reported_as_crashed(self):
        env = new_env("silence-processing-fresh")
        try:
            made = make_draft(env)
            _publication_config(env)
            approve(env, made["approval"]["code"])
            _clear_notifications(env)
            env.db.execute(
                "INSERT INTO inbound_messages(message_id,channel,sender_id,chat_id,"
                "chat_type,text,received_at,outcome,detail) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "silence-processing-fresh-1", "test", "owner-open-id",
                    "owner-open-id", "p2p",
                    f"发布状态 {made['approval']['code']}",
                    "2026-11-05T09:00:00Z", "processing", None,
                ),
            )

            result = _status(
                env, "silence-processing-fresh-1", made["approval"]["code"]
            )

            self.assertTrue(result.get("duplicate"))
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM notifications"), 0)
        finally:
            cleanup(env)

    def test_stop_then_repair_publishes_only_once(self):
        env = new_env("silence-stop-repair")
        try:
            made = make_draft(env)
            _publication_config(env)
            env.config.data["publication_agent"]["max_steps"] = 1
            approve(env, made["approval"]["code"])
            task_id = _task_id(env)
            _clear_notifications(env)
            publisher = FakePublisher()
            runtime = LivePublicationRuntime(
                env.db, env.config, env.clock, publisher=publisher
            )
            runner = PublicationRunner(
                env.db, env.config, env.clock,
                ScriptedLLM([{"action": "inspect_task", "args": {}}]), runtime,
            )
            self.assertEqual(runner.run(task_id).state, "waiting_owner")
            self.assertEqual(publisher.calls, 0)

            repaired = handle_inbound_message(
                env.db, env.config, env.clock,
                message_id="silence-stop-repair-1",
                sender_id="owner-open-id", chat_id="owner-open-id", chat_type="p2p",
                text=f"修复并重试 {made['approval']['code']}", channel="test",
            )
            self.assertEqual(repaired["outcome"], "retry_queued")
            repair_body = json.loads(_latest_outcome(env)["payload_json"])["text"]
            self.assertIn("接受修复", repair_body)
            self.assertNotIn("结果未知", repair_body)

            retry_runner = PublicationRunner(
                env.db, env.config, env.clock,
                ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}]),
                runtime,
            )
            result = retry_runner.run(task_id)

            self.assertEqual(result.state, "published")
            self.assertEqual(publisher.calls, 1)
            self.assertEqual(
                env.db.scalar(
                    "SELECT COUNT(*) FROM publish_attempts WHERE approval_id=?",
                    (made["approval"]["id"],),
                ),
                1,
            )
            self.assertEqual(
                env.db.scalar(
                    "SELECT external_post_id FROM publish_attempts WHERE approval_id=?",
                    (made["approval"]["id"],),
                ),
                "note-fake-1",
            )
        finally:
            cleanup(env)

"""Offline gray-deployment demo for publication recovery round 2.

The external bridge and Feishu transport are simulated, but the inbound
handler, SQLite queue, worker, publication runner, real Xiaohongshu adapter,
asset restore path, and notification consumer are the production components.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from bot.adapters.notify import NotificationResult
from bot.db import Database
from bot.services.approvals import handle_inbound_message
from bot.adapters.publishers import XiaohongshuPublisher
from bot.config import Secrets
from bot.services.publication_agent.live import LivePublicationRuntime
from bot.worker import Worker
from tests.helpers import approve, cleanup, make_draft, new_env
from tests.test_publication_agent import ScriptedLLM, _publication_config
from tests.test_xhs_identity import FakeBridgeHttp, _me_payload


class CaptureNotifier:
    name = "capture"
    channel = "capture"

    def __init__(self):
        self.approvals = []
        self.texts = []

    def send_approval(self, message):
        self.approvals.append(message)
        return NotificationResult(ok=True, channel="capture", message_id=f"approval-{len(self.approvals)}")

    def send_text(self, text):
        self.texts.append(text)
        return NotificationResult(ok=True, channel="capture", message_id=f"text-{len(self.texts)}")

    def health(self):
        return {"ok": True}


class FailingNotifier(CaptureNotifier):
    def send_approval(self, message):
        self.approvals.append(message)
        return NotificationResult(ok=False, channel="fail", detail="simulated delivery failure")

    def send_text(self, text):
        self.texts.append(text)
        return NotificationResult(ok=False, channel="fail", detail="simulated delivery failure")


def _live_bridge_env(env, bridge):
    env.config.data["app"]["mode"] = "live"
    env.config.data["publisher"]["provider"] = "xiaohongshu"
    env.config.data["publisher"]["live_enabled"] = True
    env.config.data["publisher"]["xiaohongshu"]["base_url"] = "http://127.0.0.1:18060"
    env.config.data["publisher"]["xiaohongshu"]["expected_red_id"] = "red-123"
    env.config.secrets = Secrets(xiaohongshu_token="test-token")
    with mock.patch.dict(os.environ, {"BOT_ALLOW_REAL_PUBLISH": "1"}):
        publisher = XiaohongshuPublisher(env.config, env.clock, allow_unarmed=True)
    publisher.http = bridge
    publisher.armed = True
    publisher.read_only = False
    # The synthetic fixture is not a real live account.  Keep the production
    # adapter code path while avoiding the live-fixture guard.
    publisher.mode = "mock"
    return publisher


def _me_payload_for(red_id: str, notes=()):
    feeds = [
        {
            "id": note_id,
            "xsecToken": token,
            "noteCard": {"displayTitle": title},
        }
        for note_id, title, token in notes
    ]
    return (
        200,
        {
            "success": True,
            "data": {
                "data": {
                    "userBasicInfo": {"redId": red_id},
                    "feeds": feeds,
                }
            },
        },
    )


class GrayDeploymentDemo(unittest.TestCase):
    def test_asset_repair_success_and_notification_restart(self):
        env = new_env("round2-gray-demo")
        try:
            made = make_draft(env)
            _publication_config(env)
            bridge = FakeBridgeHttp(me=_me_payload())
            publisher = _live_bridge_env(env, bridge)
            capture = CaptureNotifier()

            approved = approve(env, made["approval"]["code"], message_id="approval-event-1")
            self.assertEqual(approved["outcome"], "approved")
            task = env.db.fetchone("SELECT * FROM publication_tasks")
            self.assertIsNotNone(task)
            task_id = int(task["id"])

            image = env.db.fetchone(
                "SELECT path FROM draft_images WHERE draft_id=? ORDER BY ordinal LIMIT 1",
                (int(made["approval"]["draft_id"]),),
            )
            self.assertIsNotNone(image)
            image_path = Path(image["path"])
            self.assertTrue(image_path.is_file())
            image_path.unlink()

            worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            worker.publisher = publisher
            worker.notifier = capture
            worker.llm = ScriptedLLM([
                {"action": "verify_assets", "args": {}},
                {"action": "publish_approved_artifact", "args": {}},
            ])
            worker.tick()

            failed_task = env.db.fetchone("SELECT * FROM publication_tasks WHERE id=?", (task_id,))
            self.assertEqual(failed_task["state"], "waiting_owner")
            self.assertEqual(bridge.posts, [])
            self.assertTrue(
                any("发布前被拦截" in text or "image missing" in text for text in capture.texts),
                capture.texts,
            )

            # The manifest must have kept exact bytes in a program-owned backup.
            from bot.services.publication_agent.diagnostics import manifest_path

            manifest_root = env.config.resolve_path("var/backups/publication_assets")
            frozen_package = json.loads(failed_task["approved_package_json"])
            self.assertTrue(manifest_path(frozen_package, backup_root=manifest_root).is_file())

            repair = approve(
                env, made["approval"]["code"],
                text=f"修复并重试 {made['approval']['code']}",
            )
            self.assertEqual(repair["outcome"], "retry_queued")
            worker.notifier = FailingNotifier()
            worker.llm = ScriptedLLM([
                {"action": "restore_approved_assets", "args": {"asset_ids": ["1"]}},
                {"action": "verify_assets", "args": {}},
                {"action": "publish_approved_artifact", "args": {}},
            ])
            worker.tick()

            published = env.db.fetchone("SELECT * FROM publication_tasks WHERE id=?", (task_id,))
            self.assertEqual(published["state"], "published")
            self.assertEqual(len(bridge.posts), 1)
            attempt = env.db.fetchone(
                "SELECT * FROM publish_attempts WHERE publication_task_id=? ORDER BY id DESC LIMIT 1",
                (task_id,),
            )
            self.assertEqual(attempt["state"], "published")
            self.assertEqual(attempt["external_post_id"], "note-1")
            self.assertTrue(image_path.is_file())

            # Simulate a restart after the publish transaction committed but
            # before the outcome notification was delivered.
            env.clock.advance(seconds=61)
            restarted = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            restarted.publisher = publisher
            restarted.notifier = capture
            restarted.llm = ScriptedLLM([])
            restarted.tick()
            self.assertEqual(len(bridge.posts), 1)
            self.assertTrue(any("发布" in text or "成功" in text or "note-1" in text for text in capture.texts), capture.texts)

            # Replaying the same owner command must not create a second send.
            approve(env, made["approval"]["code"], message_id="approval-event-1", text=f"批准 {made['approval']['code']}")
            self.assertEqual(len(bridge.posts), 1)
            still = env.db.fetchone("SELECT * FROM publication_tasks WHERE id=?", (task_id,))
            self.assertEqual(still["state"], "published")
        finally:
            cleanup(env)

    def _run_account_switch_case(self, *, status: int, publish_payload: dict):
        env = new_env(f"round2-account-switch-{status}")
        try:
            made = make_draft(env)
            _publication_config(env)
            bridge = FakeBridgeHttp(
                me=_me_payload(),
                publish=(status, publish_payload),
                # The POST lands while the bridge is now looking at account B.
                me_after=_me_payload_for(
                    "other-account",
                    (("other-note", made["draft"]["title"], "other-token"),),
                ),
            )
            publisher = _live_bridge_env(env, bridge)
            capture = CaptureNotifier()
            approved = approve(env, made["approval"]["code"], message_id=f"account-switch-{status}")
            self.assertEqual(approved["outcome"], "approved")
            task = env.db.fetchone("SELECT * FROM publication_tasks")
            task_id = int(task["id"])

            worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            worker.publisher = publisher
            worker.notifier = capture
            worker.llm = ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}])
            with mock.patch("bot.adapters.publishers.time.sleep", lambda _: None):
                worker.tick()

            task = env.db.fetchone("SELECT * FROM publication_tasks WHERE id=?", (task_id,))
            self.assertEqual(task["state"], "waiting_owner")
            attempt = env.db.fetchone(
                "SELECT * FROM publish_attempts WHERE publication_task_id=? ORDER BY id DESC LIMIT 1",
                (task_id,),
            )
            self.assertEqual(attempt["state"], "unknown")
            self.assertEqual(len(bridge.posts), 1)
            self.assertFalse(
                any("发布成功" in text or "note-" in text for text in capture.texts),
                capture.texts,
            )

            # A second worker tick must not resend the unknown attempt.
            worker.llm = ScriptedLLM([])
            worker.tick()
            self.assertEqual(len(bridge.posts), 1)

            # Restart-style reconciliation goes through the same queue path and
            # sees account B, so it must remain unknown as well.
            repair = approve(
                env, made["approval"]["code"],
                message_id=f"account-switch-repair-{status}",
                text=f"修复并重试 {made['approval']['code']}",
            )
            self.assertEqual(repair["outcome"], "unknown_requires_reconciliation")
            worker.tick()
            task = env.db.fetchone("SELECT * FROM publication_tasks WHERE id=?", (task_id,))
            self.assertEqual(task["state"], "waiting_owner")
            attempt = env.db.fetchone(
                "SELECT * FROM publish_attempts WHERE publication_task_id=? ORDER BY id DESC LIMIT 1",
                (task_id,),
            )
            self.assertEqual(attempt["state"], "unknown")
            self.assertEqual(len(bridge.posts), 1)
            self.assertFalse(
                any("发布成功" in text or "note-" in text for text in capture.texts),
                capture.texts,
            )
        finally:
            cleanup(env)

    def test_200_without_id_cross_account_note_stays_unknown(self):
        self._run_account_switch_case(
            status=200,
            publish_payload={"success": True},
        )

    def test_504_cross_account_note_stays_unknown(self):
        self._run_account_switch_case(
            status=504,
            publish_payload={"error": "gateway timeout"},
        )

    def test_200_without_id_same_account_new_note_publishes(self):
        env = new_env("round2-account-same")
        try:
            made = make_draft(env)
            _publication_config(env)
            bridge = FakeBridgeHttp(
                me=_me_payload(),
                publish=(200, {"success": True}),
                me_after=_me_payload_for(
                    "red-123",
                    (("same-account-note", made["draft"]["title"], "same-token"),),
                ),
            )
            publisher = _live_bridge_env(env, bridge)
            capture = CaptureNotifier()
            approved = approve(env, made["approval"]["code"], message_id="account-same")
            self.assertEqual(approved["outcome"], "approved")
            task = env.db.fetchone("SELECT * FROM publication_tasks")
            task_id = int(task["id"])

            worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            worker.publisher = publisher
            worker.notifier = capture
            worker.llm = ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}])
            worker.tick()

            task = env.db.fetchone("SELECT * FROM publication_tasks WHERE id=?", (task_id,))
            self.assertEqual(task["state"], "published")
            self.assertEqual(len(bridge.posts), 1)
            attempt = env.db.fetchone(
                "SELECT * FROM publish_attempts WHERE publication_task_id=? ORDER BY id DESC LIMIT 1",
                (task_id,),
            )
            self.assertEqual(attempt["state"], "published")
            self.assertEqual(attempt["external_post_id"], "same-account-note")
        finally:
            cleanup(env)

    def test_login_failure_then_owner_login_resumes_without_second_approval(self):
        env = new_env("round2-gray-login")
        try:
            made = make_draft(env)
            _publication_config(env)
            bridge = FakeBridgeHttp(
                me=_me_payload(),
                login=(200, {"is_logged_in": False}),
            )
            publisher = _live_bridge_env(env, bridge)
            capture = CaptureNotifier()
            approved = approve(env, made["approval"]["code"], message_id="login-approval-1")
            self.assertEqual(approved["outcome"], "approved")
            task = env.db.fetchone("SELECT * FROM publication_tasks")
            task_id = int(task["id"])

            worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            worker.publisher = publisher
            worker.notifier = capture
            worker.llm = ScriptedLLM([
                {"action": "check_login", "args": {}},
                {
                    "action": "request_owner_action",
                    "args": {"question": "请用手机扫码登录小红书", "kind": "login"},
                },
            ])
            worker.tick()

            task = env.db.fetchone("SELECT * FROM publication_tasks WHERE id=?", (task_id,))
            self.assertEqual(task["state"], "waiting_login")
            self.assertEqual(bridge.posts, [])
            self.assertTrue(any("扫码登录" in text for text in capture.texts), capture.texts)

            # The owner completes the phone login out of band, then tells the
            # bot.  The already-approved package is resumed; no reapproval.
            bridge.login = (200, {"is_logged_in": True})
            resumed = approve(
                env, made["approval"]["code"],
                message_id="login-resume-1",
                text=f"已登录 {made['approval']['code']}",
            )
            self.assertEqual(resumed["outcome"], "login_check_queued")

            worker.llm = ScriptedLLM([
                {"action": "check_login", "args": {}},
                {"action": "verify_assets", "args": {}},
                {"action": "publish_approved_artifact", "args": {}},
            ])
            worker.tick()

            task = env.db.fetchone("SELECT * FROM publication_tasks WHERE id=?", (task_id,))
            self.assertEqual(task["state"], "published")
            self.assertEqual(len(bridge.posts), 1)
            attempt = env.db.fetchone(
                "SELECT * FROM publish_attempts WHERE publication_task_id=? ORDER BY id DESC LIMIT 1",
                (task_id,),
            )
            self.assertEqual(attempt["state"], "published")
            self.assertEqual(attempt["external_post_id"], "note-1")
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM publication_tasks"), 1)
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM approvals WHERE status='approved'"), 0)
        finally:
            cleanup(env)

    def test_timeout_unknown_repair_backs_off_then_stops_with_owner_notice(self):
        env = new_env("round2-gray-unknown-exhaust")
        try:
            made = make_draft(env)
            _publication_config(env)
            bridge = FakeBridgeHttp(me=_me_payload(), publish=(504, {"error": "gateway timeout"}))
            publisher = _live_bridge_env(env, bridge)
            capture = CaptureNotifier()
            approved = approve(env, made["approval"]["code"], message_id="unknown-exhaust-approval")
            self.assertEqual(approved["outcome"], "approved")
            task_id = int(env.db.fetchone("SELECT id FROM publication_tasks")["id"])

            worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            worker.publisher = publisher
            worker.notifier = capture
            worker.llm = ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}])
            worker.tick()
            self.assertEqual(env.db.scalar("SELECT state FROM publish_attempts"), "unknown")
            self.assertEqual(len(bridge.posts), 1)

            repair = approve(
                env, made["approval"]["code"], message_id="unknown-exhaust-repair",
                text=f"修复并重试 {made['approval']['code']}",
            )
            self.assertEqual(repair["outcome"], "unknown_requires_reconciliation")

            for attempt in range(3):
                worker.tick()
                task = env.db.fetchone("SELECT * FROM publication_tasks WHERE id=?", (task_id,))
                self.assertEqual(task["state"], "waiting_owner")
                self.assertEqual(env.db.scalar("SELECT state FROM publish_attempts"), "unknown")
                self.assertEqual(len(bridge.posts), 1)
                self.assertTrue(any("暂未确认" in text for text in capture.texts), capture.texts)
                self.assertFalse(any("发布成功" in text for text in capture.texts), capture.texts)
                delay = 30 * (2 ** attempt)
                env.clock.advance(seconds=delay + 1)

            # The final bounded pass observes that the three retries were used
            # and leaves the task in waiting_owner with one owner notice.
            worker.tick()
            task = env.db.fetchone("SELECT * FROM publication_tasks WHERE id=?", (task_id,))
            self.assertEqual(task["state"], "waiting_owner")
            self.assertEqual(env.db.scalar("SELECT state FROM publish_attempts"), "unknown")
            self.assertEqual(len(bridge.posts), 1)
            self.assertEqual(
                int(env.db.scalar(
                    "SELECT COUNT(*) FROM publication_agent_steps "
                    "WHERE task_id=? AND action='reconcile_wait'", (task_id,)
                )),
                3,
            )
            self.assertTrue(any("多次只读核对" in text for text in capture.texts), capture.texts)
            self.assertFalse(any("发布成功" in text for text in capture.texts), capture.texts)
            from bot.services.publication_agent.entry import recover_unknown_reconciliations

            self.assertEqual(recover_unknown_reconciliations(env.db, env.clock), 0)
            active = env.db.scalar(
                "SELECT COUNT(*) FROM jobs WHERE kind='publication_agent' "
                "AND state IN ('pending','running') "
                "AND COALESCE(json_extract(payload_json, '$.reconcile_only'), 0)=1"
            )
            self.assertEqual(int(active), 0)
        finally:
            cleanup(env)

    def test_timeout_unknown_without_owner_message_recovers_after_restart(self):
        env = new_env("round2-gray-unknown")
        try:
            made = make_draft(env)
            _publication_config(env)
            bridge = FakeBridgeHttp(me=_me_payload(), publish=(504, {"error": "gateway timeout"}))
            publisher = _live_bridge_env(env, bridge)
            capture = CaptureNotifier()
            approved = approve(env, made["approval"]["code"], message_id="unknown-approval-1")
            self.assertEqual(approved["outcome"], "approved")
            task = env.db.fetchone("SELECT * FROM publication_tasks")
            task_id = int(task["id"])

            worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            worker.publisher = publisher
            worker.notifier = capture
            worker.llm = ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}])
            worker.tick()

            task = env.db.fetchone("SELECT * FROM publication_tasks WHERE id=?", (task_id,))
            self.assertEqual(task["state"], "waiting_owner", task["state"])
            self.assertEqual(env.db.scalar("SELECT state FROM publish_attempts"), "unknown")
            self.assertEqual(len(bridge.posts), 1)

            attempt_id = int(env.db.scalar("SELECT id FROM publish_attempts"))

            # No owner "repair" command.  The 504 leaves the send unknown; the
            # recovery job must be queued by the publication runtime itself.
            # Only a fresh Worker and the frozen clock advance the case.
            bridge.me = _me_payload(("late-note", made["draft"]["title"], "late-token"))
            env.clock.advance(seconds=31)
            worker2 = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            worker2.publisher = publisher
            worker2.notifier = capture
            worker2.llm = ScriptedLLM([])
            worker2.tick()
            task_row = env.db.fetchone("SELECT * FROM publication_tasks WHERE id=?", (task_id,))
            self.assertEqual(task_row["state"], "published", task_row["state"])
            attempt = env.db.fetchone("SELECT id, state, external_post_id FROM publish_attempts")
            self.assertEqual(int(attempt["id"]), attempt_id)
            self.assertEqual(attempt["state"], "published")
            self.assertEqual(attempt["external_post_id"], "late-note")
            self.assertEqual(len(bridge.posts), 1)
            self.assertTrue(any("发布成功" in text and "late-note" in text for text in capture.texts), capture.texts)
        finally:
            cleanup(env)

    def test_owner_repair_reuses_the_automatic_reconciliation_chain(self):
        env = new_env("round2-gray-one-chain")
        try:
            made = make_draft(env)
            _publication_config(env)
            bridge = FakeBridgeHttp(me=_me_payload(), publish=(504, {"error": "gateway timeout"}))
            publisher = _live_bridge_env(env, bridge)
            capture = CaptureNotifier()
            approved = approve(env, made["approval"]["code"], message_id="one-chain-approval")
            self.assertEqual(approved["outcome"], "approved")
            task_id = int(env.db.fetchone("SELECT id FROM publication_tasks")["id"])

            worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            worker.publisher = publisher
            worker.notifier = capture
            worker.llm = ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}])
            worker.tick()
            active_query = (
                "SELECT id FROM jobs WHERE kind='publication_agent' "
                "AND state IN ('pending','running') "
                "AND COALESCE(json_extract(payload_json, '$.reconcile_only'), 0)=1 "
                "AND CAST(json_extract(payload_json, '$.task_id') AS INTEGER)=? "
                "ORDER BY id DESC LIMIT 1"
            )
            auto_job = env.db.fetchone(active_query, (task_id,))
            self.assertIsNotNone(auto_job)

            repair = approve(
                env, made["approval"]["code"], message_id="one-chain-repair",
                text=f"修复并重试 {made['approval']['code']}",
            )
            self.assertEqual(repair["outcome"], "unknown_requires_reconciliation")
            self.assertEqual(int(repair["detail"]["job_id"]), int(auto_job["id"]))
            self.assertEqual(
                int(env.db.scalar(
                    "SELECT COUNT(*) FROM jobs WHERE kind='publication_agent' "
                    "AND state IN ('pending','running') "
                    "AND COALESCE(json_extract(payload_json, '$.reconcile_only'), 0)=1 "
                    "AND CAST(json_extract(payload_json, '$.task_id') AS INTEGER)=?",
                    (task_id,),
                )),
                1,
            )

            bridge.me = _me_payload(("late-note", made["draft"]["title"], "late-token"))
            env.clock.advance(seconds=31)
            worker2 = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            worker2.publisher = publisher
            worker2.notifier = capture
            worker2.llm = ScriptedLLM([])
            worker2.tick()
            task = env.db.fetchone("SELECT state FROM publication_tasks WHERE id=?", (task_id,))
            self.assertEqual(task["state"], "published", task["state"])
            attempt = env.db.fetchone("SELECT state, external_post_id FROM publish_attempts")
            self.assertEqual(attempt["state"], "published")
            self.assertEqual(attempt["external_post_id"], "late-note")
            self.assertEqual(len(bridge.posts), 1)
            self.assertEqual(sum("发布成功" in text for text in capture.texts), 1)
        finally:
            cleanup(env)

    def test_startup_recovers_missing_reconcile_job_after_crash_window(self):
        env = new_env("round2-gray-crash-window")
        try:
            made = make_draft(env)
            _publication_config(env)
            bridge = FakeBridgeHttp(me=_me_payload(), publish=(504, {"error": "gateway timeout"}))
            publisher = _live_bridge_env(env, bridge)
            capture = CaptureNotifier()
            approved = approve(env, made["approval"]["code"], message_id="crash-window-approval")
            self.assertEqual(approved["outcome"], "approved")
            task_id = int(env.db.fetchone("SELECT id FROM publication_tasks")["id"])

            worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            worker.publisher = publisher
            worker.notifier = capture
            worker.llm = ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}])
            worker.tick()
            attempt_id = int(env.db.scalar("SELECT id FROM publish_attempts"))

            # Simulate the narrow crash window: unknown is durable but no
            # reconciliation job was ever inserted (pre-fix data / process death).
            env.db.execute(
                "DELETE FROM jobs WHERE kind='publication_agent' "
                "AND COALESCE(json_extract(payload_json, '$.reconcile_only'), 0)=1"
            )

            bridge.me = _me_payload(("late-note", made["draft"]["title"], "late-token"))
            from bot.services.publication_agent.entry import recover_unknown_reconciliations

            self.assertEqual(recover_unknown_reconciliations(env.db, env.clock), 1)
            recovered = env.db.fetchone(
                "SELECT id,state FROM jobs WHERE dedupe_key=?",
                (f"publication:wake:{task_id}:unknown-attempt:{attempt_id}:reconcile",),
            )
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered["state"], "pending")
            worker2 = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            worker2.publisher = publisher
            worker2.notifier = capture
            worker2.llm = ScriptedLLM([])
            worker2.tick()
            task = env.db.fetchone("SELECT state FROM publication_tasks WHERE id=?", (task_id,))
            self.assertEqual(task["state"], "published", task["state"])
            attempt = env.db.fetchone("SELECT state, external_post_id FROM publish_attempts")
            self.assertEqual(attempt["state"], "published")
            self.assertEqual(attempt["external_post_id"], "late-note")
            self.assertEqual(len(bridge.posts), 1)
            self.assertTrue(any("发布成功" in text and "late-note" in text for text in capture.texts), capture.texts)
        finally:
            cleanup(env)

    def test_startup_recovery_0_to_1_and_post_once(self):
        env = new_env("round2-startup-0-1")
        try:
            made = make_draft(env)
            _publication_config(env)
            bridge = FakeBridgeHttp(me=_me_payload(), publish=(504, {"error": "gateway timeout"}))
            publisher = _live_bridge_env(env, bridge)
            capture = CaptureNotifier()
            approved = approve(env, made["approval"]["code"], message_id="startup-0-1-approval")
            self.assertEqual(approved["outcome"], "approved")
            task_id = int(env.db.fetchone("SELECT id FROM publication_tasks")["id"])

            worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            worker.publisher = publisher
            worker.notifier = capture
            worker.llm = ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}])
            worker.tick()
            self.assertEqual(len(bridge.posts), 1)
            env.db.execute(
                "DELETE FROM jobs WHERE kind='publication_agent' "
                "AND COALESCE(json_extract(payload_json, '$.reconcile_only'), 0)=1"
            )
            active = env.db.scalar(
                "SELECT COUNT(*) FROM jobs WHERE kind='publication_agent' "
                "AND state IN ('pending','running') "
                "AND COALESCE(json_extract(payload_json, '$.reconcile_only'), 0)=1 "
                "AND CAST(json_extract(payload_json, '$.task_id') AS INTEGER)=?",
                (task_id,),
            )
            self.assertEqual(int(active), 0)
            # The startup decision must come from the saved send attempt, not
            # from a task-row hint that older data may not have written.
            env.db.execute(
                "UPDATE publication_tasks SET last_error_class=NULL WHERE id=?",
                (task_id,),
            )

            from bot.services.publication_agent.entry import recover_unknown_reconciliations

            self.assertEqual(recover_unknown_reconciliations(env.db, env.clock), 1)
            active = env.db.scalar(
                "SELECT COUNT(*) FROM jobs WHERE kind='publication_agent' "
                "AND state='pending' "
                "AND COALESCE(json_extract(payload_json, '$.reconcile_only'), 0)=1 "
                "AND CAST(json_extract(payload_json, '$.task_id') AS INTEGER)=?",
                (task_id,),
            )
            self.assertEqual(int(active), 1)
            self.assertEqual(len(bridge.posts), 1)

            bridge.me = _me_payload(("late-note", made["draft"]["title"], "late-token"))
            worker2 = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            worker2.publisher = publisher
            worker2.notifier = capture
            worker2.llm = ScriptedLLM([])
            env.clock.advance(seconds=31)
            worker2.tick()
            self.assertEqual(env.db.scalar("SELECT state FROM publication_tasks"), "published")
            self.assertEqual(env.db.scalar("SELECT external_post_id FROM publish_attempts"), "late-note")
            self.assertEqual(len(bridge.posts), 1)
        finally:
            cleanup(env)

    def test_startup_recovery_1_to_1_with_delayed_query(self):
        env = new_env("round2-startup-1-1")
        try:
            made = make_draft(env)
            _publication_config(env)
            bridge = FakeBridgeHttp(me=_me_payload(), publish=(504, {"error": "gateway timeout"}))
            publisher = _live_bridge_env(env, bridge)
            capture = CaptureNotifier()
            approved = approve(env, made["approval"]["code"], message_id="startup-1-1-approval")
            self.assertEqual(approved["outcome"], "approved")
            task_id = int(env.db.fetchone("SELECT id FROM publication_tasks")["id"])

            worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            worker.publisher = publisher
            worker.notifier = capture
            worker.llm = ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}])
            worker.tick()
            self.assertEqual(len(bridge.posts), 1)
            active_query = (
                "SELECT id, next_run_at FROM jobs WHERE kind='publication_agent' "
                "AND state IN ('pending','running') "
                "AND COALESCE(json_extract(payload_json, '$.reconcile_only'), 0)=1 "
                "AND CAST(json_extract(payload_json, '$.task_id') AS INTEGER)=? "
                "ORDER BY id"
            )
            before = env.db.fetchall(active_query, (task_id,))
            self.assertEqual(len(before), 1, [dict(row) for row in before])

            from bot.services.publication_agent.entry import recover_unknown_reconciliations

            self.assertEqual(recover_unknown_reconciliations(env.db, env.clock), 0)
            after = env.db.fetchall(active_query, (task_id,))
            self.assertEqual(len(after), 1, [dict(row) for row in after])
            self.assertEqual(int(after[0]["id"]), int(before[0]["id"]))
            self.assertEqual(str(after[0]["next_run_at"]), str(before[0]["next_run_at"]))
            self.assertEqual(len(bridge.posts), 1)
        finally:
            cleanup(env)

    def test_rollback_after_gray_success_does_not_resend(self):
        """Rehearse the line-31 case on an isolated copy of a published DB.

        The copy is made with SQLite's online backup API.  New execution is then
        disabled; replaying the original approval event and running the current
        worker must not create a second task, attempt, or bridge POST.
        """
        env = new_env("round2-rollback")
        try:
            made = make_draft(env)
            _publication_config(env)
            bridge = FakeBridgeHttp(me=_me_payload())
            publisher = _live_bridge_env(env, bridge)
            capture = CaptureNotifier()
            approval_code = made["approval"]["code"]

            approved = approve(
                env, approval_code, message_id="rollback-approval-1",
            )
            self.assertEqual(approved["outcome"], "approved")
            task = env.db.fetchone("SELECT * FROM publication_tasks")
            self.assertIsNotNone(task)
            task_id = int(task["id"])

            worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            worker.publisher = publisher
            worker.notifier = capture
            worker.llm = ScriptedLLM([
                {"action": "verify_assets", "args": {}},
                {"action": "publish_approved_artifact", "args": {}},
            ])
            worker.tick()
            published = env.db.fetchone("SELECT state FROM publication_tasks WHERE id=?", (task_id,))
            self.assertEqual(published["state"], "published")
            self.assertEqual(len(bridge.posts), 1)
            attempts_before = int(env.db.scalar("SELECT COUNT(*) FROM publish_attempts") or 0)

            # The rollback copy is a consistent snapshot with the gray success
            # already committed.  The live DB is never touched.
            copy_path = Path("var/tests/rollback_after_gray.sqlite3")
            env.db.backup_to(copy_path)
            rollback_db = Database(copy_path)
            rollback_db.init()
            self.assertEqual(
                str(rollback_db.scalar("PRAGMA integrity_check")), "ok",
            )
            env.config.data["publication_agent"]["enabled"] = False

            replay = handle_inbound_message(
                rollback_db,
                env.config,
                env.clock,
                message_id="rollback-approval-1",
                sender_id="owner-open-id",
                chat_id="owner-open-id",
                chat_type="p2p",
                text=f"批准 {approval_code}",
                channel="test",
            )
            self.assertIn(replay.get("outcome"), {"approved", "already_published", "approval_not_pending"})

            restarted = Worker(
                env.config, clock=env.clock, db=rollback_db, http=env.http,
            )
            restarted.publisher = publisher
            restarted.notifier = capture
            restarted.llm = ScriptedLLM([])
            restarted.tick()

            self.assertEqual(len(bridge.posts), 1)
            self.assertEqual(
                int(rollback_db.scalar("SELECT COUNT(*) FROM publication_tasks") or 0), 1,
            )
            self.assertEqual(
                int(rollback_db.scalar("SELECT COUNT(*) FROM publish_attempts") or 0),
                attempts_before,
            )
            self.assertEqual(
                rollback_db.scalar("SELECT state FROM publication_tasks WHERE id=?", (task_id,)),
                "published",
            )
            task_state = str(rollback_db.scalar(
                "SELECT state FROM publication_tasks WHERE id=?", (task_id,)
            ))
            approval_state = str(rollback_db.scalar(
                "SELECT status FROM approvals WHERE id=?", (int(made["approval"]["id"]),)
            ))
            self.assertEqual(task_state, "published")
            self.assertEqual(approval_state, "published")
            Path("var/tests/rollback_after_gray.json").write_text(
                json.dumps({
                    "source_db": str(env.db.path),
                    "rollback_copy": str(copy_path),
                    "approval_code": approval_code,
                    "task_id": task_id,
                    "attempts": attempts_before,
                    "bridge_posts": len(bridge.posts),
                    "replay_outcome": replay.get("outcome"),
                    "task_state": task_state,
                    "approval_state": approval_state,
                    "integrity": str(rollback_db.scalar("PRAGMA integrity_check")),
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        finally:
            cleanup(env)


if __name__ == "__main__":
    unittest.main()

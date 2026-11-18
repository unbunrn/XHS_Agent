"""Counterexample tests for recovery budget, asset backup and work identity."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from bot.db import json_loads
from bot.services.approvals import handle_inbound_message
from bot.services.publication_agent.diagnostics import (
    ensure_asset_manifest,
    redact_text,
    restore_from_manifest,
)
from bot.services.publish import _attempts_today
from bot.services.research import _topic_source_name, sync_sources
from bot.timeutil import to_iso
from bot.services.publication_agent.entry import handle_command, wake_task
from bot.services.publication_agent.live import LivePublicationRuntime
from bot.services.publication_agent.runner import PublicationRunner
from bot.services.publication_agent.store import PublicationStore
from bot.services.publish import validate_publish
from bot.worker import Worker
from tests.helpers import approve, cleanup, make_draft, new_env
from tests.test_publication_agent import FakePublisher, ScriptedLLM, _publication_config


class BudgetAndWakeTests(TestCase):
    def _env(self, prefix):
        env = new_env(prefix)
        made = make_draft(env)
        _publication_config(env)
        approve(env, made["approval"]["code"])
        task = env.db.fetchone("SELECT * FROM publication_tasks")
        return env, made, int(task["id"])

    def test_batch_budget_stops_before_the_next_transport(self):
        env, made, task_id = self._env("pub-r4-budget")
        try:
            env.config.data["publication_agent"]["max_model_calls"] = 1
            publisher = FakePublisher()
            runner = PublicationRunner(
                env.db, env.config, env.clock,
                ScriptedLLM([
                    {"action": "inspect_task", "args": {}},
                    {"action": "inspect_task", "args": {}},
                ]),
                LivePublicationRuntime(env.db, env.config, env.clock, publisher=publisher),
            )
            result = runner.run(task_id)
            self.assertEqual(result.state, "waiting_owner")
            self.assertEqual(publisher.calls, 0)
            self.assertEqual(env.db.scalar(
                "SELECT COUNT(*) FROM publication_agent_steps WHERE task_id=? AND action='model_attempt'",
                (task_id,),
            ), 1)
            self.assertIn("budget", env.db.scalar(
                "SELECT wait_reason FROM publication_tasks WHERE id=?", (task_id,)
            ))
        finally:
            cleanup(env)

    def test_exhausted_batch_can_be_resumed_by_a_new_owner_message(self):
        env, made, task_id = self._env("pub-r4-resume")
        try:
            env.config.data["publication_agent"]["max_model_calls"] = 1
            publisher = FakePublisher()
            runtime = LivePublicationRuntime(env.db, env.config, env.clock, publisher=publisher)
            first = PublicationRunner(
                env.db, env.config, env.clock,
                ScriptedLLM([{"action": "inspect_task", "args": {}}]), runtime,
            ).run(task_id)
            self.assertEqual(first.state, "waiting_owner")
            result = handle_command(
                env.db, env.config, env.clock,
                f"继续发布 {made['approval']['code']}", event_key="resume-msg-1",
            )
            self.assertEqual(result["outcome"], "resumed")
            second = PublicationRunner(
                env.db, env.config, env.clock,
                ScriptedLLM([{"action": "inspect_task", "args": {}}]), runtime,
            ).run(task_id)
            self.assertIn(second.state, {"waiting_owner", "diagnosing", "ready"})
            attempts = env.db.scalar(
                "SELECT COUNT(*) FROM publication_agent_steps WHERE task_id=? AND action='model_attempt'",
                (task_id,),
            )
            self.assertEqual(attempts, 2)
            self.assertGreaterEqual(env.db.scalar(
                "SELECT COUNT(*) FROM publication_agent_steps WHERE task_id=? AND action='batch_start'",
                (task_id,),
            ), 2)
        finally:
            cleanup(env)

    def test_wake_dedupes_by_inbound_message_not_generation(self):
        env, made, task_id = self._env("pub-r4-wake")
        try:
            first = wake_task(env.db, env.clock, task_id, reason="resume", event_key="msg-1")
            replay = wake_task(env.db, env.clock, task_id, reason="resume", event_key="msg-1")
            new = wake_task(env.db, env.clock, task_id, reason="resume", event_key="msg-2")
            self.assertEqual(first, replay)
            self.assertNotEqual(first, new)
            jobs = env.db.fetchall("SELECT payload_json FROM jobs WHERE id IN (?,?)", (first, new))
            self.assertEqual(len(jobs), 2)
            self.assertTrue(all(json.loads(j["payload_json"])["task_id"] == task_id for j in jobs))
        finally:
            cleanup(env)

    def test_diagnose_command_wakes_a_read_only_job(self):
        env, made, task_id = self._env("pub-r4-diagnose")
        try:
            result = handle_command(
                env.db, env.config, env.clock,
                f"检查原因 {made['approval']['code']}", event_key="msg-diagnose",
            )
            self.assertTrue(result["ok"], result)
            job = env.db.fetchone("SELECT * FROM jobs WHERE id=?", (int(result["job_id"]),))
            payload = json.loads(job["payload_json"])
            self.assertTrue(payload["diagnose_only"])
            self.assertEqual(payload["reason"], "diagnose")
        finally:
            cleanup(env)

    def test_worker_crash_is_retried_not_marked_done(self):
        env, made, task_id = self._env("pub-r4-worker")
        try:
            job = env.db.fetchone(
                "SELECT * FROM jobs WHERE kind='publication_agent' ORDER BY id DESC LIMIT 1"
            )
            worker = Worker(env.config, clock=env.clock, db=env.db)
            worker.publisher = FakePublisher()
            with patch(
                "bot.services.publication_agent.runner.PublicationRunner.run",
                side_effect=RuntimeError("simulated crash"),
            ):
                result = worker._handle_publication_agent(job)
            self.assertTrue(result.get("retry"), result)
            self.assertIn("simulated crash", result.get("error", ""))
        finally:
            cleanup(env)


class AssetManifestTests(TestCase):
    def _env(self, prefix):
        env = new_env(prefix)
        made = make_draft(env)
        _publication_config(env)
        approve(env, made["approval"]["code"])
        task = env.db.fetchone("SELECT * FROM publication_tasks")
        package = json_loads(task["approved_package_json"], {})
        return env, made, task, package

    def test_manifest_backup_and_restore_use_program_ids(self):
        env, made, task, package = self._env("pub-r5-restore")
        try:
            roots = [str(env.config.resolve_path("var/drafts"))]
            backup = env.root / "var" / "backups" / "publication_assets"
            report = ensure_asset_manifest(package, target_roots=roots, backup_root=backup)
            self.assertTrue(report["ok"], report)
            first = package["images"][0]
            target = Path(first["path"])
            expected = target.read_bytes()
            target.unlink()
            restored = restore_from_manifest(
                package, [str(first["ordinal"])], target_roots=roots, backup_root=backup
            )
            self.assertTrue(restored["ok"], restored)
            self.assertEqual(target.read_bytes(), expected)
        finally:
            cleanup(env)

    def test_missing_manifest_waits_instead_of_redrawing(self):
        env, made, task, package = self._env("pub-r5-missing")
        try:
            backup = env.root / "var" / "backups" / "publication_assets"
            shutil.rmtree(backup, ignore_errors=True)
            report = restore_from_manifest(
                package, ["1"],
                target_roots=[str(env.config.resolve_path("var/drafts"))],
                backup_root=backup,
            )
            self.assertFalse(report["ok"])
            self.assertEqual(report["reason"], "no_backup_manifest")
        finally:
            cleanup(env)

    def test_restored_bytes_still_pass_the_publish_artifact_check(self):
        env, made, task, package = self._env("pub-r5-publish")
        try:
            roots = [str(env.config.resolve_path("var/drafts"))]
            backup = env.root / "var" / "backups" / "publication_assets"
            self.assertTrue(ensure_asset_manifest(package, target_roots=roots, backup_root=backup)["ok"])
            first = package["images"][0]
            target = Path(first["path"])
            target.unlink()
            self.assertTrue(restore_from_manifest(
                package, [str(first["ordinal"])], target_roots=roots, backup_root=backup,
            )["ok"])
            approval = env.db.fetchone("SELECT * FROM approvals WHERE id=?", (made["approval"]["id"],))
            ok, reason, _, _ = validate_publish(
                env.db, env.config, env.clock, approval, FakePublisher(), task_id=int(task["id"]),
            )
            self.assertTrue(ok, reason)
        finally:
            cleanup(env)

    def test_asset_outside_approved_root_is_not_backed_up(self):
        env, made, task, package = self._env("pub-r5-root")
        try:
            report = ensure_asset_manifest(
                package,
                target_roots=[str(env.root / "not-the-drafts-root")],
                backup_root=env.root / "var" / "backups" / "publication_assets",
            )
            self.assertFalse(report["ok"])
            self.assertTrue(any(not i["available"] for i in report["images"]))
        finally:
            cleanup(env)


class WorkIdentityAndPackageTests(TestCase):
    def _env(self, prefix):
        env = new_env(prefix)
        made = make_draft(env)
        _publication_config(env)
        approve(env, made["approval"]["code"])
        task = env.db.fetchone("SELECT * FROM publication_tasks")
        return env, made, task

    def test_different_approval_cannot_create_a_parallel_send(self):
        env, made, task = self._env("pub-r6-work")
        try:
            approval = env.db.fetchone("SELECT * FROM approvals WHERE id=?", (made["approval"]["id"],))
            draft = env.db.fetchone("SELECT * FROM drafts WHERE id=?", (made["draft"]["id"],))
            images = [dict(r) for r in env.db.fetchall(
                "SELECT ordinal,path,sha256 FROM draft_images WHERE draft_id=? ORDER BY ordinal",
                (made["draft"]["id"],),
            )]
            second = dict(approval)
            second["id"] = 9999
            second["code"] = "XH-OTHER1"
            store = PublicationStore(env.db, env.clock)
            with self.assertRaises(ValueError):
                store.create_task(
                    approval=second, draft=draft, images=images,
   
                    account_id="test-account",
                )
        finally:
            cleanup(env)

    def test_frozen_package_change_is_rejected_at_publish(self):
        env, made, task = self._env("pub-r6-package")
        try:
            package = json_loads(task["approved_package_json"], {})
            package["title"] = "tampered title"
            env.db.execute(
                "UPDATE publication_tasks SET approved_package_json=? WHERE id=?",
                (json.dumps(package, ensure_ascii=False), task["id"]),
            )
            approval = env.db.fetchone("SELECT * FROM approvals WHERE id=?", (made["approval"]["id"],))
            ok, reason, _, _ = validate_publish(
                env.db, env.config, env.clock, approval, FakePublisher(), task_id=int(task["id"]),
            )
            self.assertFalse(ok)
            self.assertIn("frozen package", reason)
        finally:
            cleanup(env)

    def test_frozen_package_visibility_and_image_order_are_checked(self):
        env, made, task = self._env("pub-r6-order")
        try:
            package = json_loads(task["approved_package_json"], {})
            package["visibility"] = "private"
            package["images"] = list(reversed(package.get("images") or []))
            env.db.execute(
                "UPDATE publication_tasks SET approved_package_json=? WHERE id=?",
                (json.dumps(package, ensure_ascii=False), task["id"]),
            )
            approval = env.db.fetchone("SELECT * FROM approvals WHERE id=?", (made["approval"]["id"],))
            ok, reason, _, _ = validate_publish(
                env.db, env.config, env.clock, approval, FakePublisher(), task_id=int(task["id"]),
            )
            self.assertFalse(ok)
            self.assertIn("frozen package", reason)
        finally:
            cleanup(env)

    def test_simulated_result_does_not_become_published(self):
        env, made, task = self._env("pub-r6-simulated")
        try:
            publisher = FakePublisher(
                outcome=type("Outcome", (), {
                    "state": "simulated", "detail": "mock", "external_post_id": None,
                    "external_url": None, "receipt": {}, "failure_class": "", "retryable": False,
                })()
            )
            runner = PublicationRunner(
                env.db, env.config, env.clock,
                ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}]),
                LivePublicationRuntime(env.db, env.config, env.clock, publisher=publisher),
            )
            result = runner.run(int(task["id"]))
            self.assertIn(result.state, {"simulated", "waiting_owner", "failed"})
            self.assertEqual(
                env.db.scalar("SELECT state FROM publication_tasks WHERE id=?", (task["id"],)),
                "simulated",
            )
        finally:
            cleanup(env)


class NotificationAndOwnerRequestTests(TestCase):
    def _env(self, prefix):
        env = new_env(prefix)
        made = make_draft(env)
        _publication_config(env)
        approve(env, made["approval"]["code"])
        task = env.db.fetchone("SELECT * FROM publication_tasks")
        return env, made, task

    def test_model_text_is_redacted_and_cannot_claim_success(self):
        self.assertEqual(
            redact_text("see https://example.com/a?token=abc and api_key=xyz"),
            "see https://example.com/a and api_key=<redacted>",
        )
        env, made, task = self._env("pub-r4-redact")
        try:
            store = PublicationStore(env.db, env.clock)
            from bot.services.publication_agent.tools import PublicationContext, PublicationTools
            class Runtime:
                def __init__(self):
                    self.messages = []
                def notify(self, task, text):
                    self.messages.append(text)
            runtime = Runtime()
            store.update(int(task["id"]), state="waiting_owner", wait_reason="need owner")
            ctx = PublicationContext(
                store=store, task=store.get(task_id=int(task["id"])), package={}, runtime=runtime,
                config=env.config, clock=env.clock,
            )
            tools = PublicationTools(runtime)
            result = tools.execute("notify_owner", ctx, {"text": "已发布成功 https://x.test/?token=s"})
            self.assertTrue(result["ok"], result)
            self.assertEqual(len(runtime.messages), 1)
            self.assertNotIn("已发布成功", runtime.messages[0])
            self.assertIn("等待", runtime.messages[0])
        finally:
            cleanup(env)

    def test_owner_request_closes_when_task_resumes_and_expires_when_due(self):
        env, made, task = self._env("pub-r4-owner")
        try:
            store = PublicationStore(env.db, env.clock)
            req = store.add_owner_request(int(task["id"]), question="登录后再继续", kind="login")
            self.assertIsNotNone(store.open_owner_request(int(task["id"])))
            store.update(int(task["id"]), state="ready")
            self.assertIsNone(store.open_owner_request(int(task["id"])))
            self.assertEqual(
                env.db.scalar("SELECT state FROM publication_owner_requests WHERE id=?", (req,)),
                "closed",
            )
            expired = store.add_owner_request(
                int(task["id"]), question="过期", kind="login",
                expires_at=to_iso(env.clock.now()),
            )
            self.assertIsNone(store.open_owner_request(int(task["id"])))
            self.assertEqual(
                env.db.scalar("SELECT state FROM publication_owner_requests WHERE id=?", (expired,)),
                "expired",
            )
        finally:
            cleanup(env)


class SourceOwnershipAndQuotaTests(TestCase):
    def _env(self, prefix):
        env = new_env(prefix)
        made = make_draft(env)
        _publication_config(env)
        approve(env, made["approval"]["code"])
        task = env.db.fetchone("SELECT * FROM publication_tasks")
        return env, made, task

    def test_v2_commissioned_source_survives_config_sync(self):
        env, made, task = self._env("pub-r6-source")
        try:
            run_key = "run-source-1"
            now = to_iso(env.clock.now())
            env.db.execute(
                "INSERT INTO creator_agent_runs(run_key,account_id,origin,status,brief_json,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (run_key, "test-account", "fixture", "drafting", "{}", now, now),
            )
            code = "v2-" + run_key
            name = _topic_source_name(code)
            url = "deepseek-search://topic/" + code
            env.db.execute(
                "INSERT INTO sources(name,url,kind,trust_tier,enabled,created_at) VALUES (?,?,?,?,?,?)",
                (name, url, "deepseek_search", 2, 1, now),
            )
            sync_sources(env.db, env.config)
            row = env.db.fetchone("SELECT enabled FROM sources WHERE name=?", (name,))
            self.assertEqual(int(row["enabled"]), 1)
        finally:
            cleanup(env)

    def test_safe_retry_does_not_consume_another_daily_slot(self):
        env, made, task = self._env("pub-r6-quota")
        try:
            now = to_iso(env.clock.now())
            for attempt_no in (1, 2):
                env.db.execute(
                    "INSERT INTO publish_attempts(approval_id,draft_id,dedupe_key,platform,account_id,mode,state,"
                    "created_at,updated_at,publication_task_id,attempt_no) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (made["approval"]["id"], made["draft"]["id"], f"quota-{attempt_no}",
                     "xiaohongshu", "test-account", "mock", "failed", now, now,
                     int(task["id"]), attempt_no),
                )
            self.assertEqual(_attempts_today(env.db, env.config, env.clock), 1)
        finally:
            cleanup(env)

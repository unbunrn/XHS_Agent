"""P0 business-result loop: durable results, idempotent notices, read-only reconcile."""

from __future__ import annotations

import json
import unittest

from bot.adapters.notify import FakeNotifier
from bot.adapters.publishers import MockPublisher, Publisher
from bot.adapters.types import PublishOutcome
from bot.services import approvals, notifications, publish
from bot.services.publication_results import latest_for_approval, record_result
from bot.services.reconcile_readonly import (
    NoteEvidence,
    ReconcileError,
    UnavailableEvidenceProvider,
    read_only_reconcile,
)
from bot.worker import Worker
from tests import helpers


class UnknownPublisher(Publisher):
    mode = "mock"

    def __init__(self, config, clock):
        self.config = config
        self.clock = clock
        self.calls = 0

    def publish(self, request):
        self.calls += 1
        return PublishOutcome(state="unknown", detail="bridge accepted but returned no receipt")

    def health(self):
        return {"ok": False, "mode": "mock", "detail": "test unknown publisher"}


class PublicationResultTests(unittest.TestCase):
    def setUp(self):
        self.env = helpers.new_env("p0-results")

    def tearDown(self):
        helpers.cleanup(self.env)

    def _pending(self):
        return helpers.make_draft(self.env)

    def test_gate_block_is_persisted_and_notified_without_attempt(self):
        pack = self._pending()
        before = self.env.db.scalar("SELECT COUNT(*) FROM publish_attempts")
        result = publish.publish_approval(self.env.db, self.env.config, self.env.clock, pack["approval_id"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(int(self.env.db.scalar("SELECT COUNT(*) FROM publish_attempts")), int(before))
        rows = self.env.db.fetchall("SELECT * FROM publish_results")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "blocked")
        self.assertIsNone(rows[0]["attempt_id"])
        note = self.env.db.fetchone("SELECT * FROM notifications WHERE id=?", (rows[0]["notification_id"],))
        self.assertIsNotNone(note)
        self.assertEqual(note["kind"], "outcome")
        self.assertIn("发布前被拦截", note["payload_json"])
        self.assertIn("未提交", note["payload_json"])

    def test_simulated_success_result_and_duplicate_notice_are_idempotent(self):
        pack = self._pending()
        helpers.approve(self.env, pack["approval_code"])
        publisher = MockPublisher(self.env.config, self.env.clock)
        first = publish.publish_approval(
            self.env.db, self.env.config, self.env.clock, pack["approval_id"], publisher=publisher
        )
        self.assertTrue(first["ok"])
        self.assertEqual(first["state"], "simulated")
        self.assertIsNotNone(first["result_id"])
        self.assertTrue(first["notification_queued"])
        result = latest_for_approval(self.env.db, pack["approval_id"])
        self.assertEqual(result["state"], "simulated")
        self.assertEqual(result["state"], "simulated")
        notices_before = self.env.db.scalar("SELECT COUNT(*) FROM notifications")
        second = publish.publish_approval(
            self.env.db, self.env.config, self.env.clock, pack["approval_id"], publisher=publisher
        )
        self.assertEqual(second["state"], "already_attempted")
        self.assertEqual(second["business_state"], "simulated")
        self.assertEqual(second["result_id"], first["result_id"])
        self.assertFalse(second["notification_queued"])
        self.assertEqual(int(self.env.db.scalar("SELECT COUNT(*) FROM notifications")), int(notices_before))
        self.assertEqual(int(self.env.db.scalar("SELECT COUNT(*) FROM publish_attempts")), 1)

    def test_failed_result_is_durable_and_distinct_from_unknown(self):
        pack = self._pending()
        helpers.approve(self.env, pack["approval_code"])

        class FailingPublisher(MockPublisher):
            def publish(self, request):
                return PublishOutcome(state="failed", detail="4xx definite rejection")

        result = publish.publish_approval(
            self.env.db, self.env.config, self.env.clock, pack["approval_id"],
            publisher=FailingPublisher(self.env.config, self.env.clock),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "failed")
        stored = latest_for_approval(self.env.db, pack["approval_id"])
        self.assertEqual(stored["state"], "failed")
        self.assertEqual(self.env.db.scalar("SELECT status FROM drafts WHERE id=?", (pack["draft"]["id"],)), "failed")

    def test_recovery_records_unknown_and_never_resends(self):
        pack = self._pending()
        helpers.approve(self.env, pack["approval_code"])
        publisher = MockPublisher(self.env.config, self.env.clock)
        publish.claim_publish(self.env.db, self.env.config, self.env.clock, pack["approval_id"], publisher)
        recovered = publish.recover_interrupted_publishes(
            self.env.db, self.env.clock, self.env.config
        )
        self.assertEqual(recovered, 1)
        attempt = self.env.db.fetchone("SELECT * FROM publish_attempts WHERE approval_id=?", (pack["approval_id"],))
        self.assertEqual(attempt["state"], "unknown")
        result = latest_for_approval(self.env.db, pack["approval_id"])
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["reason_code"], "restart_while_publishing")
        self.assertIn("绝不自动重发", result["detail"])
        self.assertEqual(
            int(self.env.db.scalar("SELECT COUNT(*) FROM publish_attempts WHERE approval_id=?", (pack["approval_id"],))),
            1,
        )

    def test_terminal_result_cannot_be_downgraded(self):
        pack = self._pending()
        helpers.approve(self.env, pack["approval_code"])
        first = publish.publish_approval(
            self.env.db, self.env.config, self.env.clock, pack["approval_id"],
            publisher=MockPublisher(self.env.config, self.env.clock),
        )
        self.assertEqual(first["state"], "simulated")
        attempt = self.env.db.fetchone(
            "SELECT * FROM publish_attempts WHERE approval_id=?", (pack["approval_id"],)
        )
        fresh = approvals.get_approval(self.env.db, approval_id=pack["approval_id"])
        stale = record_result(
            self.env.db, self.env.config, self.env.clock,
            approval=fresh, state="unknown", stage="reconcile", attempt=attempt,
            reason_code="stale_async_write", detail="stale overwrite attempt",
        )
        self.assertEqual(stale["state"], "simulated")
        self.assertTrue(stale.get("transition_blocked"))
        self.assertEqual(latest_for_approval(self.env.db, pack["approval_id"])["state"], "simulated")

    def test_gate_detail_is_chinese_and_notice_is_multiline(self):
        pack = self._pending()
        result = publish.publish_approval(self.env.db, self.env.config, self.env.clock, pack["approval_id"])
        self.assertEqual(result["state"], "blocked")
        self.assertIn("尚未批准", result["detail"])
        row = self.env.db.fetchone("SELECT * FROM publish_results")
        self.assertIn("尚未批准", row["detail"])
        note = self.env.db.fetchone("SELECT * FROM notifications WHERE id=?", (row["notification_id"],))
        import json
        payload = json.loads(note["payload_json"])
        text = payload["text"]
        self.assertIn("\n", text)
        self.assertIn("下一步", text)

    def test_unknown_notice_is_delivered_and_replay_does_not_send_twice(self):
        pack = self._pending()
        helpers.approve(self.env, pack["approval_code"])
        publish.publish_approval(
            self.env.db, self.env.config, self.env.clock, pack["approval_id"],
            publisher=UnknownPublisher(self.env.config, self.env.clock),
        )
        notifier = FakeNotifier(self.env.config, self.env.clock)
        first = notifications.deliver_pending(self.env.db, self.env.config, self.env.clock, notifier)
        outcome_ids = {
            int(row["id"])
            for row in self.env.db.fetchall(
                "SELECT id FROM notifications WHERE kind='outcome' AND dedupe_key LIKE 'publish-result:%'"
            )
        }
        outcome_deliveries = [item for item in first if int(item["id"]) in outcome_ids]
        self.assertEqual(len(outcome_deliveries), 1)
        self.assertEqual(outcome_deliveries[0]["state"], "sent")
        sent_before = int(self.env.db.scalar("SELECT COUNT(*) FROM notifications WHERE state='sent'"))
        second = notifications.deliver_pending(self.env.db, self.env.config, self.env.clock, notifier)
        self.assertEqual(second, [])
        self.assertEqual(int(self.env.db.scalar("SELECT COUNT(*) FROM notifications WHERE state='sent'")), sent_before)
        outcome_row = self.env.db.fetchone(
            "SELECT payload_json FROM notifications WHERE id=?",
            (outcome_deliveries[0]["id"],),
        )
        message = outcome_row["payload_json"]
        self.assertIn("结果未知", message)
        self.assertIn("不会自动重试", message)


class ReadOnlyReconcileTests(unittest.TestCase):
    def setUp(self):
        self.env = helpers.new_env("p0-readonly-reconcile")
        pack = helpers.make_draft(self.env)
        self.draft = pack["draft"]
        self.approval = pack["approval"]
        helpers.approve(self.env, pack["approval_code"])
        self.publisher = UnknownPublisher(self.env.config, self.env.clock)
        self.published = publish.publish_approval(
            self.env.db, self.env.config, self.env.clock, pack["approval_id"], publisher=self.publisher
        )
        assert self.published["state"] == "unknown"
        self.attempt = self.env.db.fetchone(
            "SELECT * FROM publish_attempts WHERE approval_id=?", (pack["approval_id"],)
        )

    def tearDown(self):
        helpers.cleanup(self.env)

    def _note(self, *, note_id="note-1", title=None, body=None, when=None, account=None):
        return NoteEvidence(
            external_post_id=note_id,
            external_url="https://www.xiaohongshu.com/discovery/item/" + note_id,
            title=title if title is not None else self.draft["title"],
            body=body if body is not None else self.draft["body"],
            published_at=when if when is not None else self.attempt["started_at"],
            account_id=account if account is not None else self.approval["account_id"],
        )

    def test_unique_strong_match_publishes_without_another_submit(self):
        note = self._note()

        class Provider:
            name = "recorded-fixture"

            def lookup(self, *, attempt, approval, draft):
                return [note]
        result = read_only_reconcile(
            self.env.db, self.env.config, self.env.clock,
            approval_code=self.approval["code"], provider=Provider(),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "published")
        self.assertEqual(self.publisher.calls, 1)
        attempt = self.env.db.fetchone("SELECT * FROM publish_attempts WHERE id=?", (self.attempt["id"],))
        self.assertEqual(attempt["state"], "published")
        self.assertEqual(attempt["external_post_id"], "note-1")
        checks = self.env.db.fetchall("SELECT * FROM publish_reconcile_checks")
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["outcome"], "published")
        self.assertEqual(
            int(self.env.db.scalar("SELECT COUNT(*) FROM publish_attempts WHERE approval_id=?", (self.approval["id"],))),
            1,
        )

    def test_same_title_and_time_without_owner_or_body_is_not_a_match(self):
        class Provider:
            name = "fixture-weak"

            def lookup(self, *, attempt, approval, draft):
                return [NoteEvidence(
                    external_post_id="unrelated",
                    external_url="https://www.xiaohongshu.com/discovery/item/unrelated",
                    title=draft["title"],
                    body="",
                    published_at=attempt["started_at"],
                    account_id=None,
                )]

        result = read_only_reconcile(
            self.env.db, self.env.config, self.env.clock,
            attempt_id=self.attempt["id"], provider=Provider(),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["reason_code"], "no_unique_match")
        self.assertEqual(
            self.env.db.scalar("SELECT state FROM publish_attempts WHERE id=?", (self.attempt["id"],)),
            "unknown",
        )

    def test_wrong_account_or_different_body_cannot_confirm(self):
        class WrongAccount:
            name = "fixture-wrong-account"

            def lookup(self, *, attempt, approval, draft):
                return [NoteEvidence(
                    external_post_id="other-account",
                    external_url="https://www.xiaohongshu.com/discovery/item/other-account",
                    title=draft["title"], body=draft["body"],
                    published_at=attempt["started_at"], account_id="someone-else",
                )]

        wrong = read_only_reconcile(
            self.env.db, self.env.config, self.env.clock,
            attempt_id=self.attempt["id"], provider=WrongAccount(),
        )
        self.assertEqual(wrong["state"], "unknown")
        self.assertEqual(wrong["reason_code"], "no_unique_match")

        self.env.clock.advance(minutes=11)
        class DifferentBody:
            name = "fixture-different-body"

            def lookup(self, *, attempt, approval, draft):
                return [NoteEvidence(
                    external_post_id="different-body",
                    external_url="https://www.xiaohongshu.com/discovery/item/different-body",
                    title=draft["title"], body="完全不同的正文内容，不能证明是同一条发布。",
                    published_at=attempt["started_at"], account_id=approval["account_id"],
                )]

        different = read_only_reconcile(
            self.env.db, self.env.config, self.env.clock,
            attempt_id=self.attempt["id"], provider=DifferentBody(),
        )
        self.assertEqual(different["state"], "unknown")

    def test_strong_body_and_owner_without_time_can_confirm(self):
        class Provider:
            name = "fixture-strong-body"

            def lookup(self, *, attempt, approval, draft):
                return [NoteEvidence(
                    external_post_id="strong-body",
                    external_url="https://www.xiaohongshu.com/discovery/item/strong-body",
                    title=draft["title"], body=draft["body"], published_at=None,
                    account_id=approval["account_id"],
                )]

        result = read_only_reconcile(
            self.env.db, self.env.config, self.env.clock,
            attempt_id=self.attempt["id"], provider=Provider(),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "published")

    def test_manual_publish_during_lookup_is_not_downgraded(self):
        before = int(self.env.db.scalar("SELECT COUNT(*) FROM notifications"))
        env = self.env

        class Provider:
            name = "fixture-manual-race"

            def lookup(self, *, attempt, approval, draft):
                publish.reconcile_unknown(
                    env.db, env.config, env.clock,
                    approval_code=str(approval["code"]),
                    resolution="confirmed_published",
                    note="manual receipt arrived while read-only lookup was in flight",
                    external_post_id="verified-fixture",
                    external_url="https://www.xiaohongshu.com/discovery/item/verified-fixture",
                    actor="test-manual",
                )
                return []

        result = read_only_reconcile(
            self.env.db, self.env.config, self.env.clock,
            attempt_id=self.attempt["id"], provider=Provider(),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "published")
        attempt = self.env.db.fetchone("SELECT * FROM publish_attempts WHERE id=?", (self.attempt["id"],))
        self.assertEqual(attempt["state"], "published")
        self.assertEqual(attempt["external_post_id"], "verified-fixture")
        self.assertEqual(latest_for_approval(self.env.db, self.approval["id"])["state"], "published")
        after = int(self.env.db.scalar("SELECT COUNT(*) FROM notifications"))
        self.assertLessEqual(after, before + 1)

    def test_readonly_refuses_active_publishing_attempt(self):
        # The approval already has an unknown attempt from setUp. Mark that
        # attempt as actively publishing to represent the in-flight worker.
        self.env.db.execute(
            "UPDATE publish_attempts SET state='publishing' WHERE id=?",
            (self.attempt["id"],),
        )
        with self.assertRaises(ReconcileError):
            read_only_reconcile(
                self.env.db, self.env.config, self.env.clock,
                attempt_id=self.attempt["id"], provider=UnavailableEvidenceProvider(),
            )

    def test_unavailable_provider_keeps_unknown_and_never_proves_failure(self):
        result = read_only_reconcile(
            self.env.db, self.env.config, self.env.clock,
            approval_code=self.approval["code"], provider=UnavailableEvidenceProvider(),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["reason_code"], "lookup_unavailable")
        self.assertTrue(result["checked"])
        attempt = self.env.db.fetchone("SELECT * FROM publish_attempts WHERE id=?", (self.attempt["id"],))
        self.assertEqual(attempt["state"], "unknown")
        checks = self.env.db.fetchall("SELECT * FROM publish_reconcile_checks")
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["reason_code"], "lookup_unavailable")

    def test_same_title_multiple_matches_stays_unknown(self):
        def make_note(note_id):
            return NoteEvidence(
                external_post_id=note_id,
                external_url="https://www.xiaohongshu.com/discovery/item/" + note_id,
                title=self.draft["title"],
                body=self.draft["body"],
                published_at=self.attempt["started_at"],
                account_id=self.approval["account_id"],
            )

        class Provider:
            name = "recorded-fixture"

            def lookup(self, *, attempt, approval, draft):
                return [make_note("note-a"), make_note("note-b")]
        result = read_only_reconcile(
            self.env.db, self.env.config, self.env.clock,
            attempt_id=self.attempt["id"], provider=Provider(),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "ambiguous_multiple_matches")
        attempt = self.env.db.fetchone("SELECT * FROM publish_attempts WHERE id=?", (self.attempt["id"],))
        self.assertEqual(attempt["state"], "unknown")
        self.assertEqual(int(self.env.db.scalar("SELECT COUNT(*) FROM publish_attempts")), 1)

    def test_no_match_or_provider_error_never_means_not_published(self):
        class EmptyProvider:
            name = "fixture-empty"

            def lookup(self, *, attempt, approval, draft):
                return []

        result = read_only_reconcile(
            self.env.db, self.env.config, self.env.clock,
            attempt_id=self.attempt["id"], provider=EmptyProvider(),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "no_unique_match")
        self.assertIn("不能证明未发布", result["detail"])

        # advance past the per-attempt interval for the next check
        self.env.clock.advance(minutes=11)

        class BrokenProvider:
            name = "fixture-broken"

            def lookup(self, *, attempt, approval, draft):
                raise RuntimeError("bridge offline")

        second = read_only_reconcile(
            self.env.db, self.env.config, self.env.clock,
            attempt_id=self.attempt["id"], provider=BrokenProvider(),
        )
        self.assertFalse(second["ok"])
        self.assertEqual(second["reason_code"], "lookup_failed")
        attempt = self.env.db.fetchone("SELECT * FROM publish_attempts WHERE id=?", (self.attempt["id"],))
        self.assertEqual(attempt["state"], "unknown")

    def test_interval_limit_prevents_check_spam(self):
        class EmptyProvider:
            name = "fixture-empty"

            def lookup(self, *, attempt, approval, draft):
                return []

        first = read_only_reconcile(
            self.env.db, self.env.config, self.env.clock,
            attempt_id=self.attempt["id"], provider=EmptyProvider(),
        )
        self.assertTrue(first["checked"])
        second = read_only_reconcile(
            self.env.db, self.env.config, self.env.clock,
            attempt_id=self.attempt["id"], provider=EmptyProvider(),
        )
        self.assertFalse(second["checked"])
        self.assertEqual(second["reason_code"], "reconcile_interval_wait")
        self.assertEqual(int(self.env.db.scalar("SELECT COUNT(*) FROM publish_reconcile_checks")), 1)

    def test_candidate_without_receipt_is_not_published(self):
        class Provider:
            name = "fixture-no-receipt"

            def lookup(self, *, attempt, approval, draft):
                return [NoteEvidence(
                    external_post_id="", external_url="", title=draft["title"], body=draft["body"],
                    published_at=attempt["started_at"], account_id=self.approval["account_id"],
                )]

        result = read_only_reconcile(
            self.env.db, self.env.config, self.env.clock,
            attempt_id=self.attempt["id"], provider=Provider(),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(
            self.env.db.scalar("SELECT state FROM publish_attempts WHERE id=?", (self.attempt["id"],)),
            "unknown",
        )

    def test_provider_error_text_is_sanitized_before_result_and_check(self):
        class Provider:
            name = "fixture-secret-error"

            def lookup(self, *, attempt, approval, draft):
                raise RuntimeError(
                    "bridge offline token=sk-test-SECRET123 "
                    "cookie=session-abc bearer abcdefghijklmnop"
                )

        result = read_only_reconcile(
            self.env.db, self.env.config, self.env.clock,
            attempt_id=self.attempt["id"], provider=Provider(),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "lookup_failed")
        detail = result["detail"]
        self.assertNotIn("sk-test-SECRET123", detail)
        self.assertNotIn("session-abc", detail)
        self.assertNotIn("abcdefghijklmnop", detail)
        check = self.env.db.fetchone(
            "SELECT detail FROM publish_reconcile_checks WHERE attempt_id=? ORDER BY id DESC LIMIT 1",
            (self.attempt["id"],),
        )
        self.assertNotIn("sk-test-SECRET123", check["detail"])
        stored = latest_for_approval(self.env.db, self.approval["id"])
        self.assertNotIn("sk-test-SECRET123", stored["detail"])
        self.assertNotIn("session-abc", stored["detail"])


class WorkerUnconfiguredPublisherTests(unittest.TestCase):
    def setUp(self):
        self.env = helpers.new_env("p0-worker-publisher-none")
        self.pack = helpers.make_draft(self.env)
        helpers.approve(self.env, self.pack["approval_code"])

    def tearDown(self):
        helpers.cleanup(self.env)

    def _job(self):
        return {"payload_json": json.dumps({"approval_id": self.pack["approval_id"]})}

    def test_worker_records_blocked_result_notice_and_stays_idempotent(self):
        worker = Worker(self.env.config, clock=self.env.clock, db=self.env.db)
        worker.publisher = None
        worker.publisher_error = "publisher not configured in this test"
        first = worker._handle_publish(self._job())
        self.assertEqual(first["state"], "blocked")
        self.assertIsNotNone(first.get("result_id"))
        self.assertTrue(first.get("notification_queued"))
        self.assertEqual(int(self.env.db.scalar("SELECT COUNT(*) FROM publish_results")), 1)
        self.assertEqual(int(self.env.db.scalar("SELECT COUNT(*) FROM publish_attempts")), 0)
        self.assertEqual(
            int(self.env.db.scalar(
                "SELECT COUNT(*) FROM notifications WHERE kind='outcome' AND dedupe_key LIKE 'publish-result:%'"
            )),
            1,
        )
        second = worker._handle_publish(self._job())
        self.assertEqual(second.get("result_id"), first.get("result_id"))
        self.assertEqual(int(self.env.db.scalar("SELECT COUNT(*) FROM publish_results")), 1)
        self.assertEqual(int(self.env.db.scalar("SELECT COUNT(*) FROM publish_attempts")), 0)
        self.assertEqual(
            int(self.env.db.scalar(
                "SELECT COUNT(*) FROM notifications WHERE kind='outcome' AND dedupe_key LIKE 'publish-result:%'"
            )),
            1,
        )


if __name__ == "__main__":
    unittest.main()

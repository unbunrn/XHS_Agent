"""The recurring collector must never be killed by a query-less topic row."""

import unittest

from bot.services.research import _topic_source_name, ingest_source, sync_sources
from bot.worker import Worker
from tests.helpers import new_env


class CollectSourceIsolationTests(unittest.TestCase):
    def setUp(self):
        self.env = new_env("collect-isolation")
        self.code = "XT-COLLECT1"
        with self.env.db.tx() as conn:
            conn.execute(
                "INSERT INTO topic_requests(code, message_id, sender_id, raw_text, command, "
                "topic, instruction, day_key, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    self.code,
                    "m-collect",
                    "owner-open-id",
                    "t",
                    "topic",
                    "t",
                    "",
                    "2026-11-09",
                    "now",
                    "now",
                ),
            )
            conn.execute(
                "INSERT INTO sources(name, url, kind, trust_tier, enabled, created_at) "
                "VALUES (?, ?, 'deepseek_search', 2, 1, 'now')",
                (
                    _topic_source_name(self.code),
                    f"deepseek-search://topic/{self.code}",
                ),
            )
        sync_sources(self.env.db, self.env.config)

    def test_ingest_source_returns_error_instead_of_raising(self):
        row = self.env.db.fetchone(
            "SELECT * FROM sources WHERE url=?", (f"deepseek-search://topic/{self.code}",)
        )
        self.assertIsNotNone(row)
        result = ingest_source(
            self.env.db, self.env.config, self.env.http, self.env.clock, row
        )
        self.assertIn("requires a query", (result["error"] or "").lower())
        # The row is still enabled and auditable; only this round skipped it.
        self.assertEqual(
            self.env.db.scalar("SELECT enabled FROM sources WHERE id=?", (row["id"],)), 1
        )

    def test_recurring_collect_skips_commissioned_topic_rows(self):
        worker = Worker(self.env.config, db=self.env.db, clock=self.env.clock)
        worker.startup()
        result = worker._handle_collect(None)
        # Only the configured fixture source is collected; the dynamic topic
        # source is preserved for its topic job and does not abort the round.
        self.assertEqual(result["sources"], 1)
        self.assertEqual(
            self.env.db.scalar(
                "SELECT enabled FROM sources WHERE url=?",
                (f"deepseek-search://topic/{self.code}",),
            ),
            1,
        )
        self.assertIsNone(
            self.env.db.scalar(
                "SELECT last_error FROM sources WHERE url=?",
                (f"deepseek-search://topic/{self.code}",),
            )
        )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.env.root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

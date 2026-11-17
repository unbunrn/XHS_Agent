"""Offline rehearsal for the append-only v8 migration.

Builds a synthetic version-7 database, runs the real Database.init() migration
path twice, and verifies that old rows survive, new columns/tables exist, and
SQLite's integrity check passes.  Deliberately uses a temporary database; it
never opens the application's data directory.
"""

from __future__ import annotations

import shutil
import sqlite3
import unittest
import uuid
from pathlib import Path

from bot.db import Database
from bot.schema import MIGRATIONS

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = PROJECT_ROOT / "var" / "tests"


class Migration8Tests(unittest.TestCase):
    def setUp(self):
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.root = TEST_ROOT / f"migration-s8-{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True, exist_ok=False)
        self.path = self.root / "v7.sqlite3"
        self._make_v7()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _make_v7(self):
        conn = sqlite3.connect(str(self.path))
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(
                "CREATE TABLE schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            for version, sql in MIGRATIONS:
                if version > 7:
                    break
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (?, '2026-01-01T00:00:00Z')",
                    (version,),
                )
            conn.execute(
                "INSERT INTO settings(key, value, updated_at) "
                "VALUES ('legacy', 'kept', '2026-01-01T00:00:00Z')"
            )
            conn.execute(
                "INSERT INTO sources(name, url, kind, enabled, created_at) "
                "VALUES ('legacy-source', 'https://example.com/feed', 'rss', 1, "
                "'2026-01-01T00:00:00Z')"
            )
            conn.commit()
        finally:
            conn.close()

    def test_v7_to_v8_is_idempotent_and_preserves_rows(self):
        db = Database(self.path)
        db.init()

        self.assertEqual(
            db.scalar("SELECT MAX(version) FROM schema_migrations"),
            MIGRATIONS[-1][0],
        )
        columns = {
            row["name"] for row in db.fetchall("PRAGMA table_info(topic_requests)")
        }
        self.assertIn("research_mode", columns)
        self.assertIn("research_plan_json", columns)
        draft_columns = {
            row["name"] for row in db.fetchall("PRAGMA table_info(drafts)")
        }
        self.assertIn("research_mode", draft_columns)
        evidence_columns = {
            row["name"] for row in db.fetchall("PRAGMA table_info(evidence)")
        }
        self.assertIn("source_role", evidence_columns)
        self.assertIn("source_verified", evidence_columns)
        tables = {
            row["name"]
            for row in db.fetchall(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("research_cache", tables)
        self.assertIn("source_identity_cache", tables)

        self.assertEqual(
            db.scalar("SELECT value FROM settings WHERE key='legacy'"), "kept"
        )
        self.assertEqual(
            db.scalar("SELECT COUNT(*) FROM sources WHERE name='legacy-source'"), 1
        )

        # A second init must not re-run the migration or duplicate anything.
        db.init()
        self.assertEqual(
            db.scalar("SELECT MAX(version) FROM schema_migrations"),
            MIGRATIONS[-1][0],
        )
        self.assertEqual(
            db.scalar("SELECT COUNT(*) FROM schema_migrations WHERE version=8"), 1
        )
        self.assertEqual(db.fetchone("PRAGMA integrity_check")[0], "ok")


if __name__ == "__main__":
    unittest.main()

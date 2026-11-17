"""Synthetic old-database rehearsal for Creator Agent V2 migrations 9–10."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest import TestCase

from bot.db import Database
from bot.schema import MIGRATIONS


class MigrationV2Tests(TestCase):
    def test_v8_to_v10_is_additive_and_idempotent(self):
        path = Path("var/tests") / "v2-migration-synthetic.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        for version, sql in MIGRATIONS[:8]:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, '2026-01-01T00:00:00Z')", (version,))
        conn.execute("INSERT INTO settings(key, value, updated_at) VALUES ('legacy.marker', 'keep-me', '2026-01-01T00:00:00Z')")
        conn.commit()
        conn.close()

        db = Database(path)
        db.init()
        db.init()
        row = db.fetchone("SELECT MAX(version) AS v FROM schema_migrations")
        self.assertEqual(int(row["v"]), 10)
        marker = db.fetchone("SELECT value FROM settings WHERE key='legacy.marker'")
        self.assertEqual(marker["value"], "keep-me")
        for table in ("creator_agent_runs", "creator_agent_steps", "creator_agent_artifacts", "creator_agent_preferences", "creator_agent_materials"):
            exists = db.fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
            self.assertIsNotNone(exists, table)
        draft_columns = {
            str(row["name"]) for row in db.fetchall("PRAGMA table_info(drafts)")
        }
        self.assertIn("submission_key", draft_columns)
        self.assertIn("submission_hash", draft_columns)
        index = db.fetchone(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='ux_drafts_submission_key'"
        )
        self.assertIsNotNone(index)
        count = db.fetchone("SELECT COUNT(*) AS n FROM schema_migrations WHERE version=10")
        self.assertEqual(int(count["n"]), 1)
        integrity = db.fetchone("PRAGMA integrity_check")
        self.assertEqual(integrity[0], "ok")
        path.unlink(missing_ok=True)

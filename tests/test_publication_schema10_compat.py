"""Compatibility: old publish path must run before migration 11 is applied."""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest import TestCase

from bot.db import Database
from bot.schema import MIGRATIONS
from bot.services.approvals import get_approval
from bot.services.publish import publish_approval
from tests.helpers import approve, cleanup, make_draft, new_env
from tests.test_publication_agent import FakePublisher


class Schema10CompatibilityTests(TestCase):
    def test_old_publish_path_works_on_schema_10(self):
        env = new_env("schema10-old-publish")
        try:
            made = make_draft(env)
            approve(env, made["approval"]["code"])
            with tempfile.TemporaryDirectory() as tmp:
                old_path = Path(tmp) / "schema10.sqlite3"
                conn = sqlite3.connect(old_path)
                conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
                for version, sql in MIGRATIONS[:10]:
                    conn.executescript(sql)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, '2026-01-01T00:00:00Z')",
                        (version,),
                    )
                conn.commit()
                conn.close()

                src = sqlite3.connect(env.db.path)
                dst = sqlite3.connect(old_path)
                dst.execute("PRAGMA foreign_keys=OFF")
                tables = [
                    str(r[0]) for r in dst.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()
                    if r[0] != "schema_migrations"
                ]
                for table in tables:
                    src_cols = [str(r[1]) for r in src.execute(f'PRAGMA table_info("{table}")')]
                    dst_cols = [str(r[1]) for r in dst.execute(f'PRAGMA table_info("{table}")')]
                    cols = [c for c in dst_cols if c in src_cols]
                    if not cols:
                        continue
                    col_sql = ", ".join(f'"{c}"' for c in cols)
                    rows = src.execute(f'SELECT {col_sql} FROM "{table}"').fetchall()
                    if rows:
                        dst.executemany(
                            f'INSERT INTO "{table}" ({col_sql}) VALUES ({",".join("?" for _ in cols)})',
                            rows,
                        )
                dst.commit()
                dst.close()
                src.close()

                old = Database(old_path)
                approval = get_approval(old, approval_id=int(made["approval"]["id"]))
                self.assertIsNotNone(approval)
                env.config.data["app"]["db_path"] = str(old_path)
                result = publish_approval(
                    old, env.config, env.clock, int(approval["id"]), publisher=FakePublisher()
                )
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["state"], "published")
                self.assertEqual(old.scalar("SELECT COUNT(*) FROM publish_attempts"), 1)
        finally:
            cleanup(env)

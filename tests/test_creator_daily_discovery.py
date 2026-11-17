"""Offline tests for V2 scheduled discovery (AIHot lead -> scheduled run)."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from bot.db import Database
from bot.services.creator_agent.discovery import DiscoveryItem, discover_aihot
from bot.services.creator_agent.entry import scheduled_tick
from bot.worker import Worker
from tests.helpers import cleanup, new_env


class DailyDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.env = new_env("v2-daily")
        self.env.config.data["creator_agent"].update(
            {
                "enabled": True,
                "daily_discovery_enabled": True,
                "daily_discovery_seconds": 86400,
                "daily_discovery_max_topics": 2,
                "daily_discovery_max_age_hours": 48,
                "daily_discovery_feed_limit": 10,
            }
        )

    def tearDown(self):
        cleanup(self.env)

    def test_discover_aihot_filters_old_and_dedupes(self):
        now = self.env.clock.now()
        items = [
            SimpleNamespace(
                title="Fresh lead",
                original_url="https://example.com/original",
                item_url="https://aihot.news/items/fresh",
                summary="<p>fresh summary</p>",
                published_at=(now - timedelta(hours=2), "time"),
            ),
            SimpleNamespace(
                title="Fresh lead",
                original_url="https://example.com/duplicate",
                item_url="https://aihot.news/items/duplicate",
                summary="duplicate",
                published_at=(now - timedelta(hours=1), "time"),
            ),
            SimpleNamespace(
                title="Old lead",
                original_url="https://example.com/old",
                item_url="https://aihot.news/items/old",
                summary="old",
                published_at=(now - timedelta(hours=72), "time"),
            ),
        ]
        with patch("bot.services.creator_agent.discovery.fetch_items", return_value=items):
            found = discover_aihot(self.env.http, self.env.config, self.env.clock, limit=5)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].topic, "Fresh lead")
        self.assertEqual(found[0].source_url, "https://example.com/original")
        self.assertEqual(found[0].summary, "fresh summary")

    def test_scheduled_brief_carries_source_url_and_link_instruction(self):
        ids = scheduled_tick(
            self.env.db,
            self.env.clock,
            account_id="xhs-main",
            discover=lambda: [
                {
                    "topic": "Fresh lead",
                    "source_url": "https://example.com/original",
                    "summary": "summary text",
                }
            ],
            max_topics=1,
        )
        self.assertEqual(len(ids), 1)
        job = self.env.db.fetchone(
            "SELECT payload_json FROM jobs WHERE kind='creator_agent_run' AND state='pending'"
        )
        self.assertIsNotNone(job)
        import json

        brief = json.loads(job["payload_json"])["brief"]
        self.assertEqual(brief["source_url"], "https://example.com/original")
        self.assertIn("https://example.com/original", brief["instruction"])

    def test_worker_enqueues_discovery_once_then_advances(self):
        worker = Worker(self.env.config, clock=self.env.clock)
        worker.startup()
        first = worker.ensure_creator_discovery_scheduled()
        self.assertIsNotNone(first)
        second = worker.ensure_creator_discovery_scheduled()
        self.assertIsNone(second)
        job = self.env.db.fetchone(
            "SELECT * FROM jobs WHERE kind='creator_discovery' AND state='pending'"
        )
        self.assertIsNotNone(job)

    def test_handler_creates_bounded_scheduled_runs(self):
        worker = Worker(self.env.config, clock=self.env.clock)
        leads = [
            DiscoveryItem(
                topic=f"Lead {i}",
                source_url=f"https://example.com/{i}",
                summary="summary",
                published_at=None,
            )
            for i in range(3)
        ]
        with patch("bot.worker.discover_aihot", return_value=leads):
            result = worker._handle_creator_discovery(None)
        self.assertEqual(result["discovered"], 3)
        self.assertEqual(len(result["runs"]), 2)
        rows = self.env.db.fetchall(
            "SELECT payload_json FROM jobs "
            "WHERE kind='creator_agent_run' AND state='pending'"
        )
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()

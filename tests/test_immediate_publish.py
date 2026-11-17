"""Approval-time scheduling: immediate mode still goes through the gate."""

from __future__ import annotations

import unittest

from bot.services.drafts import _schedule
from bot.timeutil import parse_iso
from tests.helpers import cleanup, new_env


class ImmediatePublishScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = new_env("immediate-publish")

    def tearDown(self) -> None:
        cleanup(self.env)

    def test_immediate_mode_schedules_at_approval_time(self) -> None:
        self.env.config.data["limits"]["publish_immediately"] = True
        scheduled, expires = _schedule(self.env.config, self.env.clock)
        self.assertEqual(parse_iso(scheduled), self.env.clock.now())
        self.assertGreater(parse_iso(expires), self.env.clock.now())

    def test_default_mode_still_waits_for_evening_slot(self) -> None:
        self.env.config.data["limits"]["publish_immediately"] = False
        # The default test environment is a demo pipeline, which schedules
        # immediately for deterministic offline runs.  Make it look like a
        # production pipeline here so the normal evening-slot path is used.
        self.env.config.data["llm"]["provider"] = "deepseek"
        self.env.config.data["sources"][0]["kind"] = "rss"
        scheduled, _ = _schedule(self.env.config, self.env.clock)
        self.assertGreater(parse_iso(scheduled), self.env.clock.now())


if __name__ == "__main__":
    unittest.main()

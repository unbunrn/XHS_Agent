"""Offline checks for the optional third (Feishu gateway) process."""

from __future__ import annotations

import copy
import unittest
from pathlib import Path

from bot.config import DEFAULTS, Config, Secrets

ROOT = Path(__file__).resolve().parents[1]


def _config(*, secrets: Secrets | None = None, owner: str = "") -> Config:
    data = copy.deepcopy(DEFAULTS)
    data["notify"]["provider"] = "lark"
    data["notify"]["lark"]["owner_open_id"] = owner
    return Config(data, ROOT, secrets=secrets or Secrets())


class GatewayLifecycleTests(unittest.TestCase):
    def test_gateway_is_optional_for_fake_notifications(self) -> None:
        data = copy.deepcopy(DEFAULTS)
        state = Config(data, ROOT, secrets=Secrets()).lark_gateway_state()
        self.assertFalse(state["required"])
        self.assertFalse(state["configured"])

    def test_gateway_requires_app_credentials_and_owner(self) -> None:
        configured = _config(
            secrets=Secrets(lark_app_id="app", lark_app_secret="secret"),
            owner="ou_owner",
        )
        state = configured.lark_gateway_state()
        self.assertTrue(state["required"])
        self.assertTrue(state["configured"])

        missing_owner = _config(
            secrets=Secrets(lark_app_id="app", lark_app_secret="secret")
        ).lark_gateway_state()
        self.assertFalse(missing_owner["configured"])
        self.assertIn("LARK_OWNER_OPEN_ID", missing_owner["reason"])

        missing_secret = _config(
            secrets=Secrets(lark_app_id="app"), owner="ou_owner"
        ).lark_gateway_state()
        self.assertFalse(missing_secret["configured"])
        self.assertIn("LARK_APP_SECRET", missing_secret["reason"])

    def test_start_stop_health_manage_gateway_with_shared_identity_checks(self) -> None:
        start = (ROOT / "scripts" / "start.ps1").read_text(encoding="utf-8")
        stop = (ROOT / "scripts" / "stop.ps1").read_text(encoding="utf-8")
        health = (ROOT / "scripts" / "health.ps1").read_text(encoding="utf-8")
        launch = (ROOT / "scripts" / "launch.py").read_text(encoding="utf-8")
        for text in (start, stop, health):
            self.assertIn("gateway", text.lower())
            self.assertIn("Test-BotProcessOwnership", text)
        self.assertIn('"gateway"', launch)
        self.assertIn("gateway.pid", start)
        self.assertIn('"gateway"', stop)
        self.assertIn("gateway.pid", health)


if __name__ == "__main__":
    unittest.main()

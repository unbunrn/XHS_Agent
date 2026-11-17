"""Dependency/setup metadata regression checks (no network, no secrets)."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SetupMetadataTests(unittest.TestCase):
    def test_requirements_pin_required_runtime_dependencies(self) -> None:
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        lowered = text.lower()
        self.assertIn("pillow==", lowered)
        self.assertIn("lark-oapi==", lowered)
        self.assertNotIn("--system-site-packages", lowered)

    def test_gitignore_covers_local_secrets_runtime_and_caches(self) -> None:
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for pattern in (
            ".venv",
            "config/secrets.toml",
            "config/bot.toml",
            "var/",
            "__pycache__/",
            ".env",
        ):
            self.assertIn(pattern, text, pattern)

    def test_local_setup_command_uses_an_ordinary_venv(self) -> None:
        setup = (ROOT / "scripts" / "setup.ps1").read_text(encoding="utf-8")
        self.assertIn("-m venv", setup)
        self.assertIn("requirements.txt", setup)
        # Comments may mention the forbidden flag; the actual venv command
        # must not use it.
        venv_calls = [line for line in setup.splitlines() if "-m venv" in line]
        self.assertTrue(venv_calls)
        for line in venv_calls:
            self.assertNotIn("--system-site-packages", line)
        self.assertIn("--no-cache-dir", setup)
        self.assertIn("--timeout", setup)
        self.assertIn("--retries", setup)
        self.assertIn("PIP_CACHE_DIR", setup)
        self.assertIn("$env:TEMP", setup)
        self.assertNotIn("--upgrade pip", setup)
        self.assertTrue((ROOT / "setup-bot.cmd").is_file())
        # The wrapper must call the script and propagate its exit code.
        wrapper = (ROOT / "setup-bot.cmd").read_text(encoding="utf-8")
        self.assertIn("scripts\\setup.ps1", wrapper)
        self.assertIn("%ERRORLEVEL%", wrapper)


if __name__ == "__main__":
    unittest.main()

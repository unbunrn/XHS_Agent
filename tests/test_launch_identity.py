"""The launcher must record a verifiable process identity next to the PID."""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

from scripts.launch import _identity_path, _write_identity
from tests.helpers import _fresh_dir


class LaunchIdentityTests(unittest.TestCase):
    def test_identity_file_contains_pid_executable_command_and_project_root(self) -> None:
        root = _fresh_dir("launch-identity")
        try:
            pid_file = root / "var" / "run" / "worker.pid"
            pid_file.parent.mkdir(parents=True)
            pid_file.write_text("4242", encoding="ascii")
            config = root / "config" / "bot.toml"
            config.parent.mkdir(parents=True)
            config.write_text("", encoding="utf-8")
            command = [
                str(root / ".venv" / "Scripts" / "python.exe"),
                "-m",
                "bot",
                "--config",
                str(config),
                "worker",
            ]
            identity_path = _write_identity(
                pid_file=pid_file,
                name="worker",
                pid=4242,
                executable=command[0],
                command=command,
                root=root,
                config=config,
            )
            self.assertEqual(identity_path, _identity_path(pid_file))
            self.assertTrue(identity_path.is_file())
            payload = json.loads(identity_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["pid"], 4242)
            self.assertEqual(payload["name"], "worker")
            self.assertEqual(payload["executable"], command[0])
            self.assertEqual(payload["command"], command)
            self.assertEqual(Path(payload["project_root"]), root)
            self.assertEqual(Path(payload["config"]), config)
            self.assertIn("-m bot", payload["command_line"])
            self.assertTrue(payload["started_at"])
            # No temporary file should be left behind.
            self.assertEqual(list(identity_path.parent.glob("*.tmp")), [])
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

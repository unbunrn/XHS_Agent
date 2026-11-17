"""Offline tests for optional Docker bridge image path mapping.

The bridge container mounts ``var/drafts`` at ``/app/images``.  These tests use
a fake loopback HTTP client only: no bridge, platform, token or real network call
is involved.
"""

from __future__ import annotations

import copy
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from bot.adapters.publishers import PublishRequest, XiaohongshuPublisher
from bot.adapters.types import NotConfigured, PublishFailed
from bot.config import DEFAULTS, Config, Secrets
from bot.timeutil import FrozenClock
from tests.helpers import PROJECT_ROOT, _fresh_dir


_MISSING = object()


class FakeResponse:
    def __init__(self, status: int, payload):
        self.status = status
        self._payload = payload

    def json(self):
        return self._payload


class FakeBridgeHttp:
    def __init__(self):
        self.gets: list[str] = []
        self.posts: list[dict] = []

    def get(self, url, headers=None, allow_private=False):
        self.gets.append(url)
        if url.endswith("/api/v1/login/status"):
            return FakeResponse(200, {"is_logged_in": True})
        if url.endswith("/api/v1/user/me"):
            return FakeResponse(
                200,
                {
                    "success": True,
                    "data": {"data": {"userBasicInfo": {"redId": "red-123"}}},
                },
            )
        return FakeResponse(404, {})

    def post_json(self, url, body, headers=None, allow_private=False):
        self.posts.append({"url": url, "body": body})
        return FakeResponse(
            200,
            {
                "success": True,
                "data": {
                    "post_id": "note-container",
                    "url": "https://xhs.example/note-container",
                },
            },
        )


class ContainerImagePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = _fresh_dir("container-images")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.drafts = self.root / "var" / "drafts"
        self.drafts.mkdir(parents=True)

    def _config(
        self,
        *,
        host_root=_MISSING,
        container_root=_MISSING,
    ) -> Config:
        data = copy.deepcopy(DEFAULTS)
        data["app"]["mode"] = "live"
        data["llm"]["provider"] = "deepseek"
        data["sources"] = [
            {
                "name": "News",
                "url": "https://example.com/feed.xml",
                "kind": "rss",
                "trust_tier": 2,
                "enabled": True,
            }
        ]
        data["publisher"]["provider"] = "xiaohongshu"
        data["publisher"]["live_enabled"] = True
        mapping = data["publisher"]["xiaohongshu"]
        mapping["expected_red_id"] = "red-123"
        if host_root is not _MISSING:
            mapping["image_host_root"] = host_root
        if container_root is not _MISSING:
            mapping["image_container_root"] = container_root
        return Config(data, self.root, secrets=Secrets(xiaohongshu_token="test-token"))

    def _publisher(self, config: Config, bridge: FakeBridgeHttp | None = None):
        with mock.patch.dict(os.environ, {"BOT_ALLOW_REAL_PUBLISH": "1"}):
            publisher = XiaohongshuPublisher(config, FrozenClock())
        publisher.http = bridge or FakeBridgeHttp()
        return publisher

    def _image(self, path: Path, ordinal: int = 1, sha: str | None = None) -> dict:
        return {
            "ordinal": ordinal,
            "path": str(path),
            "sha256": sha or ("a" * 64),
            "width": 1080,
            "height": 1440,
        }

    def _request(self, images: list[dict]) -> PublishRequest:
        return PublishRequest(
            draft_id=7,
            draft_version=3,
            title="标题",
            body="正文",
            body_sha256="b" * 64,
            images=images,
            platform="xiaohongshu",
            account_id="xhs-main",
            approval_code="XH-ABC123",
            scheduled_at="2026-11-05T12:00:00Z",
            tags=["AI", "工具"],
        )

    def _assert_rejected_before_post(self, config: Config, image: dict) -> None:
        bridge = FakeBridgeHttp()
        publisher = self._publisher(config, bridge)
        with self.assertRaises(PublishFailed):
            publisher.publish(self._request([image]))
        self.assertEqual(bridge.posts, [])

    def test_default_without_mapping_keeps_native_absolute_path(self) -> None:
        image = self.drafts / "nested" / "native.png"
        image.parent.mkdir()
        image.write_bytes(b"png-native")
        bridge = FakeBridgeHttp()
        publisher = self._publisher(self._config(), bridge)

        outcome = publisher.publish(self._request([self._image(image)]))

        self.assertEqual(outcome.state, "published")
        self.assertEqual(len(bridge.posts), 1)
        self.assertEqual(bridge.posts[0]["body"]["images"], [str(image)])

    def test_valid_nested_image_maps_to_container_root(self) -> None:
        image = self.drafts / "campaign" / "deep" / "card.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"png-mapped")
        bridge = FakeBridgeHttp()
        publisher = self._publisher(
            self._config(host_root="var/drafts", container_root="/app/images"),
            bridge,
        )

        outcome = publisher.publish(self._request([self._image(image)]))

        self.assertEqual(outcome.state, "published")
        self.assertEqual(
            bridge.posts[0]["body"]["images"],
            ["/app/images/campaign/deep/card.png"],
        )

    def test_outside_root_and_prefix_collision_rejected_before_post(self) -> None:
        outside = self.root / "var" / "outside.png"
        outside.write_bytes(b"outside")
        collision_dir = self.root / "var" / "drafts-evil"
        collision_dir.mkdir()
        collision = collision_dir / "card.png"
        collision.write_bytes(b"collision")
        config = self._config(host_root="var/drafts", container_root="/app/images")

        self._assert_rejected_before_post(config, self._image(outside))
        self._assert_rejected_before_post(config, self._image(collision))

    def test_traversal_missing_file_and_directory_rejected_before_post(self) -> None:
        secret = self.root / "var" / "secret.png"
        secret.write_bytes(b"not-under-drafts")
        traversal = self.drafts / ".." / "secret.png"
        missing = self.drafts / "missing.png"
        directory = self.drafts / "folder"
        directory.mkdir()
        config = self._config(host_root="var/drafts", container_root="/app/images")

        self._assert_rejected_before_post(config, self._image(traversal))
        self._assert_rejected_before_post(config, self._image(missing))
        self._assert_rejected_before_post(config, self._image(directory))

    def test_symlink_or_junction_escape_rejected_before_post(self) -> None:
        outside_dir = self.root / "var" / "outside-images"
        outside_dir.mkdir()
        (outside_dir / "card.png").write_bytes(b"outside-target")
        link = self.drafts / "escape-dir"
        # A file symlink is the simplest case.  Windows normally withholds that
        # privilege, so fall back to a directory junction, which still follows
        # to the real target during Path.resolve().
        try:
            link.symlink_to(outside_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            if os.name != "nt":
                self.skipTest("this platform cannot create symlinks for the test")
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(outside_dir)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        if not link.is_dir():
            self.skipTest("symlink/junction creation is unavailable on this host")

        config = self._config(host_root="var/drafts", container_root="/app/images")
        self._assert_rejected_before_post(config, self._image(link / "card.png"))
        # The resolved target remains outside the configured host root.
        self.assertFalse(
            str((link / "card.png").resolve()).lower().startswith(
                str(self.drafts).lower() + os.sep
            )
        )

    def test_one_bad_image_prevents_the_whole_post(self) -> None:
        good = self.drafts / "good.png"
        good.write_bytes(b"good")
        outside = self.root / "outside.png"
        outside.write_bytes(b"bad")
        bridge = FakeBridgeHttp()
        publisher = self._publisher(
            self._config(host_root="var/drafts", container_root="/app/images"),
            bridge,
        )

        with self.assertRaises(PublishFailed):
            publisher.publish(
                self._request(
                    [
                        self._image(good, ordinal=1, sha="1" * 64),
                        self._image(outside, ordinal=2, sha="2" * 64),
                    ]
                )
            )
        self.assertEqual(bridge.posts, [])

    def test_half_configured_mapping_is_rejected(self) -> None:
        with self.assertRaises(NotConfigured):
            self._publisher(self._config(host_root="var/drafts"))
        with self.assertRaises(NotConfigured):
            self._publisher(
                self._config(container_root="/app/images")
            )

    def test_malformed_container_roots_are_rejected(self) -> None:
        for container in (
            "/app/../etc",
            "app/images",
            "/app\\images",
            r"C:\app\images",
            "C:/app/images",
            "https://example.com/app/images",
            "//app/images",
            "/app//images",
            "/app/images/",
            "/app/./images",
            "",
            " /app/images",
        ):
            with self.subTest(container=container):
                with self.assertRaises(NotConfigured):
                    self._publisher(
                        self._config(
                            host_root="var/drafts", container_root=container
                        )
                    )

    def test_host_root_outside_project_or_not_directory_rejected(self) -> None:
        for host in (
            "../drafts",
            str(PROJECT_ROOT.parent / "outside-drafts"),
            " var/drafts",
            "http://example.com/drafts",
            "",
        ):
            with self.subTest(host=host):
                with self.assertRaises(NotConfigured):
                    self._publisher(
                        self._config(host_root=host, container_root="/app/images")
                    )
        not_a_directory = self.root / "var" / "not-a-directory"
        not_a_directory.write_bytes(b"file")
        with self.assertRaises(NotConfigured):
            self._publisher(
                self._config(
                    host_root="var/not-a-directory",
                    container_root="/app/images",
                )
            )

    def test_mapping_preserves_request_metadata_and_image_hashes(self) -> None:
        first = self.drafts / "a" / "one.png"
        second = self.drafts / "b" / "two.jpg"
        first.parent.mkdir()
        second.parent.mkdir()
        first.write_bytes(b"one")
        second.write_bytes(b"two")
        images = [
            self._image(first, ordinal=1, sha="1" * 64),
            self._image(second, ordinal=2, sha="2" * 64),
        ]
        bridge = FakeBridgeHttp()
        publisher = self._publisher(
            self._config(host_root="var/drafts", container_root="/app/images"),
            bridge,
        )

        publisher.publish(self._request(images))

        body = bridge.posts[0]["body"]
        self.assertEqual(body["images"], ["/app/images/a/one.png", "/app/images/b/two.jpg"])
        self.assertEqual(body["title"], "标题")
        self.assertEqual(body["content"], "正文")
        self.assertEqual(body["tags"], ["AI", "工具"])
        self.assertEqual(body["draft_id"], 7)
        self.assertEqual(body["draft_version"], 3)
        self.assertEqual(body["body_sha256"], "b" * 64)
        self.assertEqual(body["image_sha256"], ["1" * 64, "2" * 64])
        self.assertEqual(body["approval_code"], "XH-ABC123")
        self.assertEqual(body["scheduled_at"], "2026-11-05T12:00:00Z")


if __name__ == "__main__":
    unittest.main()

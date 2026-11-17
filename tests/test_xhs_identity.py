"""Xiaohongshu bridge identity and read-only health tests.

These tests use a fake loopback HTTP client.  No real bridge, platform, token or
network request is involved.  They lock in the rule that a live publish must
confirm the logged-in account via /api/v1/user/me before POST, and that an
unconfirmed post receipt remains unknown.
"""

from __future__ import annotations

import copy
import os
import unittest
from pathlib import Path
from unittest import mock

from bot.adapters.publishers import (
    PublishRequest,
    XiaohongshuPublisher,
    build_publisher,
)
from bot.adapters.types import NotConfigured, PublishFailed, PublishUnknown
from bot.config import DEFAULTS, Config, Secrets
from bot.timeutil import FrozenClock


class FakeResponse:
    def __init__(self, status: int, payload):
        self.status = status
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeBridgeHttp:
    def __init__(self, *, login=None, me=None, publish=None, me_after=None):
        self.login = login or (200, {"is_logged_in": True})
        self.me = me or (
            200,
            {
                "success": True,
                "data": {"data": {"userBasicInfo": {"redId": "red-123"}}},
            },
        )
        self.me_after = me_after
        self.publish_response = publish or (
            200,
            {"success": True, "data": {"post_id": "note-1", "url": "https://xhs.example/note-1"}},
        )
        self.gets: list[str] = []
        self.posts: list[dict] = []

    def get(self, url, headers=None, allow_private=False):
        self.gets.append(url)
        if url.endswith("/api/v1/login/status"):
            return FakeResponse(*self.login)
        if url.endswith("/api/v1/user/me"):
            return FakeResponse(*self.me)
        return FakeResponse(404, {})

    def post_json(self, url, body, headers=None, allow_private=False):
        self.posts.append({"url": url, "body": body})
        response = FakeResponse(*self.publish_response)
        if self.me_after is not None:
            self.me = self.me_after
        return response


def _me_payload(*notes: tuple[str, str, str]):
    feeds = []
    for note_id, title, xsec in notes:
        feeds.append(
            {
                "id": note_id,
                "xsecToken": xsec,
                "noteCard": {"displayTitle": title},
            }
        )
    return (
        200,
        {
            "success": True,
            "data": {
                "data": {
                    "userBasicInfo": {"redId": "red-123"},
                    "feeds": feeds,
                }
            },
        },
    )


def _config(root: Path, *, expected: str = "red-123", token: str = "test-token") -> Config:
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
    data["publisher"]["xiaohongshu"]["expected_red_id"] = expected
    return Config(data, root, secrets=Secrets(xiaohongshu_token=token))


def _request() -> PublishRequest:
    return PublishRequest(
        draft_id=1,
        draft_version=1,
        title="标题",
        body="正文",
        body_sha256="body-sha",
        images=[],
        platform="xiaohongshu",
        account_id="xhs-main",
        approval_code="XH-ABC123",
        scheduled_at="2026-11-05T12:00:00Z",
        tags=[],
    )


class XhsIdentityTests(unittest.TestCase):
    def _publisher(self, config: Config, bridge: FakeBridgeHttp) -> XiaohongshuPublisher:
        with mock.patch.dict(os.environ, {"BOT_ALLOW_REAL_PUBLISH": "1"}):
            publisher = XiaohongshuPublisher(config, FrozenClock())
        publisher.http = bridge
        return publisher

    def test_identity_checked_before_post_and_matching_account_publishes(self) -> None:
        bridge = FakeBridgeHttp()
        publisher = self._publisher(_config(Path.cwd() / "var"), bridge)
        outcome = publisher.publish(_request())
        self.assertEqual(outcome.state, "published")
        self.assertEqual(outcome.external_post_id, "note-1")
        # Both read-only identity endpoints must be called before the POST.
        self.assertEqual(len(bridge.gets), 2)
        self.assertEqual(len(bridge.posts), 1)
        self.assertIn("/api/v1/user/me", bridge.gets[1])

    def test_mismatched_red_id_refuses_before_post(self) -> None:
        bridge = FakeBridgeHttp(
            me=(200, {"success": True, "data": {"data": {"userBasicInfo": {"redId": "someone-else"}}}})
        )
        publisher = self._publisher(_config(Path.cwd() / "var"), bridge)
        with self.assertRaises(PublishFailed) as ctx:
            publisher.publish(_request())
        self.assertIn("does not match", str(ctx.exception))
        self.assertEqual(bridge.posts, [])

    def test_absent_or_guessed_identity_shape_refuses_before_post(self) -> None:
        for payload in (
            {"success": True, "data": {"nickname": "only-a-nickname"}},
            # A guessed user_id is not the documented redId field.
            {"success": True, "data": {"user_id": "guessed-user"}},
            {"success": True, "data": {"data": {"userBasicInfo": {"nickname": "nick"}}}},
        ):
            bridge = FakeBridgeHttp(me=(200, payload))
            publisher = self._publisher(_config(Path.cwd() / "var"), bridge)
            with self.assertRaises(PublishFailed) as ctx:
                publisher.publish(_request())
            self.assertIn("redId", str(ctx.exception))
            self.assertEqual(bridge.posts, [])

    def test_login_failure_refuses_before_post(self) -> None:
        bridge = FakeBridgeHttp(login=(401, {}))
        publisher = self._publisher(_config(Path.cwd() / "var"), bridge)
        with self.assertRaises(PublishFailed):
            publisher.publish(_request())
        self.assertEqual(bridge.posts, [])

        bridge = FakeBridgeHttp(login=(200, {"logged_in": False}))
        publisher = self._publisher(_config(Path.cwd() / "var"), bridge)
        with self.assertRaises(PublishFailed):
            publisher.publish(_request())
        self.assertEqual(bridge.posts, [])

    def test_expected_red_id_is_required_and_cannot_be_the_local_alias(self) -> None:
        with mock.patch.dict(os.environ, {"BOT_ALLOW_REAL_PUBLISH": "1"}):
            with self.assertRaises(NotConfigured):
                build_publisher(_config(Path.cwd() / "var", expected=""), FrozenClock())
            alias_cfg = _config(Path.cwd() / "var", expected="xhs-main")
            alias_cfg.data["account"]["id"] = "xhs-main"
            with self.assertRaises(NotConfigured):
                build_publisher(alias_cfg, FrozenClock())
            legacy = _config(Path.cwd() / "var", expected="")
            legacy.data["publisher"]["xiaohongshu"]["expected_user_id"] = "legacy-id"
            with self.assertRaises(NotConfigured):
                build_publisher(legacy, FrozenClock())

    def test_read_only_health_check_works_with_real_publishing_disabled(self) -> None:
        cfg = _config(Path.cwd() / "var")
        cfg.data["publisher"]["live_enabled"] = False
        bridge = FakeBridgeHttp()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BOT_ALLOW_REAL_PUBLISH", None)
            publisher = build_publisher(cfg, FrozenClock(), health_only=True)
        publisher.http = bridge
        health = publisher.health()
        self.assertTrue(health["read_only"])
        self.assertFalse(health["armed"])
        self.assertFalse(health["publish_ready"])
        self.assertTrue(health["ok"])
        self.assertEqual(health["logged_in"], True)
        self.assertEqual(health["identity_ok"], True)
        self.assertEqual(bridge.posts, [])
        with self.assertRaises(NotConfigured):
            publisher.publish(_request())
        self.assertEqual(bridge.posts, [])

    def test_documented_receipt_without_note_id_stays_unknown(self) -> None:
        for data in (
            # Documented publish response: status only, no note id/URL.
            {"title": "t", "content": "c", "images": [], "status": "发布完成"},
            # A generic data.id is not a documented note id.
            {"id": "unrelated-id", "status": "发布完成"},
        ):
            bridge = FakeBridgeHttp(publish=(200, {"success": True, "data": data}))
            publisher = self._publisher(_config(Path.cwd() / "var"), bridge)
            with self.assertRaises(PublishUnknown):
                publisher.publish(_request())
            self.assertEqual(len(bridge.posts), 1)

    def test_new_matching_note_on_feed_confirms_2xx_without_receipt(self) -> None:
        before = _me_payload(("old-note", "旧笔记", "old-xsec"))
        after = _me_payload(
            ("new-note", "标题", "new-xsec"),
            ("old-note", "旧笔记", "old-xsec"),
        )
        bridge = FakeBridgeHttp(
            me=before,
            me_after=after,
            publish=(200, {"success": True, "data": {"status": "发布完成"}}),
        )
        publisher = self._publisher(_config(Path.cwd() / "var"), bridge)
        outcome = publisher.publish(_request())
        self.assertEqual(outcome.state, "published")
        self.assertEqual(outcome.external_post_id, "new-note")
        self.assertEqual(
            outcome.external_url,
            "https://www.xiaohongshu.com/explore/new-note?xsec_token=new-xsec",
        )
        self.assertTrue(outcome.receipt["verified_from_feed"])
        self.assertEqual(outcome.receipt["new_note"]["title"], "标题")
        self.assertEqual(len(bridge.posts), 1)
        # login, identity, then the read-only post-publish feed check
        self.assertEqual(len(bridge.gets), 3)

    def test_unchanged_feed_does_not_confirm_2xx_without_receipt(self) -> None:
        feed = _me_payload(("old-note", "旧笔记", "old-xsec"))
        bridge = FakeBridgeHttp(
            me=feed,
            me_after=feed,
            publish=(200, {"success": True, "data": {"status": "发布完成"}}),
        )
        publisher = self._publisher(_config(Path.cwd() / "var"), bridge)
        with self.assertRaises(PublishUnknown):
            publisher.publish(_request())

    def test_old_duplicate_title_is_not_a_new_note(self) -> None:
        feed = _me_payload(("old-note", "标题", "old-xsec"))
        bridge = FakeBridgeHttp(
            me=feed,
            me_after=feed,
            publish=(200, {"success": True, "data": {"status": "发布完成"}}),
        )
        publisher = self._publisher(_config(Path.cwd() / "var"), bridge)
        with self.assertRaises(PublishUnknown):
            publisher.publish(_request())

    def test_new_note_with_other_title_does_not_confirm(self) -> None:
        before = _me_payload(("old-note", "旧笔记", "old-xsec"))
        after = _me_payload(
            ("new-note", "另一个标题", "new-xsec"),
            ("old-note", "旧笔记", "old-xsec"),
        )
        bridge = FakeBridgeHttp(
            me=before,
            me_after=after,
            publish=(200, {"success": True, "data": {"status": "发布完成"}}),
        )
        publisher = self._publisher(_config(Path.cwd() / "var"), bridge)
        with self.assertRaises(PublishUnknown):
            publisher.publish(_request())

    def test_5xx_with_new_matching_note_is_published(self) -> None:
        before = _me_payload(("old-note", "旧笔记", "old-xsec"))
        after = _me_payload(
            ("new-note", "标题", "new-xsec"),
            ("old-note", "旧笔记", "old-xsec"),
        )
        bridge = FakeBridgeHttp(
            me=before,
            me_after=after,
            publish=(500, {"success": False, "message": "发布未确认成功"}),
        )
        publisher = self._publisher(_config(Path.cwd() / "var"), bridge)
        outcome = publisher.publish(_request())
        self.assertEqual(outcome.state, "published")
        self.assertEqual(outcome.external_post_id, "new-note")
        self.assertTrue(outcome.receipt["verified_from_feed"])
        self.assertEqual(len(bridge.posts), 1)

    def test_5xx_with_readable_unchanged_feed_is_failed(self) -> None:
        feed = _me_payload(("old-note", "旧笔记", "old-xsec"))
        bridge = FakeBridgeHttp(
            me=feed,
            me_after=feed,
            publish=(500, {"success": False, "message": "发布未确认成功"}),
        )
        publisher = self._publisher(_config(Path.cwd() / "var"), bridge)
        with mock.patch("bot.adapters.publishers.time.sleep"):
            with self.assertRaises(PublishFailed):
                publisher.publish(_request())

    def test_5xx_with_unreadable_feed_stays_unknown(self) -> None:
        before = _me_payload(("old-note", "旧笔记", "old-xsec"))
        bridge = FakeBridgeHttp(
            me=before,
            me_after=(500, {"success": False}),
            publish=(500, {"success": False, "message": "发布未确认成功"}),
        )
        publisher = self._publisher(_config(Path.cwd() / "var"), bridge)
        with self.assertRaises(PublishUnknown):
            publisher.publish(_request())


if __name__ == "__main__":
    unittest.main()

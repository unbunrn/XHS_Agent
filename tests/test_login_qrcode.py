"""Tests for automatic login QR code delivery and owner refresh command."""

from __future__ import annotations

import base64
import json
import unittest
from pathlib import Path
from unittest import mock

from bot.adapters.notify import FakeNotifier
from bot.adapters.publishers import XiaohongshuPublisher
from bot.services.approvals import configured_owner, handle_inbound_message
from bot.services.notifications import deliver_pending
from bot.services.publication_agent.entry import create_task_for_approval
from bot.services.publication_agent.live import LivePublicationRuntime
from bot.services.publication_agent.store import PublicationStore
from bot.services.publication_agent.tools import PublicationContext, PublicationTools
from tests.helpers import approve, cleanup, make_draft, new_env


# Minimal valid 1x1 PNG (>64 bytes) for base64 QR stubbing.
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)


class LoginQrCodeTests(unittest.TestCase):
    def test_fetch_login_qrcode_logged_in_and_png_decode(self) -> None:
        env = new_env("xhs-qr-fetch")
        try:
            env.config.secrets.xiaohongshu_token = "test-bridge-token"
            pub = XiaohongshuPublisher(env.config, env.clock, allow_unarmed=True)

            with mock.patch.object(
                pub,
                "_get_json",
                return_value=(200, {"success": True, "data": {"is_logged_in": True, "timeout": "0s"}}),
            ):
                res = pub.fetch_login_qrcode()
            self.assertTrue(res["ok"])
            self.assertTrue(res["already_logged_in"])
            self.assertNotIn("image_path", res)

            b64 = "data:image/png;base64," + base64.b64encode(_TINY_PNG).decode("ascii")
            with mock.patch.object(
                pub,
                "_get_json",
                return_value=(200, {"success": True, "data": {"is_logged_in": False, "timeout": "4m0s", "img": b64}}),
            ):
                res2 = pub.fetch_login_qrcode()
            self.assertTrue(res2["ok"])
            self.assertFalse(res2["already_logged_in"])
            qr_path = Path(res2["image_path"])
            self.assertTrue(qr_path.is_file())
            self.assertEqual(qr_path.read_bytes(), _TINY_PNG)
        finally:
            cleanup(env)

    def test_feishu_refresh_qrcode_command_sends_and_cleans_up_png(self) -> None:
        env = new_env("xhs-qr-cmd")
        try:
            owner_id = configured_owner(env.config)
            qr_file = env.config.resolve_path("var/tmp/xhs_qr/test-qr.png")
            qr_file.parent.mkdir(parents=True, exist_ok=True)
            qr_file.write_bytes(_TINY_PNG)

            # Group chat must be rejected.
            rejected = handle_inbound_message(
                env.db,
                env.config,
                env.clock,
                message_id="msg-qr-group",
                sender_id=owner_id,
                chat_id="oc_group",
                chat_type="group",
                text="刷新二维码",
            )
            self.assertFalse(rejected["ok"])
            self.assertEqual(rejected["outcome"], "rejected_chat_type")

            # Owner p2p chat fetches QR, queues notification with image, and cleans up after send.
            with mock.patch(
                "bot.adapters.publishers.XiaohongshuPublisher.fetch_login_qrcode",
                return_value={
                    "ok": True,
                    "already_logged_in": False,
                    "image_path": str(qr_file),
                    "timeout": "4m0s",
                    "detail": "ok",
                },
            ):
                res = handle_inbound_message(
                    env.db,
                    env.config,
                    env.clock,
                    message_id="msg-qr-p2p",
                    sender_id=owner_id,
                    chat_id="oc_p2p",
                    chat_type="p2p",
                    text="刷新二维码 XH-ABC123",
                )
            self.assertTrue(res["ok"])
            self.assertEqual(res["outcome"], "login_qrcode_sent")
            self.assertTrue(qr_file.exists())

            notifier = FakeNotifier(env.config, env.clock)
            delivered = deliver_pending(env.db, env.config, env.clock, notifier)
            self.assertEqual(len(delivered), 1)
            self.assertTrue(delivered[0]["ok"])
            # Temporary QR file must be removed after delivery.
            self.assertFalse(qr_file.exists())
        finally:
            cleanup(env)

    def test_publication_agent_waiting_login_triggers_qr_without_leaking_to_model(self) -> None:
        env = new_env("xhs-qr-pub")
        try:
            draft_info = make_draft(env)
            approve(env, draft_info["approval_code"])
            store = PublicationStore(env.db, env.clock)
            created = create_task_for_approval(
                env.db, env.config, env.clock, int(draft_info["approval_id"])
            )
            task = store.get(task_id=int(created["task"]["id"]))
            assert task is not None
            qr_file = env.config.resolve_path("var/tmp/xhs_qr/pub-qr.png")
            qr_file.parent.mkdir(parents=True, exist_ok=True)
            qr_file.write_bytes(_TINY_PNG)

            fake_pub = mock.MagicMock()
            fake_pub.fetch_login_qrcode.return_value = {
                "ok": True,
                "already_logged_in": False,
                "image_path": str(qr_file),
                "timeout": "4m0s",
                "detail": "ok",
            }
            runtime = LivePublicationRuntime(env.db, env.config, env.clock, publisher=fake_pub, store=store)
            tools = PublicationTools(runtime)
            ctx = PublicationContext(
                store=store,
                task=task,
                package={"approval_code": draft_info["approval_code"], "assets": []},
                runtime=runtime,
                config=env.config,
                clock=env.clock,
            )
            result = tools._request_owner(ctx, {"kind": "login", "question": "小红书登录已失效，请重新扫码"})
            self.assertTrue(result["ok"])
            obs = result["observation"]
            self.assertEqual(obs["state"], "waiting_login")
            self.assertTrue(obs["qr_sent"])
            # Observation returned to model must NOT contain file path or base64.
            obs_text = json.dumps(obs, ensure_ascii=False)
            self.assertNotIn("pub-qr.png", obs_text)
            self.assertNotIn("base64", obs_text)

            # Verify queued notification carries the QR image and cleanup flag.
            notif = env.db.fetchone("SELECT payload_json FROM notifications ORDER BY id DESC LIMIT 1")
            payload = json.loads(notif["payload_json"])
            self.assertEqual(payload["image_paths"], [str(qr_file)])
            self.assertTrue(payload["cleanup_images"])
            self.assertIn("刷新二维码", payload["text"])
        finally:
            cleanup(env)


if __name__ == "__main__":
    unittest.main()

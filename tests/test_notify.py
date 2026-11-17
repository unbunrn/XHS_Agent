"""Approval notification content and Feishu adapter safety tests."""

# =============================================================================
# 中文导读：通知里必须让人看到“最终会发出去的原文”
# =============================================================================
# 审批通知不是摘要，而是最终稿：正文全文 + 全部图片路径。
# 同时要明确写“本地模拟”，不能让收件人误以为已经发布。
# 飞书配置不完整时，健康检查只能老实说“未连接/未验证”，不能假装正常。
# 解析飞书回调时，外部 JSON 只当数据读取，缺字段就返回 None，不能崩。
# =============================================================================

from __future__ import annotations

import json
import unittest

from bot.adapters.notify import (
    FakeNotifier,
    LarkNotifier,
    NotificationResult,
    Notifier,
    build_notifier,
)
from bot.lark_gateway import parse_message_event
from bot.services.approvals import get_approval, handle_inbound_message
from bot.services.notifications import build_approval_message, deliver_pending
from tests.helpers import cleanup, make_draft, new_env


class PartialImageNotifier(Notifier):
    name = "partial-lark"
    channel = "lark"

    def __init__(self) -> None:
        self.calls = 0

    def send_approval(self, message):
        self.calls += 1
        return NotificationResult(
            ok=False,
            channel="lark",
            message_id="om_text_only",
            detail="INCOMPLETE delivery: image 2 upload failed",
            images_sent=1,
            images_total=2,
            failed_images=("image 2: upload failed",),
        )

    def send_text(self, text):
        return NotificationResult(ok=True, channel="lark", message_id="om_outcome")

    def health(self):
        return {"ok": False, "configured": True}


class CompleteNotifier(Notifier):
    name = "complete-lark"
    channel = "lark"

    def __init__(self) -> None:
        self.calls = 0

    def send_approval(self, message):
        self.calls += 1
        return NotificationResult(
            ok=True,
            channel="lark",
            message_id="om_complete",
            detail="sent final text and every image",
            images_sent=len(message.get("image_paths") or []),
            images_total=len(message.get("image_paths") or []),
        )

    def send_text(self, text):
        return NotificationResult(ok=True, channel="lark", message_id="om_outcome")

    def health(self):
        return {"ok": True, "configured": True}


class ApprovalMessageTests(unittest.TestCase):
    def test_message_contains_final_body_and_every_image(self) -> None:
        # 通知必须包含：最终正文全文、图片张数和每张图的顺序路径。
        # 新稿不再强制四个可见章节，所以不能要求“发生了什么”标题。
        # 并且必须写“本地模拟”，不能出现“已发布”。
        env = new_env("notify-content")
        try:
            draft = make_draft(env)
            approval = env.db.fetchone(
                "SELECT * FROM approvals WHERE id=?", (draft["approval_id"],)
            )
            message = build_approval_message(env.db, env.config, approval)
            self.assertIn("【正文】", message["text"])
            self.assertNotIn("【发生了什么】", message["text"])
            self.assertIn(draft["draft"]["body"], message["text"])
            self.assertIn(f"图片：{len(draft['images'])} 张", message["text"])
            self.assertEqual(len(message["image_paths"]), len(draft["images"]))
            self.assertEqual(
                [p for p in message["image_paths"]],
                [i["path"] for i in draft["images"]],
            )
            self.assertIn("本地模拟", message["text"])
            self.assertNotIn("已发布", message["text"])

            notifier = build_notifier(env.config, env.clock)
            # 默认配置用 fake 通知器：把消息写成本地 JSON 文件，不联网。
            self.assertIsInstance(notifier, FakeNotifier)
            result = notifier.send_approval(message)
            self.assertTrue(result.ok)
            self.assertEqual(result.channel, "fake")
            with open(result.path, encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual(payload["message"]["text"], message["text"])
            self.assertEqual(payload["message"]["image_paths"], message["image_paths"])
            # 文件里也明确说明：这条消息没有发到飞书。
            self.assertIn("没有发送到飞书", payload["notice"])
        finally:
            cleanup(env)

    def test_lark_health_never_claims_connected(self) -> None:
        # 测试环境没有飞书应用凭据：必须如实返回 ok/verified/configured 全为 False，
        # 详情里明确写“未连接”。宁可报红，也不能让运维以为已经通了。
        env = new_env("notify-lark")
        try:
            env.config.data["notify"]["provider"] = "lark"
            notifier = LarkNotifier(env.config, env.clock)
            health = notifier.health()
            self.assertFalse(health["ok"])
            self.assertFalse(health["verified"])
            self.assertFalse(health["configured"])  # no app credentials in tests
            self.assertTrue(
                "missing" in health["detail"].lower()
                or "not installed" in health["detail"].lower()
            )
        finally:
            cleanup(env)

    def test_parse_lark_message_event_as_data(self) -> None:
        # 飞书回调是一层层嵌套的 JSON：
        #   event.sender.sender_id.open_id  -> 谁发的
        #   event.message.message_id        -> 幂等键
        #   event.message.content           -> 一段 JSON 字符串，里面才有 text
        # 解析函数只做取值和校验；缺字段返回 None，不能抛异常。
        event = {
            "event": {
                "sender": {"sender_id": {"open_id": "ou_owner"}},
                "message": {
                    "message_id": "om_1",
                    "chat_id": "oc_1",
                    "chat_type": "p2p",
                    "content": json.dumps({"text": "批准 XH-ABC123"}),
                },
            }
        }
        parsed = parse_message_event(event)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["message_id"], "om_1")
        self.assertEqual(parsed["text"], "批准 XH-ABC123")
        self.assertEqual(parsed["sender_id"], "ou_owner")
        self.assertEqual(parsed["chat_type"], "p2p")
        self.assertIsNone(parse_message_event({"event": {}}))


class DeliveryCompletenessTests(unittest.TestCase):
    def test_lark_adapter_reports_partial_image_failure_as_not_ok(self) -> None:
        # 文字发成功不代表审批材料完整。任何一张图失败都必须 ok=False，
        # 否则通知队列会误标 sent，飞书审批可能在缺图时生效。
        from unittest import mock

        env = new_env("notify-partial-adapter")
        try:
            notifier = LarkNotifier(env.config, env.clock)
            notifier.app_id = "app"
            notifier.app_secret = "secret"
            notifier.owner_open_id = "owner"
            notifier._sdk = object()
            notifier._client = object()

            def upload(path):
                if path.endswith("1.png"):
                    return "image-key-1"
                raise RuntimeError("upload denied")

            with mock.patch.object(notifier, "_send_text", return_value="om_text"):
                with mock.patch.object(notifier, "_upload_image", side_effect=upload):
                    with mock.patch.object(notifier, "_send_image", return_value="om_image"):
                        result = notifier.send_approval(
                            {
                                "text": "approval text",
                                "image_paths": ["var/a1.png", "var/a2.png"],
                            }
                        )
            self.assertFalse(result.ok)
            self.assertEqual(result.images_sent, 1)
            self.assertEqual(result.images_total, 2)
            self.assertIn("INCOMPLETE", result.detail)
            self.assertEqual(result.message_id, "om_text")
        finally:
            cleanup(env)

    def test_partial_image_failure_blocks_phone_approval_and_is_retryable(self) -> None:
        env = new_env("notify-partial-phone")
        try:
            draft = make_draft(env)
            code = draft["approval_code"]
            partial = PartialImageNotifier()
            results = deliver_pending(env.db, env.config, env.clock, partial)
            self.assertFalse(results[0]["ok"])
            notice = env.db.fetchone("SELECT * FROM notifications")
            self.assertEqual(notice["state"], "retry")
            self.assertIn("INCOMPLETE", notice["error"])
            self.assertIsNone(
                env.db.scalar(
                    "SELECT notified_at FROM approvals WHERE id=?", (draft["approval_id"],)
                )
            )

            # Early phone reply after only text arrived must not become effective.
            early = handle_inbound_message(
                env.db,
                env.config,
                env.clock,
                message_id="phone-early",
                sender_id="owner-open-id",
                chat_id="owner-open-id",
                chat_type="p2p",
                text=f"批准 {code}",
                channel="lark",
            )
            self.assertFalse(early["ok"])
            self.assertEqual(early["outcome"], "delivery_incomplete")
            self.assertEqual(get_approval(env.db, code=code)["status"], "pending")

            # A later complete retry makes the notification fully delivered.
            complete = CompleteNotifier()
            results = deliver_pending(env.db, env.config, env.clock, complete)
            self.assertTrue(results[0]["ok"], results)
            notice = env.db.fetchone("SELECT * FROM notifications")
            self.assertEqual(notice["state"], "sent")
            self.assertIsNotNone(
                env.db.scalar(
                    "SELECT notified_at FROM approvals WHERE id=?", (draft["approval_id"],)
                )
            )

            # A fresh phone message after complete delivery is accepted exactly
            # once; replay/duplicate does not create a second decision/job.
            accepted = handle_inbound_message(
                env.db,
                env.config,
                env.clock,
                message_id="phone-after",
                sender_id="owner-open-id",
                chat_id="owner-open-id",
                chat_type="p2p",
                text=f"批准 {code}",
                channel="lark",
            )
            self.assertTrue(accepted["ok"], accepted)
            duplicate = handle_inbound_message(
                env.db,
                env.config,
                env.clock,
                message_id="phone-after-2",
                sender_id="owner-open-id",
                chat_id="owner-open-id",
                chat_type="p2p",
                text=f"批准 {code}",
                channel="lark",
            )
            self.assertEqual(duplicate["outcome"], "already_approved")
            self.assertEqual(
                env.db.scalar(
                    "SELECT COUNT(*) FROM jobs WHERE kind='publish' AND dedupe_key=?",
                    (f"publish:approval:{draft['approval_id']}",),
                ),
                1,
            )
        finally:
            cleanup(env)

    def test_console_approval_does_not_resend_incomplete_prompt(self) -> None:
        env = new_env("notify-console-cancel")
        try:
            draft = make_draft(env)
            code = draft["approval_code"]
            partial = PartialImageNotifier()
            deliver_pending(env.db, env.config, env.clock, partial)
            self.assertEqual(partial.calls, 1)

            # Authenticated local console remains a valid separate path.
            result = handle_inbound_message(
                env.db,
                env.config,
                env.clock,
                message_id="console-approve",
                sender_id="owner-open-id",
                chat_id="owner-open-id",
                chat_type="p2p",
                text=f"批准 {code}",
                channel="console",
            )
            self.assertTrue(result["ok"], result)
            self.assertEqual(get_approval(env.db, code=code)["status"], "approved")

            # A retry must not resend a prompt for an already accepted decision.
            results = deliver_pending(env.db, env.config, env.clock, partial)
            self.assertTrue(results[0].get("cancelled"), results)
            self.assertEqual(partial.calls, 1)
            notice = env.db.fetchone("SELECT * FROM notifications")
            self.assertEqual(notice["state"], "cancelled")
            self.assertIn("already approved", notice["error"])
        finally:
            cleanup(env)


if __name__ == "__main__":
    unittest.main()

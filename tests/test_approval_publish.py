"""Approval binding and publish-gate tests (the eight mandatory conditions)."""

# =============================================================================
# 中文导读：审批绑定与发布闸门的核心测试
# =============================================================================
# 这一组测试保护的是全项目最不能出错的地方。可以把它看成“发布前必须
# 同时满足的八个条件”的自动验收：
#   1. 只有 approved 的审批才能发布，pending 连发布器都不许碰；
#   2. 只有 owner 本人、在私聊里、用正确格式发的命令才算数；
#   3. 同一条回调重复到达不会重复发布；
#   4. 正文被改过 -> 审批失效；
#   5. 图片被改过 -> 审批失效；
#   6. 审批过期 -> 不能发布；
#   7. 计划时间/账号被改 -> 审批失效；
#   8. 指纹绑定每一个字段，任何一项变化都会改变指纹。
# 另外还测试了修订生成新版本、mock 永远不是 published、
# unknown 绝不重试、重启恢复、人工对账必须有真实回执。
# =============================================================================

from __future__ import annotations

import hashlib
import sqlite3
import unittest
from datetime import timedelta
from unittest import mock

from bot.adapters.publishers import Publisher
from bot.adapters.types import PublishOutcome, PublishUnknown
from bot.db import Database
from bot.security import sha256_file, sha256_text
from bot.services import publish
from bot.services.approvals import (
    approval_fingerprint,
    get_approval,
    handle_inbound_message,
)
from tests.helpers import approve, cleanup, make_draft, new_env


class CountingPublisher(Publisher):
    # 测试替身：只数“被调用了几次”，返回事先设定的结果。
    # 用它来断言“该拦下的请求一次都没到发布器”。
    name = "counting"
    mode = "mock"

    def __init__(self, outcome: PublishOutcome | None = None):
        self.calls = 0
        self.outcome = outcome or PublishOutcome(
            state="simulated", detail="test simulation", receipt={"simulated": True}
        )

    def publish(self, request):
        self.calls += 1
        return self.outcome

    def health(self):
        return {"ok": True, "mode": self.mode}


class ApprovalGateTests(unittest.TestCase):
    # 这一组只关心“闸门有没有拦住”，大多断言 results 和 spy.calls。
    def test_pending_approval_never_calls_publisher(self) -> None:
        # 还没批准（pending）时调用发布：应返回 blocked，
        # 发布器调用次数为 0，数据库里也不能出现发布尝试。
        env = new_env("pending")
        try:
            draft = make_draft(env)
            spy = CountingPublisher()
            result = publish.publish_approval(
                env.db, env.config, env.clock, draft["approval_id"], publisher=spy
            )
            self.assertEqual(result["state"], "blocked")
            self.assertEqual(spy.calls, 0)
            self.assertEqual(
                env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 0
            )
        finally:
            cleanup(env)

    def test_non_owner_group_and_wrong_text_are_ignored(self) -> None:
        # 三种“看起来像命令”的消息都必须无效：
        # 陌生人发的、群里发的、文字不是批准/拒绝/修改的。
        # 最后再确认审批仍然是 pending，没有被偷偷改掉。
        env = new_env("owner-check")
        try:
            draft = make_draft(env)
            code = draft["approval_code"]
            bad = handle_inbound_message(
                env.db,
                env.config,
                env.clock,
                message_id="bad-owner",
                sender_id="stranger",
                chat_id="stranger",
                chat_type="p2p",
                text=f"批准 {code}",
            )
            self.assertFalse(bad["ok"])
            group = handle_inbound_message(
                env.db,
                env.config,
                env.clock,
                message_id="group-message",
                sender_id="owner-open-id",
                chat_id="group-chat",
                chat_type="group",
                text=f"批准 {code}",
            )
            self.assertFalse(group["ok"])
            nonsense = approve(env, code, text="今天天气不错")
            self.assertFalse(nonsense["ok"])
            self.assertEqual(get_approval(env.db, code=code)["status"], "pending")
        finally:
            cleanup(env)

    def test_duplicate_approval_and_callback_do_not_republish(self) -> None:
        # 三种重复场景：
        #   1. 完全相同的 message_id -> duplicate no-op；
        #   2. 不同 message_id 再批准 -> already_approved；
        #   3. 发布完成后再调 publish_approval -> already_attempted。
        # 最终只有一个发布尝试、发布器只被调用一次。
        env = new_env("duplicate")
        try:
            draft = make_draft(env)
            code = draft["approval_code"]
            first = approve(env, code, message_id="same-message-id")
            self.assertTrue(first["ok"], first)
            # Same message id is a callback replay: must be an idempotent no-op.
            replay = approve(env, code, message_id="same-message-id")
            self.assertTrue(replay.get("duplicate"), replay)
            again = approve(env, code, message_id="another-message-id")
            self.assertEqual(again.get("outcome"), "already_approved", again)

            publisher = CountingPublisher()
            result = publish.publish_approval(
                env.db, env.config, env.clock, draft["approval_id"], publisher=publisher
            )
            self.assertEqual(result["state"], "simulated")
            self.assertEqual(publisher.calls, 1)
            second = publish.publish_approval(
                env.db, env.config, env.clock, draft["approval_id"], publisher=publisher
            )
            self.assertEqual(second["state"], "already_attempted")
            self.assertEqual(publisher.calls, 1)
            self.assertEqual(
                env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 1
            )
        finally:
            cleanup(env)

    def test_body_edit_invalidates_approval(self) -> None:
        # 批准之后直接改数据库里的正文（并同步改哈希，模拟“改得很像真的”）。
        # 发布闸门重算正文哈希后应该发现与审批不一致，返回 blocked。
        env = new_env("body-edit")
        try:
            draft = make_draft(env)
            approve(env, draft["approval_code"])
            with env.db.tx() as conn:
                row = conn.execute(
                    "SELECT body FROM drafts WHERE id=?", (draft["draft"]["id"],)
                ).fetchone()
                changed = row["body"] + "\n未经审批的新增正文"
                conn.execute(
                    "UPDATE drafts SET body=?, body_sha256=? WHERE id=?",
                    (changed, sha256_text(changed), draft["draft"]["id"]),
                )
            spy = CountingPublisher()
            result = publish.publish_approval(
                env.db, env.config, env.clock, draft["approval_id"], publisher=spy
            )
            self.assertEqual(result["state"], "blocked")
            self.assertIn("body", result["detail"].lower())
            self.assertEqual(spy.calls, 0)
            self.assertEqual(
                env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 0
            )
        finally:
            cleanup(env)

    def test_image_edit_invalidates_approval(self) -> None:
        # 直接往已批准的 PNG 末尾追加几个字节（文件仍能打开）。
        # 发布前会重新读取文件算 sha256，所以这种篡改也逃不掉。
        env = new_env("image-edit")
        try:
            draft = make_draft(env)
            approve(env, draft["approval_code"])
            image = draft["images"][0]
            with open(image["path"], "ab") as handle:
                handle.write(b"tampered")
            spy = CountingPublisher()
            result = publish.publish_approval(
                env.db, env.config, env.clock, draft["approval_id"], publisher=spy
            )
            self.assertEqual(result["state"], "blocked")
            self.assertIn("image", result["detail"].lower())
            self.assertEqual(spy.calls, 0)
            self.assertEqual(
                env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 0
            )
        finally:
            cleanup(env)

    def test_expiry_or_content_edit_between_validation_and_claim(self) -> None:
        # 回归：事务外预检通过后，状态可能立刻变化。
        # claim_publish 必须在同一个写事务里重新检查过期和正文哈希，
        # 不能把预检结果当成“已经安全”。
        env = new_env("validate-claim-race")
        try:
            draft = make_draft(env)
            approve(env, draft["approval_code"])
            approval = get_approval(env.db, approval_id=draft["approval_id"])
            spy = CountingPublisher()
            started = env.clock.now()
            ok, reason, _, _ = publish.validate_publish(
                env.db, env.config, env.clock, approval, spy
            )
            self.assertTrue(ok, reason)

            # Expiry changes between validation and claim.
            env.clock.advance(hours=30)
            claim, claim_reason = publish.claim_publish(
                env.db, env.config, env.clock, int(approval["id"]), spy
            )
            self.assertIsNone(claim)
            self.assertIn("expired", claim_reason.lower())
            self.assertEqual(spy.calls, 0)
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 0)

            # Content changes between validation and claim (even with the
            # stored hash updated, as a crude edit would do).
            env.clock.set(started)
            with env.db.tx() as conn:
                row = conn.execute(
                    "SELECT body FROM drafts WHERE id=?", (draft["draft"]["id"],)
                ).fetchone()
                changed = row["body"] + "\n预检之后才加入的正文"
                conn.execute(
                    "UPDATE drafts SET body=?, body_sha256=? WHERE id=?",
                    (changed, sha256_text(changed), draft["draft"]["id"]),
                )
            claim, claim_reason = publish.claim_publish(
                env.db, env.config, env.clock, int(approval["id"]), spy
            )
            self.assertIsNone(claim)
            self.assertIn("body", claim_reason.lower())
            self.assertEqual(spy.calls, 0)
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 0)
        finally:
            cleanup(env)

    def test_expired_approval_cannot_publish(self) -> None:
        # 批准后把时钟往前拨 30 小时，超过审批有效期。
        # 预期 expired，并且审批状态也被级联改成 expired。
        env = new_env("expired")
        try:
            draft = make_draft(env)
            approve(env, draft["approval_code"])
            env.clock.advance(hours=30)
            spy = CountingPublisher()
            result = publish.publish_approval(
                env.db, env.config, env.clock, draft["approval_id"], publisher=spy
            )
            self.assertEqual(result["state"], "expired")
            self.assertEqual(spy.calls, 0)
            self.assertEqual(
                get_approval(env.db, code=draft["approval_code"])["status"], "expired"
            )
        finally:
            cleanup(env)

    def test_schedule_or_account_change_invalidates_approval(self) -> None:
        # 先改计划发布时间，再改账号 ID。两者都属于审批绑定字段，
        # 任一变化都应让发布被 blocked，且原因里明确指出是哪一项。
        env = new_env("schedule-account-edit")
        try:
            draft = make_draft(env)
            approve(env, draft["approval_code"])
            with env.db.tx() as conn:
                row = conn.execute(
                    "SELECT scheduled_at FROM drafts WHERE id=?", (draft["draft"]["id"],)
                ).fetchone()
                conn.execute(
                    "UPDATE drafts SET scheduled_at=? WHERE id=?",
                    (row["scheduled_at"], draft["draft"]["id"]),
                )
            # A changed schedule is rejected even if the ISO string is merely
            # rewritten; change the value to make the binding explicit.
            with env.db.tx() as conn:
                conn.execute(
                    "UPDATE drafts SET scheduled_at='2026-11-05T11:00:00+00:00' WHERE id=?",
                    (draft["draft"]["id"],),
                )
            spy = CountingPublisher()
            result = publish.publish_approval(
                env.db, env.config, env.clock, draft["approval_id"], publisher=spy
            )
            self.assertEqual(result["state"], "blocked")
            self.assertIn("scheduled", result["detail"].lower())
            self.assertEqual(spy.calls, 0)

            with env.db.tx() as conn:
                conn.execute(
                    "UPDATE drafts SET scheduled_at=?, account_id='other-account' WHERE id=?",
                    (draft["draft"]["scheduled_at"], draft["draft"]["id"]),
                )
            result = publish.publish_approval(
                env.db, env.config, env.clock, draft["approval_id"], publisher=spy
            )
            self.assertEqual(result["state"], "blocked")
            self.assertIn("account", result["detail"].lower())
            self.assertEqual(spy.calls, 0)
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 0)
        finally:
            cleanup(env)

    def test_fingerprint_binds_every_field(self) -> None:
        # 逐个字段改动，验证指纹都会变。
        # 这保证“审批指纹”不是只看了正文，而是绑定了全部 8 个维度。
        base = dict(
            platform="xiaohongshu",
            account_id="xhs-main",
            draft_id=1,
            draft_version=1,
            body_sha256="a",
            images_sha256="b",
            scheduled_at="2026-11-05T12:00:00Z",
            expires_at="2026-11-06T12:00:00Z",
        )
        original = approval_fingerprint(**base)
        for key, value in {
            "platform": "other",
            "account_id": "other",
            "draft_id": 2,
            "draft_version": 2,
            "body_sha256": "c",
            "images_sha256": "d",
            "scheduled_at": "2026-11-05T13:00:00Z",
            "expires_at": "2026-11-06T13:00:00Z",
        }.items():
            changed = dict(base)
            changed[key] = value
            self.assertNotEqual(approval_fingerprint(**changed), original, key)

    def test_revision_is_queued_then_supersedes_old_approval_and_draft(self) -> None:
        # 用户发“修改 <code> ...”后，回调只排队，不调用模型；重复回复
        # 不会重复建 job。worker 处理 revise job 后才生成 v2。
        env = new_env("revise")
        try:
            from bot.worker import Worker

            draft = make_draft(env)
            code = draft["approval_code"]

            class BoomLLM:
                calls = 0

                def complete_json(self, **kwargs):  # pragma: no cover - must not run
                    self.calls += 1
                    raise AssertionError("callback must not call the model")

            boom = BoomLLM()
            result = handle_inbound_message(
                env.db,
                env.config,
                env.clock,
                message_id="revise-1",
                sender_id="owner-open-id",
                chat_id="owner-open-id",
                chat_type="p2p",
                text=f"修改 {code} 请精简正文",
                llm=boom,
            )
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["outcome"], "revision_queued")
            self.assertEqual(boom.calls, 0)
            self.assertEqual(
                env.db.scalar("SELECT COUNT(*) FROM jobs WHERE kind='revise'"), 1
            )
            replay = handle_inbound_message(
                env.db,
                env.config,
                env.clock,
                message_id="revise-1",
                sender_id="owner-open-id",
                chat_id="owner-open-id",
                chat_type="p2p",
                text=f"修改 {code} 请精简正文",
                llm=boom,
            )
            self.assertTrue(replay.get("duplicate"), replay)
            self.assertEqual(
                env.db.scalar("SELECT COUNT(*) FROM jobs WHERE kind='revise'"), 1
            )
            outcome = env.db.fetchone(
                "SELECT * FROM notifications WHERE dedupe_key='inbound:revise-1'"
            )
            self.assertEqual(outcome["kind"], "outcome")
            self.assertEqual(outcome["state"], "pending")

            worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            tick = worker.tick()
            self.assertTrue(any(j["kind"] == "revise" for j in tick["jobs"]), tick)
            old = get_approval(env.db, code=code)
            self.assertEqual(old["status"], "superseded")
            old_draft = env.db.fetchone(
                "SELECT * FROM drafts WHERE id=?", (draft["draft"]["id"],)
            )
            self.assertEqual(old_draft["status"], "superseded")
            new_pending = env.db.fetchall("SELECT * FROM approvals WHERE status='pending'")
            self.assertEqual(len(new_pending), 1)
            new_draft = env.db.fetchone(
                "SELECT * FROM drafts WHERE id=?", (new_pending[0]["draft_id"],)
            )
            self.assertEqual(int(new_draft["version"]), 2)
            self.assertEqual(new_draft["status"], "review_pending")
            self.assertNotEqual(new_pending[0]["code"], code)
            self.assertEqual(
                env.db.scalar(
                    "SELECT state FROM notifications WHERE dedupe_key='inbound:revise-1'"
                ),
                "sent",
            )
        finally:
            cleanup(env)

    def test_queued_revision_freezes_pending_approval_immediately(self) -> None:
        env = new_env("revise-freeze-pending")
        try:
            draft = make_draft(env)
            code = draft["approval_code"]

            result = handle_inbound_message(
                env.db,
                env.config,
                env.clock,
                message_id="revise-freeze-1",
                sender_id="owner-open-id",
                chat_id="owner-open-id",
                chat_type="p2p",
                text=f"修改 {code} 请缩短标题",
                channel="test",
            )
            self.assertEqual(result["outcome"], "revision_queued", result)
            self.assertEqual(
                get_approval(env.db, code=code)["status"], "revision_pending"
            )
            self.assertEqual(
                env.db.scalar(
                    "SELECT status FROM drafts WHERE id=?", (draft["draft"]["id"],)
                ),
                "revision_pending",
            )

            # The old code can no longer be approved or published.
            blocked = approve(env, code)
            self.assertFalse(blocked["ok"], blocked)
            self.assertEqual(blocked["outcome"], "rejected_revision_pending")
            self.assertEqual(
                get_approval(env.db, code=code)["status"], "revision_pending"
            )
            self.assertEqual(
                env.db.scalar("SELECT COUNT(*) FROM jobs WHERE kind='publish'"), 0
            )

            # A second, distinct message for the same approval must not add a job.
            duplicate = handle_inbound_message(
                env.db,
                env.config,
                env.clock,
                message_id="revise-freeze-2",
                sender_id="owner-open-id",
                chat_id="owner-open-id",
                chat_type="p2p",
                text=f"修改 {code} 再短一点",
                channel="test",
            )
            self.assertFalse(duplicate["ok"], duplicate)
            self.assertEqual(
                env.db.scalar("SELECT COUNT(*) FROM jobs WHERE kind='revise'"), 1
            )
        finally:
            cleanup(env)

    def test_queued_revision_cancels_an_approved_publish_gate(self) -> None:
        env = new_env("revise-freeze-approved")
        try:
            draft = make_draft(env)
            code = draft["approval_code"]
            approved = approve(env, code)
            self.assertEqual(approved["outcome"], "approved", approved)
            self.assertEqual(
                env.db.scalar("SELECT state FROM jobs WHERE kind='publish'"), "pending"
            )

            result = handle_inbound_message(
                env.db,
                env.config,
                env.clock,
                message_id="revise-freeze-approved-1",
                sender_id="owner-open-id",
                chat_id="owner-open-id",
                chat_type="p2p",
                text=f"修改 {code} 调整正文",
                channel="test",
            )
            self.assertEqual(result["outcome"], "revision_queued", result)
            self.assertEqual(
                get_approval(env.db, code=code)["status"], "revision_pending"
            )
            self.assertEqual(
                env.db.scalar("SELECT state FROM jobs WHERE kind='publish'"), "cancelled"
            )

            spy = CountingPublisher()
            blocked = publish.publish_approval(
                env.db, env.config, env.clock, draft["approval_id"], publisher=spy
            )
            self.assertEqual(blocked["state"], "blocked")
            self.assertEqual(spy.calls, 0)
            self.assertEqual(
                env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 0
            )

            from bot.worker import Worker

            worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            worker.tick()
            self.assertEqual(
                get_approval(env.db, code=code)["status"], "superseded"
            )
            self.assertEqual(
                env.db.scalar("SELECT COUNT(*) FROM approvals WHERE status='pending'"), 1
            )
            self.assertEqual(
                env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 0
            )
        finally:
            cleanup(env)

    def test_revision_failure_cannot_restore_approved_status(self) -> None:
        env = new_env("revise-failure-frozen")
        try:
            draft = make_draft(env)
            code = draft["approval_code"]
            approved = approve(env, code)
            self.assertEqual(approved["outcome"], "approved", approved)

            result = handle_inbound_message(
                env.db,
                env.config,
                env.clock,
                message_id="revise-failure-1",
                sender_id="owner-open-id",
                chat_id="owner-open-id",
                chat_type="p2p",
                text=f"修改 {code} 重新组织措辞",
                channel="test",
            )
            self.assertEqual(result["outcome"], "revision_queued", result)

            from bot.worker import Worker

            worker = Worker(env.config, clock=env.clock, db=env.db, http=env.http)
            with mock.patch(
                "bot.worker.revise_draft", side_effect=RuntimeError("model unavailable")
            ):
                tick = worker.tick()
            self.assertTrue(
                any("revision_failed" in job for job in tick["jobs"]), tick
            )
            # The failed revision leaves the old approval frozen, never approved.
            self.assertEqual(
                get_approval(env.db, code=code)["status"], "revision_pending"
            )
            self.assertEqual(
                env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 0
            )
            retry = approve(env, code)
            self.assertFalse(retry["ok"], retry)
        finally:
            cleanup(env)


class PublishLifecycleTests(unittest.TestCase):
    # 这一组关心“发布之后的状态”：simulated/unknown/failed/published
    # 分别应该怎么写库，以及重启后怎么恢复。
    def _approved(self, env):
        # 测试内快捷方式：写稿 + 批准，返回包含 approval_id 的草稿字典。
        draft = make_draft(env)
        approve(env, draft["approval_code"])
        return draft

    def test_mock_is_simulated_never_published(self) -> None:
        # MockPublisher 只能产生 simulated：
        # 没有 external_post_id、没有 URL、receipt 里明确写 simulated，
        # 草稿最终状态也是 simulated，绝不能是 published。
        env = new_env("mock")
        try:
            draft = self._approved(env)
            from bot.adapters.publishers import MockPublisher

            result = publish.publish_approval(
                env.db,
                env.config,
                env.clock,
                draft["approval_id"],
                publisher=MockPublisher(env.config, env.clock),
            )
            self.assertEqual(result["state"], "simulated")
            self.assertIsNone(result["external_post_id"])
            self.assertIsNone(result["external_url"])
            self.assertTrue(result["simulated"])
            attempt = env.db.fetchone("SELECT * FROM publish_attempts")
            self.assertEqual(attempt["state"], "simulated")
            self.assertEqual(attempt["mode"], "mock")
            self.assertIn("simulated", attempt["receipt_json"])
            self.assertEqual(
                env.db.scalar("SELECT status FROM drafts WHERE id=?", (draft["draft"]["id"],)),
                "simulated",
            )
        finally:
            cleanup(env)

    def test_unknown_is_not_retried(self) -> None:
        # 第一次发布返回 unknown；第二次调用直接 already_attempted，
        # 发布器调用次数保持 1，发布尝试也保持 1 条。
        env = new_env("unknown")
        try:
            draft = self._approved(env)
            unknown = CountingPublisher(PublishOutcome(state="unknown", detail="timeout"))
            result = publish.publish_approval(
                env.db, env.config, env.clock, draft["approval_id"], publisher=unknown
            )
            self.assertEqual(result["state"], "unknown")
            self.assertEqual(unknown.calls, 1)
            again = publish.publish_approval(
                env.db, env.config, env.clock, draft["approval_id"], publisher=unknown
            )
            self.assertEqual(again["state"], "already_attempted")
            self.assertEqual(unknown.calls, 1)
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 1)
            self.assertEqual(
                env.db.scalar(
                    "SELECT status FROM approvals WHERE id=?", (draft["approval_id"],)
                ),
                "publish_unknown",
            )
        finally:
            cleanup(env)

    def test_adapter_exception_becomes_unknown_and_is_not_retried(self) -> None:
        # 发布器直接抛 PublishUnknown（模拟“请求写出去之后连接断了”）。
        # 闸门应把它当作 unknown，并且第二次调用不再碰发布器。
        env = new_env("exception")
        try:
            draft = self._approved(env)

            class Exploding(CountingPublisher):
                def publish(self, request):
                    self.calls += 1
                    raise PublishUnknown("socket closed after request write")

            pub = Exploding()
            result = publish.publish_approval(
                env.db, env.config, env.clock, draft["approval_id"], publisher=pub
            )
            self.assertEqual(result["state"], "unknown")
            self.assertEqual(pub.calls, 1)
            publish.publish_approval(
                env.db, env.config, env.clock, draft["approval_id"], publisher=pub
            )
            self.assertEqual(pub.calls, 1)
        finally:
            cleanup(env)

    def test_published_without_receipt_is_downgraded_to_unknown(self) -> None:
        # 发布器“自称”published 但没给 post id/URL。
        # 闸门不信口头声明，降级成 unknown，草稿状态写 publish_unknown。
        env = new_env("no-receipt")
        try:
            draft = self._approved(env)
            liar = CountingPublisher(PublishOutcome(state="published", detail="trust me"))
            result = publish.publish_approval(
                env.db, env.config, env.clock, draft["approval_id"], publisher=liar
            )
            self.assertEqual(result["state"], "unknown")
            self.assertIsNone(result["external_post_id"])
            self.assertEqual(
                env.db.scalar(
                    "SELECT status FROM drafts WHERE id=?", (draft["draft"]["id"],)
                ),
                "publish_unknown",
            )
        finally:
            cleanup(env)

    def test_restart_after_claim_reconciles_without_resending(self) -> None:
        # 只做 claim（占坑），不调用发布器，然后模拟“进程重启”。
        # recover_interrupted_publishes 应把 publishing 改成 unknown，
        # 绝不重发；发布器调用次数必须是 0。
        env = new_env("restart")
        try:
            draft = self._approved(env)
            approval = get_approval(env.db, approval_id=draft["approval_id"])
            spy = CountingPublisher()
            claim, reason = publish.claim_publish(
                env.db, env.config, env.clock, int(approval["id"]), spy
            )
            self.assertIsNotNone(claim, reason)
            attempt = claim.attempt
            # Simulate a process restart: reopen the database and recover.
            reopened = Database(env.config.db_path)
            recovered = publish.recover_interrupted_publishes(reopened, env.clock)
            self.assertEqual(recovered, 1)
            self.assertEqual(spy.calls, 0)
            row = reopened.fetchone("SELECT * FROM publish_attempts")
            self.assertEqual(row["state"], "unknown")
            self.assertIn("restart", (row["reconcile_note"] or "").lower())
            self.assertEqual(
                reopened.scalar(
                    "SELECT status FROM approvals WHERE id=?", (approval["id"],)
                ),
                "publish_unknown",
            )
        finally:
            cleanup(env)

    def test_reconcile_requires_receipt_for_published(self) -> None:
        # 人工对账选“确认已发布”却不给回执 -> 必须报错；
        # 补上真实 post id/URL 后才接受，并把审批/事件都改成 published。
        env = new_env("reconcile")
        try:
            draft = self._approved(env)
            pub = CountingPublisher(PublishOutcome(state="unknown", detail="timeout"))
            publish.publish_approval(
                env.db, env.config, env.clock, draft["approval_id"], publisher=pub
            )
            with self.assertRaises(Exception):
                publish.reconcile_unknown(
                    env.db,
                    env.config,
                    env.clock,
                    approval_code=draft["approval_code"],
                    resolution="confirmed_published",
                    note="",
                )
            result = publish.reconcile_unknown(
                env.db,
                env.config,
                env.clock,
                approval_code=draft["approval_code"],
                resolution="confirmed_published",
                note="checked manually",
                external_post_id="note-123",
                external_url="https://www.xiaohongshu.com/explore/abc",
            )
            self.assertEqual(result["state"], "published")
            self.assertEqual(
                get_approval(env.db, code=draft["approval_code"])["status"], "published"
            )
            self.assertEqual(
                env.db.scalar(
                    "SELECT e.status FROM events e JOIN drafts d ON d.event_id=e.id WHERE d.id=?",
                    (draft["draft"]["id"],),
                ),
                "published",
            )
        finally:
            cleanup(env)


if __name__ == "__main__":
    unittest.main()

"""Concurrent publish claims must produce exactly one attempt."""

# =============================================================================
# 中文导读：两个线程同时抢发布，只能有一个成功
# =============================================================================
# 这是最容易出灾难性 bug 的地方：如果两个 worker 同时看到“审批已批准”，
# 都去调用发布器，就会发出两条一模一样的帖子。
#
# 测试做法：
#   - 用一个“慢发布器”把发布动作拖住 0.2 秒，制造重叠窗口；
#   - 两个线程用 Barrier 在同一瞬间放行；
#   - 断言发布器只被调用 1 次、数据库只有 1 条 publish_attempts，
#     另一个线程拿到 not_claimed/already_attempted/blocked 之一。
# 真正兜底的是 publish_attempts.dedupe_key 的唯一约束。
# =============================================================================

from __future__ import annotations

import threading
import time
import unittest

from bot.adapters.publishers import Publisher
from bot.adapters.types import PublishOutcome
from bot.services import publish
from bot.services.drafts import create_draft
from tests.helpers import approve, cleanup, default_item, make_draft, new_env


class SlowPublisher(Publisher):
    # 故意放慢的发布器。sleep 让两个线程的“占坑->调用发布器”窗口重叠，
    # 如果代码没有并发保护，这里就会看到 calls == 2。
    name = "slow-mock"
    mode = "mock"

    def __init__(self) -> None:
        self.calls = 0
        self.lock = threading.Lock()

    def publish(self, request):
        with self.lock:
            self.calls += 1
        time.sleep(0.2)
        return PublishOutcome(
            state="simulated",
            detail="concurrency test simulation",
            receipt={"simulated": True},
        )

    def health(self):
        return {"ok": True, "mode": self.mode}


class ConcurrentClaimTests(unittest.TestCase):
    def test_two_threads_publish_one_approval_at_most_once(self) -> None:
        env = new_env("concurrent")
        try:
            draft = make_draft(env)
            approve(env, draft["approval_code"])
            publisher = SlowPublisher()
            # Barrier(2)：两个线程都到齐后才同时往下走，
            # 比“先 start 再 sleep”更稳定地制造并发。
            barrier = threading.Barrier(2)
            results: list[dict] = []
            errors: list[BaseException] = []

            def run() -> None:
                try:
                    # 等到两个线程都准备好，再一起发起发布。
                    barrier.wait(timeout=5)
                    results.append(
                        publish.publish_approval(
                            env.db,
                            env.config,
                            env.clock,
                            draft["approval_id"],
                            publisher=publisher,
                        )
                    )
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            # 核心断言：发布器只被调用一次，数据库只有一条尝试。
            self.assertEqual(publisher.calls, 1)
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 1)
            attempt = env.db.fetchone("SELECT * FROM publish_attempts")
            self.assertEqual(attempt["state"], "simulated")
            states = {result.get("state") for result in results}
            # 一个线程成功（simulated），另一个必须是被挡下的结果。
            # 具体是 not_claimed / already_attempted / blocked 都算正常，
            # 因为两个线程的时序可能略有不同。
            self.assertIn("simulated", states)
            self.assertTrue(
                any(
                    result.get("state") in {"not_claimed", "already_attempted", "blocked"}
                    or not result.get("ok", True)
                    for result in results
                )
            )
        finally:
            cleanup(env)

    def test_two_distinct_approvals_race_for_one_daily_publish_quota(self) -> None:
        # 两个不同审批同时通过事务外预检时，每日限额必须在 claim 事务内
        # 重新计数，否则两边都会认为自己还有 1 个名额。
        env = new_env(
            "quota-race",
            items=[
                default_item(
                    title="开源模型 A 发布新版本支持长上下文",
                    url="https://example.com/quota-a",
                    summary="A 模型发布，支持更长上下文。",
                ),
                default_item(
                    title="完全不同的机器人硬件开始预售",
                    url="https://example.com/quota-b",
                    summary="B 硬件开始预售，面向开发者。",
                ),
            ],
            config_kwargs={"max_publish": 1},
        )
        try:
            env.ingest()
            env.queue()
            events = env.db.fetchall("SELECT * FROM events ORDER BY id")
            self.assertGreaterEqual(len(events), 2)
            drafts = [
                create_draft(
                    env.db, env.config, env.clock, env.llm, event_id=int(event["id"])
                )
                for event in events[:2]
            ]
            for draft in drafts:
                approved = approve(env, draft["approval_code"])
                self.assertTrue(approved["ok"], approved)

            publisher = SlowPublisher()
            barrier = threading.Barrier(2)
            results: list[dict] = []
            errors: list[BaseException] = []

            def run(approval_id: int) -> None:
                try:
                    barrier.wait(timeout=5)
                    results.append(
                        publish.publish_approval(
                            env.db,
                            env.config,
                            env.clock,
                            approval_id,
                            publisher=publisher,
                        )
                    )
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [
                threading.Thread(target=run, args=(int(draft["approval_id"]),))
                for draft in drafts
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            # 每日上限 1：只允许一次真实发布尝试，另一条必须被事务内拒绝。
            self.assertEqual(publisher.calls, 1)
            self.assertEqual(env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 1)
            self.assertEqual(
                sum(1 for result in results if result.get("state") == "simulated"), 1
            )
            states = {result.get("state") for result in results}
            self.assertTrue(states & {"blocked", "not_claimed"})
            self.assertEqual(
                env.db.scalar(
                    "SELECT COUNT(*) FROM approvals WHERE status='simulated'"
                ),
                1,
            )
            self.assertEqual(
                env.db.scalar("SELECT COUNT(*) FROM approvals WHERE status='approved'"),
                1,
            )
        finally:
            cleanup(env)


if __name__ == "__main__":
    unittest.main()

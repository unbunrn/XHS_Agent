#!/usr/bin/env python
"""Offline fault-injection demo for the publication recovery agent.

This script never touches the live database, bridge, or platform.  It creates a
temporary database, publishes through a fake publisher that first returns
`unknown`, then simulates the owner confirming “not published” and retries with
a successful fake publish.  Output is redacted JSONL on stdout.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from bot.adapters.types import PublishOutcome
from bot.services.approvals import handle_inbound_message
from bot.services.publication_agent.live import LivePublicationRuntime
from bot.services.publication_agent.runner import PublicationRunner
from tests.helpers import approve, cleanup, make_draft, new_env
from tests.test_publication_agent import FakePublisher, ScriptedLLM, _publication_config


def emit(kind: str, **data) -> None:
    payload = {"event": kind, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    payload.update(data)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jsonl", action="store_true", help="emit JSONL (default)")
    parser.add_argument("--keep", action="store_true", help="keep the temporary workspace")
    args = parser.parse_args()

    env = new_env("publication-demo")
    started = time.time()
    try:
        made = make_draft(env)
        _publication_config(env)
        approval = made["approval"]
        approve(env, approval["code"])
        task = env.db.fetchone("SELECT * FROM publication_tasks")
        emit("approval_created", approval_code=approval["code"], task=task["code"])

        unknown = LivePublicationRuntime(
            env.db, env.config, env.clock,
            publisher=FakePublisher(PublishOutcome(state="unknown", detail="simulated timeout")),
        )
        runner = PublicationRunner(
            env.db, env.config, env.clock,
            ScriptedLLM([{"action": "publish_approved_artifact", "args": {}}]),
            unknown,
        )
        first = runner.run(int(task["id"]))
        emit("attempt_unknown", task=task["code"], state=first.state)

        reply = handle_inbound_message(
            env.db, env.config, env.clock,
            message_id="demo-confirm-not-published",
            sender_id="owner-open-id", chat_id="owner-open-id", chat_type="p2p",
            text=f"确认未发布并重试 {approval['code']}", channel="test",
        )
        emit("owner_confirmed_not_published", task=task["code"], outcome=reply.get("outcome"))

        success = LivePublicationRuntime(env.db, env.config, env.clock, publisher=FakePublisher())
        runner2 = PublicationRunner(
            env.db, env.config, env.clock,
            ScriptedLLM([{"action": "publish_approved_artifact", "args": {"retry_basis": "demo"}}]),
            success,
        )
        second = runner2.run(int(task["id"]))
        emit(
            "attempt_published",
            task=task["code"],
            state=second.state,
            post_count=int(success.publisher.calls),
            model_calls=env.db.scalar("SELECT COUNT(*) FROM publication_agent_steps"),
            notifications=env.db.scalar("SELECT COUNT(*) FROM notifications"),
            duration_s=round(time.time() - started, 3),
        )
        emit("done", ok=second.state == "published")
        return 0 if second.state == "published" else 1
    finally:
        if args.keep:
            print(json.dumps({"event": "workspace_kept", "path": str(Path(env.root))}))
        else:
            cleanup(env)


if __name__ == "__main__":
    raise SystemExit(main())

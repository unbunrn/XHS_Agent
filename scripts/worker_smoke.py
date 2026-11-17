"""Worker end-to-end smoke test on a throwaway DB."""

# =============================================================================
# 中文导读：Worker 端到端冒烟测试
# =============================================================================
# 用一次性的 var/smoke-worker 数据库跑完整流程：
# 启动 -> 连续 tick -> 生成草稿/审批 -> 批准 -> 再 tick 触发模拟发布。
# 每次运行都会先删掉这个目录，所以不会污染真实数据库。
# 冻结时钟让“计划发布时间”可控：批准后手动前进 5 秒，发布才到点。
# =============================================================================

from __future__ import annotations

import os
import shutil
from pathlib import Path

from bot.config import load_config
from bot.timeutil import FrozenClock
from bot.worker import Worker
from bot.services.approvals import handle_inbound_message

root = Path.cwd()
tmp = root / "var" / "smoke-worker"
# 只删冒烟专用目录；rmtree 前请确认路径没有写错。
shutil.rmtree(tmp, ignore_errors=True)
tmp.mkdir(parents=True)
# 必须在 load_config 之前设置：配置加载时会读取这个环境变量，
# 把数据库指向一次性文件，而不是真实的 var/bot.sqlite3。
os.environ["BOT_DB_PATH"] = str(tmp / "bot.sqlite3")
config = load_config(require_file=True)
clock = FrozenClock()
worker = Worker(config, clock=clock)
print("startup", worker.startup())
# 连续跑三个周期：通常第一个周期采集、后面的周期研究/写稿。
# 任务有租约和退避，所以多跑几次比只跑一次更接近真实运行。
for i in range(3):
    print("tick", i, worker.tick())
rows = worker.db.fetchall("SELECT id,status,title,body_sha256 FROM drafts")
print("drafts", [dict(r) for r in rows])
approvals = worker.db.fetchall("SELECT id,code,status,fingerprint FROM approvals")
print("approvals", [dict(r) for r in approvals])
if approvals:
    a = approvals[0]
    # 取出第一条审批，用 demo owner 身份批准。
    result = handle_inbound_message(
        worker.db,
        config,
        clock,
        message_id="worker-approve-1",
        sender_id="demo-owner",
        chat_id="demo-owner",
        chat_type="p2p",
        text=f"批准 {a['code']}",
    )
    print("approve", result)
    # 冻结时钟不会自己走。批准时 scheduled_at 通常已经是“现在”，
    # 但为了避开边界相等的情况，手动前进 5 秒再让 worker tick。
    clock.advance(seconds=5)
    print("tick publish", worker.tick())
    # 这里重点看三件事：attempt 的 state（应为 simulated）、
    # dedupe_key（应为 approval:<id>）、draft 的最终状态。
    attempts = worker.db.fetchall("SELECT id,state,mode,external_post_id,external_url,dedupe_key FROM publish_attempts")
    print("attempts", [dict(r) for r in attempts])
    states = worker.db.fetchall("SELECT id,status FROM drafts")
    print("draft status", [dict(r) for r in states])
    print("receipt", worker.db.fetchone("SELECT receipt_json FROM publish_attempts"))
print("tmp", tmp)

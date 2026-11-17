"""Fully local P0 demonstration: fixture -> research -> draft -> PNG -> approval
-> simulated publish, with a restart between approval and publishing.

Nothing here contacts a real platform.  The output deliberately says
"simulated", and the demo refuses to use a real publisher even if the ambient
configuration is live.
"""

# =============================================================================
# 中文导读：一条命令跑完整个离线闭环
# =============================================================================
# 这个文件是给第一次接触项目的人看的“活说明书”。它按顺序演示：
#   1. 从内置 fixture 采集（不联网）；
#   2. 去重、聚类、评分、写稿（假模型）；
#   3. 渲染 PNG；
#   4. 生成本地审批通知；
#   5. 模拟一次进程重启，证明待审批不会丢；
#   6. 本机 owner 批准；
#   7. 发布门检查后由 MockPublisher 写“模拟导出”；
#   8. 再重启一次，证明不会重复发布。
#
# 为什么演示要强制覆盖配置？
#   用户可能拿真实配置来跑 demo。如果沿用 live 设置，就有误发风险。
#   所以 _demo_config 把模式、模型、通知、发布器、数据目录、来源全部
#   改成演示专用，并且使用空的 Secrets——即使环境变量里有真密钥，
#   demo 也拿不到、不会用。
#
# 演示输出里会反复出现“simulated / 模拟导出 / 不是真实发布”，
# 这是故意的：不能让任何人把演示结果误当成真的发过小红书。
# =============================================================================

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .db import Database
from .services.approvals import configured_owner, handle_inbound_message
from .timeutil import SystemClock, local_iso
from .worker import Worker


class DemoError(RuntimeError):
    # 演示流程没有按预期发展时抛出，例如没有生成草稿、
    # 重启后审批丢了、出现了重复发布尝试。
    pass


def _demo_config(config: Config) -> Config:
    # 关键：deepcopy 一份配置再改，绝不修改调用方传进来的真实配置对象。
    data = copy.deepcopy(config.data)
    data.setdefault("app", {})
    data["app"]["mode"] = "offline"
    # 所有产物都写到 var/demo，和真实运行数据（var/bot.sqlite3 等）隔离。
    # 这样演示可以随便重置，不会碰到你的真实审批和发布记录。
    data["app"]["data_dir"] = "var/demo"
    data["app"]["db_path"] = "var/demo/bot.sqlite3"
    data["app"]["log_dir"] = "var/demo/logs"
    data["llm"] = {**data.get("llm", {}), "provider": "fake"}
    # 通知也用 fake：只写本地 JSON，不会给任何人发飞书消息。
    # demo-owner 是一个虚构的 owner open_id，仅用于演示 owner 校验路径。
    data["notify"] = {
        **data.get("notify", {}),
        "provider": "fake",
        "lark": {
            **data.get("notify", {}).get("lark", {}),
            "owner_open_id": "demo-owner",
            "chat_id": "",
            "require_p2p": True,
        },
    }
    data["publisher"] = {
        **data.get("publisher", {}),
        # 强制 mock，并再次关闭 live_enabled。
        # 即使环境变量 BOT_ALLOW_REAL_PUBLISH=1，demo 也不会创建真实发布器。
        "provider": "mock",
        "live_enabled": False,
        "export_dir": "var/demo/exports",
    }
    data["render"] = {**data.get("render", {}), "output_dir": "var/demo/drafts"}
    data["notify"]["fake_dir"] = "var/demo/notifications"
    # The demo owns its source list, so a live config cannot accidentally pull
    # real feeds or reuse a fixture from somewhere else.
    # 中文：来源列表也由 demo 完全接管，只留两个本地 fixture。
    # 这样即使用户配置里有真实 RSS，演示也不会去联网采集。
    data["sources"] = [
        {
            "name": "Demo AI News",
            "url": "fixtures/demo_feed.json",
            "kind": "fixture",
            "trust_tier": 2,
            "enabled": True,
        },
        {
            "name": "Demo Vendor",
            "url": "fixtures/vendor_feed.json",
            "kind": "fixture",
            "trust_tier": 1,
            "enabled": True,
        },
    ]
    from .config import Secrets

    # 返回一个全新的 Config，并传入空的 Secrets：
    # 演示进程完全看不到环境变量或 secrets.toml 里的真实密钥。
    return Config(data=data, root=config.root, path=config.path, secrets=Secrets())


def run_demo(config_path: str | Path | None = None, *, reset: bool = True) -> dict[str, Any]:
    # 先加载“环境里的配置”，只取它的 root/path 等位置信息，
    # 随后立刻用 _demo_config 替换掉所有运行参数。
    ambient = load_config(config_path)
    config = _demo_config(ambient)
    demo_dir = config.root / "var" / "demo"
    # reset 只会删除 var/demo 这个演示专用目录，不会碰真实数据。
    # 如果你手动把数据放在别处，demo 也看不到。
    if reset and demo_dir.exists():
        shutil.rmtree(demo_dir)
    demo_dir.mkdir(parents=True, exist_ok=True)
    print("=" * 72)
    print("本地离线演示（默认不需要任何外部账号）")
    print("来源=fixture  模型=fake  通知=本地文件  发布=mock 模拟导出")
    print("注意：模拟发布不是真实小红书发布，数据库只会写入 simulated。")
    print("=" * 72)

    clock = SystemClock()
    # Worker 负责所有后台步骤。startup() 会建库、恢复中断状态、
    # 安排第一次采集；tick() 执行到期的任务。
    worker = Worker(config, clock=clock)
    startup = worker.startup()
    print(f"1) 启动/恢复: {startup}")
    tick1 = worker.tick()
    print(f"2) 采集/研究/生成: {tick1}")

    # tick 之后，数据库里应该有最新草稿和对应审批。
    # 这里直接查库验证，而不是相信打印结果。
    draft = worker.db.fetchone("SELECT * FROM drafts ORDER BY id DESC LIMIT 1")
    approval = worker.db.fetchone("SELECT * FROM approvals ORDER BY id DESC LIMIT 1")
    images = worker.db.fetchall(
        "SELECT * FROM draft_images WHERE draft_id=? ORDER BY ordinal", (draft["id"],)
    ) if draft else []
    if draft is None or approval is None:
        raise DemoError("演示没有生成草稿和审批，请查看上面的任务输出")
    print(f"   草稿 #{int(draft['id'])} v{int(draft['version'])}：{draft['title']}")
    print(f"   审批码：{approval['code']}  状态={approval['status']}")
    print(f"   正文 SHA256：{draft['body_sha256']}")
    print(f"   图片 SHA256：{draft['images_sha256']}")
    for image in images:
        print(f"   PNG：{image['path']}  {image['width']}x{image['height']}  sha256={image['sha256']}")
    print("   通知文件：", worker.db.fetchone("SELECT payload_json FROM notifications ORDER BY id DESC LIMIT 1")["payload_json"][:160], "…")

    # Restart before the decision: a pending approval must survive.
    # 中文：模拟“审批还没做，程序重启了”。新建一个 Worker 访问同一个数据库，
    # 然后确认审批仍然是 pending——这是持久化的基本要求。
    restarted = Worker(config, clock=clock)
    restarted.startup()
    still = restarted.db.fetchone("SELECT status,code FROM approvals WHERE id=?", (approval["id"],))
    print(f"3) 重启恢复后审批仍为: {still['status']} {still['code']}")
    if still["status"] != "pending":
        raise DemoError("重启后待审批状态丢失")

    owner = configured_owner(config)
    if not owner:
        raise DemoError("demo owner 未配置")
    # 批准走的是正式审批服务，不是直接改数据库。
    # message_id 固定为 demo-owner-approval-1：即使演示重跑，
    # 同一条消息也只会被处理一次。
    decision = handle_inbound_message(
        restarted.db,
        config,
        clock,
        message_id="demo-owner-approval-1",
        sender_id=owner,
        chat_id=owner,
        chat_type="p2p",
        text=f"批准 {approval['code']}",
    )
    print(f"4) 本地管理员批准: {decision}")
    # 再 tick 一次，worker 会创建发布任务并经过发布闸门。
    # 因为 publisher 是 mock，最终状态应为 simulated。
    tick2 = restarted.tick()
    print(f"5) 发布门/模拟发布: {tick2}")

    attempt = restarted.db.fetchone(
        "SELECT * FROM publish_attempts WHERE approval_id=? ORDER BY id DESC LIMIT 1",
        (approval["id"],),
    )
    # 发布尝试必须存在，否则说明批准后的任务没有跑通。
    if attempt is None:
        raise DemoError("批准后没有产生发布尝试")
    published_draft = restarted.db.fetchone("SELECT status FROM drafts WHERE id=?", (draft["id"],))
    print(f"   发布尝试状态: {attempt['state']}  模式: {attempt['mode']}")
    print(f"   草稿状态: {published_draft['status']}")
    receipt = json.loads(attempt["receipt_json"] or "{}")
    print(f"   模拟回执 manifest: {receipt.get('manifest_path')}")
    print("   外部 post_id/URL：无（模拟，不会伪造真实回执）")

    # Restart after the attempt: no duplicate attempt, no claim of real publish.
    # 中文：发布之后再重启一次。这里要验证两条底线：
    #   1. 同一个审批仍然只有一条发布尝试（绝不重发）；
    #   2. mock 的结果绝不是 published（不伪造真实回执）。
    after = Worker(config, clock=clock)
    after.startup()
    after.tick()
    count = after.db.scalar(
        "SELECT COUNT(*) FROM publish_attempts WHERE approval_id=?", (approval["id"],), 0
    )
    print(f"6) 再次重启后，该审批的发布尝试数量: {count}")
    if int(count) != 1:
        raise DemoError("重启造成了重复发布尝试")
    if attempt["state"] == "published":
        raise DemoError("mock publisher 不得写入 published")

    summary = {
        # 这份 summary 可以直接贴到 issue 或日志里，
        # 其中的 simulated=True 明确说明全程没有真实发布。
        "demo": True,
        "simulated": True,
        "mode": config.mode,
        "draft_id": int(draft["id"]),
        "approval_code": approval["code"],
        "approval_status_after_restart": still["status"],
        "attempt_state": attempt["state"],
        "attempt_mode": attempt["mode"],
        "attempt_count": int(count),
        "external_post_id": attempt["external_post_id"],
        "external_url": attempt["external_url"],
        "images": [str(image["path"]) for image in images],
        "db": str(config.db_path),
        "exports": str(config.resolve_path(str(config.get("publisher.export_dir")))),
    }
    print("-" * 72)
    print("演示结论：本地闭环完成；模拟发布不会被写成真实成功。")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main(config_path: str | None = None) -> int:
    # 供 python -m bot.demo 调用；异常会向上抛，由外层决定怎么显示。
    run_demo(config_path)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

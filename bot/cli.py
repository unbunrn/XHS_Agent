"""Command line entry point.

Examples:
    python -m bot doctor
    python -m bot demo
    python -m bot tick
    python -m bot worker
    python -m bot console
    python -m bot approve XH-ABC123
"""

# =============================================================================
# 中文导读：命令行入口——人跟机器人说话的地方
# =============================================================================
# 这个文件本身几乎不做事，只做三件事：
#   1. 解析你在终端敲的命令（init/status/approve/...）；
#   2. 读取配置、打开数据库；
#   3. 把活交给真正的模块（worker、console、publish、approvals...）。
#
# 重要：命令行不是“后门”。
#   - approve/reject/revise 走的是和飞书消息完全相同的
#     handle_inbound_message，同样要过 owner 校验、审批码、有效期检查；
#   - publish-due 也只是“手动触发一次到点发布”，
#     发布前仍然会跑完整的 validate_publish 检查；
#   - reconcile 只做人工对账，永远不会重发。
#
# 命令大致分四组：
#   日常运行：init、status、tick、worker、console
#   检查排错：doctor、demo、backup
#   审批操作：approve、reject、revise、reconcile
#   配置辅助：configure、lark
# =============================================================================

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from .config import Config, ConfigError, load_config
from .db import Database
from .doctor import run_doctor
from .jobs import enqueue_job
from .services.approvals import (
    configured_owner,
    get_approval,
    handle_inbound_message,
)
from .services.publish import publish_due, reconcile_unknown
from .timeutil import SystemClock, to_iso


def _config(args: argparse.Namespace) -> Config:
    # 统一的配置加载入口。load_config 会处理默认路径
    # config/bot.toml、环境变量覆盖和 secrets 合并。
    return load_config(args.config)


def _init_db(config: Config) -> Database:
    # 打开数据库并确保表结构/迁移已就绪。
    # init() 是幂等的：已经初始化过再调一次也不会出问题，
    # 所以每个命令都可以放心地先调它。
    db = Database(config.db_path)
    db.init()
    return db


def cmd_init(args: argparse.Namespace) -> int:
    config = _config(args)
    db = _init_db(config)
    # 局部导入：避免 cli 模块在启动时就把整个研究服务链条加载进来，
    # 也能减少模块之间的循环依赖风险。
    from .services.research import sync_sources

    # 把配置里的 [[sources]] 同步进数据库：新增的插入，已有名字的更新。
    count = sync_sources(db, config)
    print(f"数据库已初始化: {config.db_path}")
    print(f"已同步 {count} 个来源")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    # 只读汇总：把各张表按状态计数，方便一眼看出卡在哪一步。
    # 这个命令不修改任何数据，可以随时安全运行。
    config = _config(args)
    db = _init_db(config)
    approvals = db.fetchall(
        "SELECT status, COUNT(*) n FROM approvals GROUP BY status ORDER BY status"
    )
    drafts = db.fetchall("SELECT status, COUNT(*) n FROM drafts GROUP BY status ORDER BY status")
    events = db.fetchall("SELECT status, COUNT(*) n FROM events GROUP BY status ORDER BY status")
    attempts = db.fetchall(
        "SELECT state, mode, COUNT(*) n FROM publish_attempts GROUP BY state, mode ORDER BY state"
    )
    jobs = db.fetchall("SELECT state, COUNT(*) n FROM jobs GROUP BY state ORDER BY state")
    results = db.fetchall("SELECT state, COUNT(*) n FROM publish_results GROUP BY state ORDER BY state")
    from .budget import BudgetGuard

    budget = BudgetGuard(db, config, SystemClock()).snapshot()
    print(f"模式: {config.mode}  demo={config.demo_pipeline}  "
          f"真实发布已武装={config.is_live_publish_armed()}")
    print(f"数据库: {config.db_path}")
    print(f"账号: {config.account().get('id')} enabled={config.account().get('enabled')} "
          f"status={config.account().get('status')}")
    print("事件:", dict((r["status"], r["n"]) for r in events) or "无")
    print("草稿:", dict((r["status"], r["n"]) for r in drafts) or "无")
    print("审批:", dict((r["status"], r["n"]) for r in approvals) or "无")
    print("发布尝试:", [dict(r) for r in attempts] or "无")
    print("发布结果:", dict((r["state"], r["n"]) for r in results) or "无")
    print("任务:", dict((r["state"], r["n"]) for r in jobs) or "无")
    print(f"预算: {budget.used_usd:.4f}/{budget.daily_usd:.4f} USD, "
          f"calls={budget.calls}/{budget.max_calls} paused={budget.paused}")
    unknown = db.fetchall(
        "SELECT id,approval_id,draft_id,error,reconcile_note,created_at FROM publish_attempts "
        "WHERE state='unknown' ORDER BY id DESC"
    )
    # publish_unknown 是最需要人关注的：可能已经发到平台了。
    # 这里把它们单独列出来，提醒用户去平台核对后用 reconcile 处理。
    if unknown:
        print("需要人工核对的 publish_unknown:")
        for row in unknown:
            print(f"  attempt #{row['id']} approval={row['approval_id']} draft={row['draft_id']} "
                  f"{row['created_at']} {row['error'] or row['reconcile_note']}")
    pending = db.fetchall(
        "SELECT code,expires_at,draft_id FROM approvals WHERE status='pending' ORDER BY id"
    )
    # 待审批的单独打印审批码，方便直接复制到 approve/reject 命令里。
    for row in pending:
        print(f"待审批: {row['code']} draft=#{row['draft_id']} expires={row['expires_at']}")
    return 0


def cmd_tick(args: argparse.Namespace) -> int:
    # 跑一个工作周期就退出：启动恢复 -> 处理到期任务 -> 打印结果。
    # 适合调试或“手动推进一下”，常驻运行请用 worker。
    config = _config(args)
    from .worker import Worker

    worker = Worker(config)
    startup = worker.startup()
    print(f"启动: {startup}")
    result = worker.tick(limit=args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    # 交给 worker.main：它会安装信号处理、循环 tick、优雅退出。
    # 这里不重复实现循环，避免两套逻辑不一致。
    from .worker import main as worker_main

    return worker_main(args.config)


def cmd_console(args: argparse.Namespace) -> int:
    # 启动本机审批控制台（只监听回环地址）。
    from .console import main as console_main

    return console_main(args.config)


def cmd_doctor(args: argparse.Namespace) -> int:
    # 体检命令。run_doctor 返回一份报告，overall == "fail" 时
    # 用退出码 1 告诉脚本“有问题”，否则 0。
    # 这样健康检查脚本不需要解析输出文字。
    config = _config(args)
    report = run_doctor(config, json_output=args.json)
    return 1 if report["overall"] == "fail" else 0


def cmd_demo(args: argparse.Namespace) -> int:
    # 完整离线演示：采集 -> 写稿 -> 渲染 -> 模拟发布，不需要任何外部账号。
    # 演示失败时把异常打印到 stderr 并返回 1，
    # 不把 Python 堆栈直接糊到用户脸上。
    from .demo import run_demo

    try:
        run_demo(args.config)
    except Exception as exc:  # noqa: BLE001
        print(f"演示失败: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    # 备份 SQLite 数据库。db.backup_to 使用 SQLite 自带的在线备份接口，
    # 即使 worker 正在写，也能得到一致的副本，而不是简单复制文件。
    config = _config(args)
    db = _init_db(config)
    target = Path(args.output) if args.output else config.data_dir / "backups" / "bot.sqlite3"
    if not target.is_absolute():
        target = config.root / target
    db.backup_to(target)
    print(f"备份写入: {target}")
    return 0


def _decide(args: argparse.Namespace, action: str) -> int:
    # 审批/拒绝/修改三个命令共用这个函数。
    # 它故意不直接改数据库，而是拼出一条“像用户从飞书发来的消息”，
    # 交给 handle_inbound_message 走完整流程。这样 CLI 和飞书的行为永远一致，
    # 不会出现“命令行能批准、飞书不能”的双重标准。
    config = _config(args)
    db = _init_db(config)
    approval = get_approval(db, code=args.code)
    if approval is None:
        # 先友好地报告找不到，避免后续流程抛一个难懂的空值异常。
        print(f"找不到审批码 {args.code}", file=sys.stderr)
        return 2
    owner = configured_owner(config)
    if not owner:
        # 没有配置 owner 就不允许做决定。命令行不等于“超级管理员”，
        # 否则任何能运行程序的人都能绕过 owner 绑定批准内容。
        print("未配置 owner open_id，不能做出决定", file=sys.stderr)
        return 2
    # 拼成和飞书里一样的命令文本：动作词 + 审批码 +（可选）修改意见。
    text = f"{action} {approval['code']} {getattr(args, 'instruction', '') or ''}".strip()
    result = handle_inbound_message(
        db,
        config,
        SystemClock(),
        # 每条 CLI 调用生成一个唯一的 message_id，作为幂等键：
        # 同一条消息重复投递时，handle_inbound_message 只会处理一次。
        message_id=f"cli-{uuid.uuid4().hex}",
        sender_id=owner,
        chat_id=owner,
        chat_type="p2p",
        text=text,
        channel="cli",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def cmd_approve(args: argparse.Namespace) -> int:
    # 三个薄壳：只是动作词不同，真正的校验逻辑全部复用 _decide。
    return _decide(args, "批准")


def cmd_reject(args: argparse.Namespace) -> int:
    return _decide(args, "拒绝")


def cmd_revise(args: argparse.Namespace) -> int:
    return _decide(args, "修改")


def cmd_reconcile(args: argparse.Namespace) -> int:
    # 人工核对 publish_unknown。resolution 由命令行参数限定为三种：
    # confirmed_published / confirmed_not_published / still_unknown。
    # 这个命令只记录人的判断，绝不重新发布。
    config = _config(args)
    db = _init_db(config)
    approval = get_approval(db, code=args.code)
    if approval is None:
        print("找不到审批码", file=sys.stderr)
        return 2
    try:
        result = reconcile_unknown(
            db,
            config,
            SystemClock(),
            approval_code=approval["code"],
            resolution=args.resolution,
            note=args.note or "",
            external_post_id=args.post_id,
            external_url=args.url,
            actor="cli-owner",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"核对失败: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_results(args: argparse.Namespace) -> int:
    # Read-only: the durable answer to “did it publish?” independent of jobs.
    config = _config(args)
    db = _init_db(config)
    from .services.publication_results import list_results, result_label

    rows = list_results(db, limit=args.limit)
    if not rows:
        print("暂无发布结果记录")
        return 0
    for row in rows:
        print(
            f"#{row['id']} {row['approval_code']} draft=#{row['draft_id']} "
            f"{result_label(row['state'])} stage={row['stage']} "
            f"reason={row['reason_code'] or '-'} time={row['created_at']}"
        )
        if row["external_post_id"] or row["external_url"]:
            print(f"   平台回执: {row['external_post_id'] or '-'} {row['external_url'] or ''}")
        if row["detail"]:
            print(f"   说明: {row['detail']}")
    return 0


def cmd_refresh_topic(args: argparse.Namespace) -> int:
    # Explicit, owner-requested re-research of a terminal blocked/failed topic.
    # This is not an automatic retry: it keeps every old diagnostic row, clears
    # only the request's linkage to the old research, and enqueues one new job
    # with refresh=True so cached bodies are not silently reused.
    config = _config(args)
    db = _init_db(config)
    row = db.fetchone("SELECT * FROM topic_requests WHERE code=?", (args.code,))
    if row is None:
        print("找不到选题码", file=sys.stderr)
        return 2
    if row["status"] not in {"blocked", "failed"}:
        print(f"选题 {row['code']} 当前状态为 {row['status']}，只能刷新 blocked/failed 请求", file=sys.stderr)
        return 2
    if row["draft_id"] or row["approval_id"]:
        print("该选题已经生成草稿或审批，不能通过刷新绕过审批；请发起新的选题", file=sys.stderr)
        return 2
    clock = SystemClock()
    with db.tx() as conn:
        changed = conn.execute(
            "UPDATE topic_requests SET status='queued', error=NULL, progress=?, "
            "result_code=NULL, finished_at=NULL, event_id=NULL, evidence_ids_json=NULL, "
            "updated_at=? WHERE id=? AND status IN ('blocked','failed')",
            ("等待显式重新检索（refresh）：不复用旧缓存正文", to_iso(clock.now()), int(row["id"])),
        ).rowcount
    if changed != 1:
        print("选题状态已变化，未刷新", file=sys.stderr)
        return 1
    job_id = enqueue_job(
        db,
        "topic",
        f"topic:{int(row['id'])}:refresh:{uuid.uuid4().hex[:10]}",
        payload={"request_id": int(row["id"]), "refresh": True},
        priority=5,
        max_attempts=2,
        clock=clock,
    )
    with db.tx() as conn:
        conn.execute(
            "UPDATE topic_requests SET job_id=?, updated_at=? WHERE id=?",
            (int(job_id), to_iso(clock.now()), int(row["id"])),
        )
    print(f"已排队显式重新检索：{row['code']} (job {job_id}, refresh=True)")
    return 0


def cmd_reconcile_readonly(args: argparse.Namespace) -> int:
    # Safe default: no evidence provider is configured.  This records one
    # bounded check and keeps the attempt unknown; it never proves “not posted”.
    config = _config(args)
    db = _init_db(config)
    from .services.reconcile_readonly import read_only_reconcile

    try:
        result = read_only_reconcile(
            db, config, SystemClock(), approval_code=args.code, provider=None
        )
    except Exception as exc:  # noqa: BLE001
        print(f"只读对账失败: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_publish_due(args: argparse.Namespace) -> int:
    # 手动触发一次“发布所有到点的审批”。
    # 注意：这不是跳过检查的快捷方式。publish_due 内部仍然会对每一条
    # 执行完整的 validate_publish + claim_publish，任何一项不满足都不会发。
    config = _config(args)
    from .adapters.publishers import build_publisher
    from .adapters.types import NotConfigured

    db = _init_db(config)
    try:
        publisher = build_publisher(config, SystemClock())
    except NotConfigured as exc:
        # live 开关没全开、token 缺失等，都从这里给出明确原因。
        print(f"发布适配器未配置: {exc}")
        return 1
    result = publish_due(db, config, SystemClock(), publisher=publisher)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_gateway(args: argparse.Namespace) -> int:
    # 启动飞书长连接：先打印健康状态，再开始接收审批消息。
    # 没装 SDK / 没配凭据时 gateway.start() 抛 NotConfigured，
    # 这里转成友好提示并返回 1。
    config = _config(args)
    db = _init_db(config)
    from .lark_gateway import LarkLongConnection
    from .adapters.types import NotConfigured

    gateway = LarkLongConnection(config, db)
    print(json.dumps(gateway.health(), ensure_ascii=False, indent=2))
    try:
        gateway.start()
    except NotConfigured as exc:
        print(f"飞书长连接未启动: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_gateway_state(args: argparse.Namespace) -> int:
    # Read-only plan used by start/stop/health PowerShell scripts.  It never
    # opens a connection, reads no secret values, and only reports presence.
    config = _config(args)
    state = config.lark_gateway_state()
    text = json.dumps(state, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        if not output.is_absolute():
            output = config.root / output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


def _toml_string(value: str) -> str:
    # 用 json.dumps 生成带引号的 TOML 字符串字面量。
    # JSON 的字符串转义规则与 TOML 基本兼容，ensure_ascii=False
    # 保证中文直接写入而不是变成 \uXXXX。
    return json.dumps(value, ensure_ascii=False)


def cmd_configure(args: argparse.Namespace) -> int:
    """Write a non-secret config skeleton; secrets remain environment-only."""
    # 中文：生成一个“可以照着改”的配置骨架。原则：
    #   - 只写非密钥配置；
    #   - 密钥一律用环境变量或 config/secrets.toml；
    #   - offline 模式默认 fake LLM / 本地通知 / mock 发布，开箱即可演示；
    #   - live 模式默认选 DeepSeek / 飞书 / 小红书桥，但 live_enabled
    #     仍然是 false，需要用户再显式打开。
    config = _config(args)
    output = Path(args.output)
    if not output.is_absolute():
        output = config.root / output
    if output.exists() and not args.force:
        # 覆盖保护：配置里可能有用户手改的内容，必须先加 --force。
        print(f"{output} 已存在；使用 --force 覆盖（不会碰 config/bot.toml，除非你显式指定）", file=sys.stderr)
        return 2
    mode = args.mode
    lines = [
        "# Generated by `python -m bot configure`.",
        "# Secrets are NOT stored here. Use environment variables or config/secrets.toml.",
        "",
        "[app]",
        f"mode = {_toml_string(mode)}",
        'data_dir = "var"',
        'db_path = "var/bot.sqlite3"',
        "",
        "[account]",
        f"id = {_toml_string(args.account)}",
        f"display_name = {_toml_string(args.display_name)}",
        "platform = \"xiaohongshu\"",
        "enabled = false",
        "status = \"unverified\"",
        "",
        f"[llm]",
        f"provider = {_toml_string('deepseek' if mode == 'live' else 'fake')}",
        'model = "deepseek-flash"',
        'base_url = "https://api.deepseek.com"',
        "",
        "[notify]",
        f"provider = {_toml_string('lark' if mode == 'live' else 'fake')}",
        "",
        "[notify.lark]",
        'owner_open_id = ""',
        'chat_id = ""',
        "require_p2p = true",
        "",
        "[publisher]",
        f"provider = {_toml_string('xiaohongshu' if mode == 'live' else 'mock')}",
        "live_enabled = false",
        'export_dir = "var/exports"',
        "",
        "[publisher.xiaohongshu]",
        'base_url = "http://127.0.0.1:18060"',
        'login_status_path = "/api/v1/login/status"',
        'publish_path = "/api/v1/publish"',
        "",
        "[console]",
        'host = "127.0.0.1"',
        "port = 8765",
        "",
        "[render]",
        'output_dir = "var/drafts"',
        "",
    ]
    sources = args.source or (
        # 没有显式给 --source 时，按模式选默认来源：
        # live 用真实 RSS；offline 用仓库里的 JSON 演示数据。
        [
            "OpenAI News=https://openai.com/news/rss.xml",
            "Hugging Face Blog=https://huggingface.co/blog/feed.xml",
        ]
        if mode == "live"
        else [
            "Demo AI News=fixtures/demo_feed.json",
            "Demo Vendor=fixtures/vendor_feed.json",
        ]
    )
    for item in sources:
        # 支持 NAME=URL 的写法；只写一个 URL 时，名字就用 URL 本身。
        if "=" in item:
            name, url = item.split("=", 1)
        else:
            name, url = item, item
        kind = "rss"
        # offline 模式或 .json 结尾的来源，按 fixture 处理；
        # 其他情况仍是 rss。fixture 只用于离线演示，live 发布会拒绝它。
        if mode != "live" or url.endswith(".json"):
            kind = "fixture" if url.endswith(".json") else "rss"
        lines += [
            "",
            "[[sources]]",
            f"name = {_toml_string(name)}",
            f"url = {_toml_string(url)}",
            f"kind = {_toml_string(kind)}",
            "trust_tier = 2",
            "enabled = true",
        ]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # 配置写完后，明确告诉用户“还差哪些密钥”和“真实发布还需要什么”。
    # 很多误发事故来自“以为配好了其实没配”，所以这里把条件逐条列出。
    print(f"已写入非密钥配置: {output}")
    print("下一步：把密钥写入环境变量或 config/secrets.toml：")
    print("  DEEPSEEK_API_KEY / LARK_APP_ID / LARK_APP_SECRET / LARK_OWNER_OPEN_ID")
    print("  XHS_MCP_AUTH_TOKEN（仅本机小红书桥） / BOT_CONSOLE_TOKEN")
    print("真实发布还需要：mode=live、publisher.live_enabled=true、BOT_ALLOW_REAL_PUBLISH=1、"
          "账号核验，以及一次具体作品的最终确认。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    # 用 argparse 组装所有子命令。每个子命令只负责把参数收集好，
    # 然后通过 set_defaults(func=...) 指向对应的 cmd_* 函数。
    # main() 最后统一调用 args.func(args)。
    parser = argparse.ArgumentParser(prog="bot", description="AI 工具观察 · 本地运营机器人")
    parser.add_argument("--config", help="配置文件路径（默认 config/bot.toml）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="初始化数据库并同步来源")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("status", help="查看审批/发布/预算状态")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("tick", help="运行一个工作周期")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_tick)

    p = sub.add_parser("worker", help="启动常驻工作进程（隐藏窗口脚本会调用）")
    p.set_defaults(func=cmd_worker)

    p = sub.add_parser("console", help="启动本地审批控制台")
    p.set_defaults(func=cmd_console)

    p = sub.add_parser("doctor", help="检查配置/依赖/外部账号状态")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("demo", help="运行完整离线演示（无需外部账号）")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("backup", help="备份 SQLite 数据库")
    p.add_argument("--output")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("approve", help="以本机管理员身份批准审批码")
    p.add_argument("code")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("reject", help="以本机管理员身份拒绝审批码")
    p.add_argument("code")
    p.set_defaults(func=cmd_reject)

    p = sub.add_parser("revise", help="请求修改（可附意见）")
    p.add_argument("code")
    p.add_argument("instruction", nargs="?")
    p.set_defaults(func=cmd_revise)

    p = sub.add_parser("reconcile", help="人工核对 publish_unknown 结果（不重发）")
    p.add_argument("code")
    p.add_argument("resolution", choices=["confirmed_published", "confirmed_not_published", "still_unknown"])
    p.add_argument("--post-id")
    p.add_argument("--url")
    p.add_argument("--note")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser(
        "refresh-topic",
        help="显式重新检索 blocked/failed 选题（不删除旧记录、不自动重试）",
    )
    p.add_argument("code")
    p.set_defaults(func=cmd_refresh_topic)

    p = sub.add_parser("results", help="只读查看发布业务结果")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_results)

    p = sub.add_parser(
        "reconcile-readonly",
        help="只读核对 publish_unknown；无可靠证据时保持 unknown",
    )
    p.add_argument("code")
    p.set_defaults(func=cmd_reconcile_readonly)

    p = sub.add_parser("publish-due", help="手动运行发布门（仍会做全部检查）")
    # 帮助文字特意强调“仍会做全部检查”，防止用户把它当成跳过审批的开关。
    p.set_defaults(func=cmd_publish_due)

    p = sub.add_parser("gateway", aliases=["lark"], help="启动飞书长连接审批网关")
    p.set_defaults(func=cmd_gateway)

    p = sub.add_parser("gateway-state", help="只读报告是否应管理飞书网关进程")
    p.add_argument("--output")
    p.set_defaults(func=cmd_gateway_state)

    p = sub.add_parser("configure", help="生成不含密钥的配置骨架")
    p.add_argument("--mode", choices=["offline", "live"], default="offline")
    p.add_argument("--output", default="config/bot.generated.toml")
    p.add_argument("--force", action="store_true")
    p.add_argument("--account", default="xhs-main")
    p.add_argument("--display-name", default="AI 工具观察")
    p.add_argument("--source", action="append", help="NAME=URL，可重复")
    p.set_defaults(func=cmd_configure)
    return parser


def main(argv: list[str] | None = None) -> int:
    # 统一入口：解析参数 -> 调用子命令函数 -> 把返回码交给外壳。
    # 两类常见异常在这里转成人能看懂的提示：
    #   ConfigError      配置写错了 -> 返回 2（区别于业务失败 1）
    #   KeyboardInterrupt 用户按 Ctrl+C -> 返回 130（Unix 惯例）
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

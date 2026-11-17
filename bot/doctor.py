"""Read-only-ish readiness checks.

``doctor`` never sends a real post, never prints a secret, and never treats a
missing external credential as connected.  It initialises/reads the local DB so
it can report migrations and queue state, but it makes no platform write calls.
"""

# =============================================================================
# 中文导读：上线前的“体检报告”
# =============================================================================
# doctor 回答一个问题：这台机器现在能不能跑？哪些东西还缺？
#
# 四条原则：
#   1. 绝不发真实帖子，也不调用任何平台的写接口；
#   2. 绝不打印密钥值，只报告 set / missing；
#   3. 没配置的外部服务一律算“没连上”，不假装正常；
#   4. 缺失（missing）不等于失败（fail）：缺飞书凭据仍然可以本地演示，
#      只有 Python 版本、Pillow、字体、数据库、配置守卫这类硬问题才算 fail。
#
# 它会打开（并在必要时初始化）本地数据库，因为要检查迁移是否完成、
# 队列里有没有卡住的任务；但不会写任何业务数据。
# =============================================================================

from __future__ import annotations

import importlib.util
import json
import platform
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .config import Config
from .db import Database
from .lark_gateway import LarkLongConnection
from .timeutil import SystemClock

OK = "ok"
WARN = "warn"
MISSING = "missing"
# 四种状态的区别：
#   ok      正常
#   warn    能用，但建议注意（比如账号未启用、用的是 mock）
#   missing 外部服务没配置；不影响本地演示，但对应功能不可用
#   fail    硬性问题，必须修（解释器太旧、Pillow 缺失、数据库打不开等）
FAIL = "fail"


def _font_check(config: Config) -> tuple[str, str]:
    # 字体检查：标题字体、正文字体，以及所有后备字体。
    # 任何一个配置了却不存在，就报 fail——因为渲染 PNG 时会直接失败，
    # 与其等到发布前才炸，不如体检时就说清楚。
    fonts = [
        config.get("render.title_font"),
        config.get("render.body_font"),
        *list(config.get("render.fallback_fonts", []) or []),
    ]
    missing = [str(p) for p in fonts if p and not Path(str(p)).exists()]
    if missing:
        return FAIL, "字体缺失: " + ", ".join(missing)
    return OK, f"{len(fonts)} 个字体路径可用"


def run_doctor(config: Config, *, json_output: bool = False) -> dict[str, Any]:
    # 整个体检结果放在 checks 列表里，每项是
    # {name, status, detail}。最后再汇总出 overall。
    clock = SystemClock()
    checks: list[dict[str, str]] = []

    def add(name: str, status: str, detail: str) -> None:
        # 小助手：统一格式，避免每处都手写字典。
        checks.append({"name": name, "status": status, "detail": detail})

    # Python 版本、SQLite 版本是所有功能的基础。
    add("python", OK if sys.version_info >= (3, 11) else FAIL, platform.python_version())
    add("sqlite", OK, sqlite3.sqlite_version)

    try:
        import PIL  # type: ignore

        # Pillow 用来把草稿渲染成 PNG。没有它，整条发布链路走不通。
        add("pillow", OK, f"Pillow {getattr(PIL, '__version__', '?')}")
    except Exception as exc:  # noqa: BLE001
        add("pillow", FAIL, f"Pillow 不可用: {exc}")

    status, detail = _font_check(config)
    add("fonts", status, detail)

    add("mode", OK, f"{config.mode} (demo={config.demo_pipeline})")
    # 账号未启用只算 warn：mock 模式不需要绑定账号；
    # 但 live 发布前必须 enabled 且 verified，发布闸门会再检查一次。
    add(
        "account",
        OK if config.account().get("enabled") else WARN,
        f"{config.account().get('id')} enabled={config.account().get('enabled')} "
        f"status={config.account().get('status')}",
    )

    db = Database(config.db_path)
    try:
        # init() 会执行迁移，确保报告里的表结构就是实际运行用的结构。
        # 这也是 doctor 唯一会“写”本地文件的地方。
        db.init()
        counts = {
            "events": db.scalar("SELECT COUNT(*) FROM events", default=0),
            "drafts": db.scalar("SELECT COUNT(*) FROM drafts", default=0),
            "pending_approvals": db.scalar(
                "SELECT COUNT(*) FROM approvals WHERE status='pending'", default=0
            ),
            "unknown_publishes": db.scalar(
                "SELECT COUNT(*) FROM publish_attempts WHERE state='unknown'", default=0
            ),
        }
        add("database", OK, f"{config.db_path} {counts}")
    except Exception as exc:  # noqa: BLE001
        add("database", FAIL, f"数据库不可用: {exc}")

    try:
        # validate() 找配置里的软问题（比如路径可疑、模式组合不常见）；
        # runtime_guards() 找硬问题（比如 live 配置缺关键项）。
        warnings = config.validate()
        add("config", OK if not warnings else WARN, "; ".join(warnings) if warnings else "无警告")
    except Exception as exc:  # noqa: BLE001
        add("config", FAIL, f"配置错误: {exc}")
    try:
        problems = config.runtime_guards()
        add("guards", OK if not problems else FAIL, "; ".join(problems) if problems else "通过")
    except Exception as exc:  # noqa: BLE001
        add("guards", FAIL, str(exc))

    secrets_status = config.secrets.status()
    # secrets.status() 只返回 "set"/"missing"，绝不返回值本身。
    # 体检报告可能会被贴到聊天里，所以这条边界非常重要。
    add(
        "secrets",
        OK if any(v == "set" for v in secrets_status.values()) else WARN,
        "仅检查存在性: " + ", ".join(f"{k}={v}" for k, v in secrets_status.items()),
    )
    # DeepSeek 未配置只算 missing：offline/fake 模型仍然能演示。
    # live 模式下没有 key，写稿任务会被预算/配置检查挡住，而不是悄悄用假模型。
    add(
        "deepseek",
        OK if config.secrets.deepseek_api_key else MISSING,
        "DEEPSEEK_API_KEY "
        + ("已设置" if config.secrets.deepseek_api_key else "未设置；live 模式无法生成")
        + f" model={config.get('llm.model')} base={config.get('llm.base_url')}",
    )

    sources = []
    try:
        # 只统计 enabled 的来源；配置里写了但禁用的不算。
        sources = config.enabled_sources()
        add("sources", OK if sources else MISSING, f"{len(sources)} 个启用的来源")
    except Exception as exc:  # noqa: BLE001
        add("sources", FAIL, str(exc))

    notify_provider = str(config.get("notify.provider", "fake"))
    # fake 通知永远可用，但明确标 warn：它不会真的通知到人。
    if notify_provider == "fake":
        add("notify", WARN, "fake：只写本地通知文件，不会联系飞书")
    else:
        try:
            from .adapters.notify import LarkNotifier

            health = LarkNotifier(config, clock).health()
            add(
                "notify",
                OK if health.get("configured") and health.get("sdk_installed") else MISSING,
                json.dumps(health, ensure_ascii=False),
            )
        except Exception as exc:  # noqa: BLE001
            add("notify", FAIL, f"Lark notifier 初始化失败: {exc}")
        gateway = LarkLongConnection(config, db, clock).health()
        # 长连接健康检查也只报告“能不能启动”，不会真的建立连接。
        add(
            "lark_long_connection",
            OK if gateway.get("ok") else MISSING,
            json.dumps(gateway, ensure_ascii=False),
        )

    publisher_provider = str(config.get("publisher.provider", "mock"))
    # mock 标 warn：能跑，但只是写本地文件，绝对不是真实发布。
    if publisher_provider == "mock":
        add("publisher", WARN, "mock：只写模拟导出，绝不代表真实发布")
    elif publisher_provider == "xiaohongshu":
        expected = str(config.get("publisher.xiaohongshu.expected_red_id", "") or "").strip()
        if not config.secrets.xiaohongshu_token:
            add(
                "publisher",
                MISSING,
                "XHS_MCP_AUTH_TOKEN / xhs token 未设置；"
                + ("expected_red_id=set" if expected else "expected_red_id=missing"),
            )
        else:
            try:
                from .adapters.publishers import build_publisher

                # Read-only health check is allowed even when real publishing is
                # disabled; this instance's publish() always refuses.
                pub = build_publisher(config, clock, health_only=True)
                health = pub.health()
                status = OK if health.get("ok") else MISSING
                add(
                    "publisher",
                    status,
                    json.dumps(health, ensure_ascii=False),
                )
            except Exception as exc:  # noqa: BLE001
                add("publisher", MISSING, f"小红书本地桥不可用: {exc}")
        if not config.is_live_publish_armed():
            add(
                "publisher_armed",
                MISSING,
                "小红书真实发布未武装：需要 live 模式、publisher.live_enabled=true、"
                "BOT_ALLOW_REAL_PUBLISH=1、非 fixture 来源、账号已核验，"
                "并按 /api/v1/user/me 的 data.data.userBasicInfo.redId 配置 expected_red_id"
                "（不是本地别名，也不是未文档化的 user_id）。",
            )
    else:
        add("publisher", MISSING, f"未知 publisher.provider={publisher_provider}")

    console_host = str(config.get("console.host", "127.0.0.1"))
    # 控制台绑定非回环地址不是硬错误（配置允许显式放开），但一定给 warn。
    add(
        "console",
        OK if console_host in {"127.0.0.1", "localhost", "::1"} else WARN,
        f"http://{console_host}:{config.get('console.port')} token="
        + ("set" if config.secrets.console_token else "generated-at-start"),
    )

    if not config.secrets.lark_app_id and notify_provider == "lark":
        add("lark_app", MISSING, "LARK_APP_ID / LARK_APP_SECRET 未设置")
    if config.mode == "live" and not config.secrets.lark_owner_open_id:
        # live 模式必须有 owner：没有 owner 就没人能批准，审批会永远卡住。
        add("owner", MISSING, "LARK_OWNER_OPEN_ID 未设置；live 审批无法绑定本人")

    # 汇总规则：只有 fail 会让 overall 变成 fail；
    # missing 只表示“某个外部功能不可用”，本地闭环仍然可以跑。
    overall = FAIL if any(c["status"] == FAIL for c in checks) else OK
    report = {
        "overall": overall,
        "mode": config.mode,
        "demo": config.demo_pipeline,
        "real_publish_armed": config.is_live_publish_armed(),
        "checks": checks,
        "generated_at": clock.now().isoformat(),
    }
    if json_output:
        # --json 模式给脚本/其他程序读；默认模式给人看。
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        # ASCII labels keep doctor usable in legacy Windows console code pages
        # (cp936 cannot encode check marks).
        symbol = {OK: "OK", WARN: "WARN", MISSING: "MISS", FAIL: "FAIL"}
        print(f"bot doctor · mode={config.mode} · demo={config.demo_pipeline} · "
              f"real_publish_armed={config.is_live_publish_armed()}")
        for check in checks:
            print(f"[{symbol.get(check['status'], '?')}] {check['name']}: {check['detail']}")
        print(f"结论: {overall}")
        if any(c["status"] == MISSING for c in checks):
            print("缺失项不会阻止本地演示；补齐后才可能连接外部服务。")
    return report

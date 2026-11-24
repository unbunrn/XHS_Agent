"""Shared test helpers.  Everything is local and disposable."""

# =============================================================================
# 中文导读：测试的“公共底座”
# =============================================================================
# 所有测试都用这里的函数搭一个小而干净的世界：
#   - var/tests/<prefix>-<随机> 下的一次性目录和数据库；
#   - 一份完整的默认配置（offline + fake 模型 + fake 通知 + mock 发布）；
#   - 一个冻结时钟（时间可手动前进，测试结果稳定可复现）；
#   - 一条默认 fixture 新闻，以及从采集到写稿的快捷函数。
#
# 关键原则：
#   1. 每个测试都有独立目录，互不干扰，跑完 cleanup 直接删；
#   2. 默认绝不触碰真实网络/真实平台/真实密钥；
#   3. 测试里也尽量走真实服务函数，而不是另写一套简化逻辑，
#      否则测试通过了、线上路径却坏了。
# =============================================================================

from __future__ import annotations

import copy
import json
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from bot.adapters.llm import FakeLLM
from bot.config import DEFAULTS, Config, Secrets
from bot.db import Database
from bot.httpclient import HttpClient
from bot.security import SSRFGuard
from bot.services import research
from bot.services.approvals import handle_inbound_message
from bot.services.drafts import create_draft
from bot.timeutil import FrozenClock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = PROJECT_ROOT / "var" / "tests"


def _fresh_dir(prefix: str) -> Path:
    # 在 var/tests 下建一个唯一目录。加随机后缀是为了：
    #   - 并行跑测试时不会互相覆盖；
    #   - 上一次失败留下的目录不会影响这一次。
    # exist_ok=False 保证“如果撞名就报错”，而不是悄悄复用旧数据。
    TEST_ROOT.mkdir(parents=True, exist_ok=True)
    path = TEST_ROOT / f"{prefix}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def make_config(
    root: Path,
    *,
    mode: str = "offline",
    llm_provider: str = "fake",
    notify_provider: str = "fake",
    publisher_provider: str = "mock",
    source_items: list[dict] | None = None,
    fixture_kind: str = "fixture",
    account_enabled: bool = False,
    account_status: str = "unverified",
    live_enabled: bool = False,
    max_drafts: int = 10,
    max_pending: int = 10,
    max_queued: int = 10,
    max_publish: int = 10,
) -> Config:
    # 从项目默认配置深拷贝一份，再按测试需要覆盖。
    # 深拷贝很重要：多个测试共享 DEFAULTS，直接改会互相污染。
    data = copy.deepcopy(DEFAULTS)
    data["render"]["engine"] = "pillow"
    data["app"].update(
        {
            "mode": mode,
            "data_dir": "var",
            "db_path": "var/bot.sqlite3",
            "log_dir": "var/logs",
            "pause_all": False,
        }
    )
    data["account"].update(
        {
            "id": "test-account",
            "display_name": "测试账号",
            "platform": "xiaohongshu",
            "enabled": account_enabled,
            "status": account_status,
        }
    )
    data["llm"]["provider"] = llm_provider
    data["llm"]["model"] = "deepseek-flash"
    data["llm"]["base_url"] = "https://api.deepseek.com"
    data["notify"]["provider"] = notify_provider
    data["notify"]["fake_dir"] = "var/notifications"
    data["notify"]["lark"].update(
        {"owner_open_id": "owner-open-id", "chat_id": "", "require_p2p": True}
    )
    data["publisher"].update(
        {
            "provider": publisher_provider,
            "live_enabled": live_enabled,
            "export_dir": "var/exports",
        }
    )
    data["publisher"]["xiaohongshu"]["base_url"] = "http://127.0.0.1:18060"
    data["console"].update({"host": "127.0.0.1", "port": 0})
    data["render"]["output_dir"] = "var/drafts"
    data["limits"].update(
        {
            "max_drafts_per_day": max_drafts,
            "max_pending_approvals": max_pending,
            "max_queued_topics": max_queued,
            "max_publish_per_day": max_publish,
            "max_age_hours": 72,
        }
    )
    if source_items is not None:
        data["sources"] = [
            {
                "name": "Test Fixture",
                "url": "feed.json",
                "kind": fixture_kind,
                "trust_tier": 2,
                "enabled": True,
            }
        ]
        # 把测试数据写到临时目录里的 feed.json。
        # FixtureSource 会把它当成“本地演示来源”读取，不联网。
        (root / "feed.json").write_text(
            json.dumps({"items": source_items}, ensure_ascii=False), encoding="utf-8"
        )
    else:
        data["sources"] = []
    return Config(
        data=data,
        root=root,
        # 测试用虚构的 owner 和 console token：
        # 即使环境变量里存在真实密钥，也不会被测试读到。
        secrets=Secrets(
            lark_owner_open_id="owner-open-id",
            console_token="test-console-token",
        ),
    )


def make_http(config: Config, **kwargs) -> HttpClient:
    # 测试用 HTTP 客户端：不重试、不节流、超时短，避免测试变慢。
    # 但仍然使用真正的 SSRFGuard，确保网络防护逻辑也被覆盖。
    options = dict(
        guard=SSRFGuard(config.get("network.allow_private_hosts") or ()),
        timeout=5.0,
        max_bytes=1_000_000,
        min_host_interval=0.0,
        max_retries=0,
        backoff_base=0.01,
    )
    options.update(kwargs)
    return HttpClient(**options)


@dataclass
class Env:
    # 一个小型“测试环境对象”：把目录、配置、数据库、时钟、HTTP 客户端
    # 打包在一起，测试函数只传一个 env 就够了。
    root: Path
    config: Config
    db: Database
    clock: FrozenClock
    http: HttpClient
    llm: FakeLLM = field(init=False)
    events: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        # 用 FakeLLM：不调用 DeepSeek，输出确定，测试可离线运行。
        self.llm = FakeLLM(self.config, self.clock)

    def ingest(self) -> dict:
        # 同步来源 -> 找到测试来源 -> 采集。
        # 返回采集统计，便于测试断言“确实处理了条目”。
        research.sync_sources(self.db, self.config)
        source = research.source_row(self.db, "Test Fixture")
        assert source is not None
        return research.ingest_source(self.db, self.config, self.http, self.clock, source)

    def queue(self) -> list[dict]:
        # 把采集到的条目聚类/评分，挑出可写的选题放进研究队列。
        return research.ensure_research_queue(self.db, self.config, self.clock)


def new_env(
    prefix: str = "case",
    *,
    items: list[dict] | None = None,
    config_kwargs: dict | None = None,
) -> Env:
    # 一站式搭建测试环境：目录、配置、数据库、冻结时钟、HTTP 客户端。
    # 默认时间设为 2026-11-05 09:00 UTC，和项目里的示例日期保持一致。
    root = _fresh_dir(prefix)
    config_kwargs = config_kwargs or {}
    if items is None:
        items = [default_item()]
    config = make_config(root, source_items=items, **config_kwargs)
    db = Database(config.db_path)
    db.init()
    env = Env(
        root=root,
        config=config,
        db=db,
        clock=FrozenClock(datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)),
        http=make_http(config),
    )
    return env


def default_item(
    *,
    title: str = "开源 AI 工具发布免费 API 更新",
    url: str = "https://example.com/news/free-api",
    summary: str = "该开源 AI 工具新增免费 API，支持中文开发者使用。官方文档说明可以本地部署。",
    hours: float | None = -2,
    content: str | None = None,
) -> dict:
    # 默认新闻：一条“开源 AI 工具发布免费 API”的中文消息。
    # hours=-2 表示发布时间是 2 小时前，足够新，不会被“太旧”规则过滤。
    item = {
        "title": title,
        "url": url,
        "summary": summary,
        "content": content or summary,
        "author": "Test Fixture",
    }
    if hours is not None:
        item["published_offset_hours"] = hours
    return item


def make_draft(env: Env, *, item: dict | None = None) -> dict:
    # 采集 -> 入队 -> 取一个研究完成的事件 -> 写稿。
    # 如果传了 item，就先用它重写 feed.json，方便测试不同内容。
    if item is not None:
        (env.root / "feed.json").write_text(
            json.dumps({"items": [item]}, ensure_ascii=False), encoding="utf-8"
        )
    env.ingest()
    env.queue()
    event = research.next_researched_event(env.db)
    assert event is not None, "fixture did not produce a researched event"
    draft = create_draft(env.db, env.config, env.clock, env.llm, event_id=int(event["id"]))
    return draft


def approve(env: Env, code: str, *, message_id: str | None = None, text: str | None = None):
    # 模拟 owner 从私聊发来“批准 <code>”。
    # message_id 默认随机；需要测试幂等时由调用方传固定值。
    return handle_inbound_message(
        env.db,
        env.config,
        env.clock,
        message_id=message_id or f"test-{uuid.uuid4().hex}",
        sender_id="owner-open-id",
        chat_id="owner-open-id",
        chat_type="p2p",
        text=text or f"批准 {code}",
        channel="test",
    )


def cleanup(env: Env) -> None:
    # 删除本次测试的临时目录。ignore_errors=True：即使文件被占用，
    # 也不应该让清理失败掩盖真正的测试结果。
    shutil.rmtree(env.root, ignore_errors=True)

"""Configuration loading.

Config is TOML (stdlib ``tomllib``).  Secrets are never stored in the normal
config file: they come from environment variables or an untracked
``config/secrets.toml``.  Nothing in this module prints secret values.
"""

# =============================================================================
# 中文导读：配置是怎么一层层叠起来的
# =============================================================================
# 优先级从低到高（后面的覆盖前面的）：
#   1. 代码里的 DEFAULTS          —— 出厂默认值，保证缺配置也能跑
#   2. config/bot.toml            —— 你写的普通配置（不含密钥）
#   3. 环境变量                   —— 部署时临时覆盖，例如 BOT_MODE=live
#   4. config/secrets.toml        —— 本机密钥文件（不要提交到代码库）
#
# 为什么不把密钥直接写进 bot.toml？
#   bot.toml 很可能被复制、截图、提交或发给别人；密钥一旦泄露，
#   别人就能用你的 DeepSeek 额度，甚至操作你的账号。
#   所以密钥只从环境变量或未跟踪的 secrets.toml 读取，并且本项目
#   任何日志/doctor 输出都只显示 "set" 或 "missing"，绝不打印值。
#
# 另一个重点：真实发布不是靠一个开关，而是四道门同时打开：
#   app.mode=live + publisher.live_enabled=true
#   + publisher.provider=xiaohongshu + 环境变量 BOT_ALLOW_REAL_PUBLISH=1
#   并且不能是 fixture/fake 演示管线。少一道都只会模拟发布。
# =============================================================================

from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "bot.toml"
EXAMPLE_CONFIG = PROJECT_ROOT / "config" / "bot.example.toml"

# 出厂默认值。设计原则是“保守”：
#   - 默认离线、默认假模型、默认 mock 发布，初学者直接能跑；
#   - 每日草稿/发布/审批数量都很小，避免一上来就刷屏；
#   - 网络超时、重试、响应大小都有上限，防止卡死或被打爆。
# 注意：这里的 dict 会被 copy.deepcopy 后再合并，绝不能直接修改它。
DEFAULTS: dict[str, Any] = {
    # app：程序运行的基本信息。mode 是总开关：
    #   offline = 只用本地样例/假模型/mock 发布；
    #   live    = 允许接真实外部服务（但仍需其他开关和审批）。
    "app": {
        "name": "bot",
        "mode": "offline",  # offline | live
        "timezone": "Asia/Shanghai",
        "data_dir": "var",
        "db_path": "var/bot.sqlite3",
        "log_dir": "var/logs",
        "log_level": "INFO",
        "log_max_bytes": 5_000_000,
        "log_backups": 5,
        # Untracked local file used only for OAuth-ish IDs/secrets.
        "secrets_file": "config/secrets.toml",
        # Must be explicitly true before any fake adapter may run in live mode.
        "allow_fake_in_live": False,
        "pause_all": False,
    },
    # account：账号身份与核验状态。
    # 真实发布要求 enabled=true 且 status=verified；
    # 未核验的账号可以走离线模拟，但不会被说成“已连接”。
    "account": {
        "id": "xhs-main",
        "platform": "xiaohongshu",
        "display_name": "未绑定账号",
        "enabled": False,
        "status": "unverified",  # unverified | verified | needs_login | disabled
    },
    # limits：业务护栏。它们不是“目标”，而是“上限”。
    # 例如 max_drafts_per_day=1 表示宁可少发，也不为凑数硬写。
    # max_age_hours 用来拒绝旧闻；approval_ttl_hours 让没人理的审批自动过期。
    # Read-only reconciliation is deliberately bounded.  A lookup that cannot
    # prove identity stays unknown; it never releases quota by itself.
    "reconcile": {
        "max_checks_per_attempt": 5,
        "max_age_hours": 72,
        "min_interval_minutes": 10,
        "match_window_minutes": 120,
        "min_body_similarity": 0.65,
    },
    "limits": {
        "max_drafts_per_day": 1,
        "max_queued_topics": 3,
        "max_pending_approvals": 3,
        "max_candidates_per_round": 3,
        "max_publish_per_day": 1,
        "max_manual_publish_per_day": 1,
        "max_publish_attempts_per_task": 3,
        "max_manual_drafts_per_day": 1,
        "max_age_hours": 72,
        "event_window_days": 7,
        "poll_interval_seconds": 1800,
        "poll_jitter_seconds": 120,
        "approval_ttl_hours": 24,
        "publish_immediately": False,
        "publish_hour_local": 20,
        "publish_minute_local": 0,
        "max_items_per_source": 30,
        "max_attempts": 5,
    },
    # agent：可选的质量闭环。默认关闭，离线测试和旧部署行为不变；
    # live 配置开启后，写稿会先经过一次结构化审稿，必要时只做一轮修订。
    "agent": {
        "critic_enabled": False,
        "critic_max_output_tokens": 900,
        "max_critic_problems": 5,
        "max_critic_revisions": 1,
    },
    # Creator Agent V2: bounded model-driven loop.  Off by default so existing
    # deployments keep their deterministic pipeline until explicitly enabled.
    "creator_agent": {
        "enabled": False,
        "max_steps": 8,
        "max_revision_steps": 6,
        "max_review_cycles": 2,
        "max_searches": 5,
        "max_physical_fetches": 6,
        "max_model_calls": 14,
        "max_invalid_decisions": 2,
        "max_no_progress_steps": 2,
        "max_run_seconds": 600,
        "semantic_review": False,
        "resume_deferred": False,
        "daily_discovery_enabled": False,
        "daily_discovery_seconds": 86400,
        "daily_discovery_max_topics": 2,
        "daily_discovery_max_age_hours": 48,
        "daily_discovery_feed_limit": 50,
    },
    # Publication Recovery Agent: approved artifact -> durable task -> bounded
    # diagnosis/retry. Off by default; the old publish gate remains the final
    # authority either way.
    "publication_agent": {
        "enabled": False,
        "max_steps": 8,
        "max_model_calls": 12,
        "max_daily_model_calls": 100,
        "max_invalid_decisions": 2,
        "max_retries": 3,
        "retry_base_seconds": 30,
        "retry_max_seconds": 1800,
        "login_wait_seconds": 300,
        "owner_wait_seconds": 3600,
        "task_ttl_seconds": 86400,
    },
    # network：抓取网页时的边界。
    # max_fetch_bytes 防止一个超大响应把内存吃光；
    # min_host_interval_seconds 是“对同一个网站别打太快”的礼貌限速；
    # allow_private_hosts 默认空——新闻 URL 绝不允许访问本机/内网地址（防 SSRF）。
    "network": {
        "timeout_seconds": 20,
        "max_fetch_bytes": 2_097_152,
        "min_host_interval_seconds": 2.0,
        "max_retries": 2,
        "backoff_base_seconds": 1.0,
        "backoff_max_seconds": 20.0,
        "user_agent": "BotOperator/0.1 (+local; contact: operator)",
        # Hosts that may resolve to private addresses.  Only the configured
        # local publishing service may be listed here; news URLs never use it.
        "allow_private_hosts": [],
    },
    # budget：模型花费的“每日钱包”。
    # 这里故意用美元记账且不自动换算汇率，避免“看起来没超支”的错觉；
    # 拿不到 usage 的调用会按 reserve_unknown_usd 保守预留，而不是记 0。
    "budget": {
        # Official DeepSeek list prices are denominated in USD.  We do not
        # silently assume an FX rate; reports stay in USD and the operator can
        # compare them with the provider invoice.
        "daily_usd": 0.10,
        "max_calls_per_day": 40,
        "input_usd_per_million": 0.30,
        "output_usd_per_million": 1.20,
        "reserve_unknown_usd": 0.005,
        "max_output_tokens": 1800,
    },
    # llm：写稿模型。fake 是完全离线的模板生成器，方便演示；
    # deepseek 才会真正发网络请求并花钱。live 模式不会静默回退到 fake。
    "llm": {
        "provider": "fake",  # fake | deepseek
        "model": "deepseek-flash",
        "base_url": "https://api.deepseek.com",
        "timeout_seconds": 60,
        "max_retries": 2,
        "temperature": 0.5,
        # Short JSON drafts do not need reasoning tokens; disabling thinking
        # keeps the output bounded and avoids finish_reason=length surprises.
        "thinking_disabled": True,
    },
    # notify：审批通知渠道。fake 只把消息写到本地文件（离线演示）；
    # lark 通过官方 SDK 发给 owner 的私聊，需要 app_id/secret 和 open_id。
    "notify": {
        "provider": "fake",  # fake | lark
        "fake_dir": "var/notifications",
        "lark": {
            "domain": "https://open.feishu.cn",
            "owner_open_id": "",
            "chat_id": "",
            "require_p2p": True,
        },
    },
    # publisher：发布适配器。mock 只写本地“模拟导出”；
    # xiaohongshu 才会调用本机的小红书桥（社区浏览器自动化，不是官方 API）。
    # 即使选了 xiaohongshu，live_enabled=false 时也不会真正发布。
    "publisher": {
        "provider": "mock",  # mock | xiaohongshu
        # Real external publishing stays off until the operator enables it in
        # both this file and the environment.
        "live_enabled": False,
        "export_dir": "var/exports",
        "xiaohongshu": {
            "base_url": "http://127.0.0.1:18060",
            "login_status_path": "/api/v1/login/status",
            # The bridge's logged-in account is checked before any publish POST.
            # The documented response exposes the public account number
            # data.data.userBasicInfo.redId, not a stable internal user_id.
            # Configure that public redId, never the local "xhs-main" alias.
            # It can also come from the XHS_EXPECTED_RED_ID environment variable.
            "user_me_path": "/api/v1/user/me",
            "expected_red_id": "",
            "publish_path": "/api/v1/publish",
            "timeout_seconds": 60,
            "connect_timeout_seconds": 8,
        },
    },
    # console：本机审批网页。默认只监听 127.0.0.1，外部电脑访问不到。
    # 如果要改成 0.0.0.0，必须显式打开 allow_non_loopback，且自己承担风险。
    "console": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 8765,
        "session_ttl_minutes": 120,
        "allow_non_loopback": False,
    },
    # render：PNG 卡片尺寸与字体。Windows 自带微软雅黑，路径写死在这里；
    # 换机器时如果找不到字体，fallback_fonts 会依次尝试。
    "render": {
        "width": 1080,
        "height": 1440,
        "margin": 72,
        "title_font": "C:/Windows/Fonts/msyhbd.ttc",
        "body_font": "C:/Windows/Fonts/msyh.ttc",
        "fallback_fonts": [
            "C:/Windows/Fonts/simhei.ttf",
            "C:/Windows/Fonts/Deng.ttf",
        ],
        "max_cards": 6,
        "output_dir": "var/drafts",
    },
    # writer：文案规则。banned_phrases 是“禁止出现的营销/亲测话术”，
    # 校验器发现草稿里有这些词就直接拒绝生成，防止机器人编造个人经历。
    "writer": {
        "title_max_chars": 20,
        "body_max_chars": 1000,
        # Caps are rendering/publish bounds.  They are not a mandated caption
        # length, card count, layout combination or image/body ratio.
        "storyboard_max_cards": 4,
        "storyboard_max_chars": 900,
        # Nonsecret, editable owner editorial profile.  It is loaded for every
        # generation; per-post instructions and webpage text are never written
        # into it automatically.
        "editorial_profile_path": "config/editorial-profile.toml",
        "banned_phrases": [
            "我亲测",
            "我实测",
            "我赚了",
            "保证收益",
            "稳赚",
            "必爆",
            "包赚",
            "内部消息",
        ],
    },
    # topics：手机飞书“选题/写一篇”请求的独立护栏。
    # 它与 limits.max_drafts_per_day 相互独立：选题请求的数量上限控制的是
    # “允许多少次检索/写作委托”，真正写稿仍受每日草稿、待审批和预算限制。
    "topics": {
        "max_input_chars": 4000,
        "max_pending": 3,
        "max_per_day": 5,
        "search_max_results": 5,
        "search_max_fetches": 2,
        "search_lookback_days": 7,
        # S1 research pipeline bounds.  The physical-attempt cap includes
        # retries and is not a "number of successes" cap.
        "research_max_fetch_attempts": 6,
        "research_cache_hours": 6,
        "research_min_usable": 1,
        "research_min_body_chars": 0,
        # 模型只在“已配置可信域名找不到证据”时，为这一次选题提出候选官方
        # 域名并做一次补充检索。候选域名不会写入配置或长期可信集合；最终
        # 仍要 owner 审批稿件。关闭时完全保持原来的 fail-closed 行为。
        "model_source_review": False,
        "model_source_min_confidence": 0.7,
        "model_source_max_domains": 3,
        "model_source_max_output_tokens": 500,
        # S2: one compact planning call (entities/versions/queries) and one
        # batch source-identity judge over actually fetched material.
        "model_plan": True,
        "model_plan_max_output_tokens": 700,
        "model_identity": True,
        "identity_max_output_tokens": 1800,
        "identity_cache_hours": 24,
        "identity_min_confidence": 0.6,
        # P0 adaptive loop: after the first bounded plan, detect concrete
        # coverage gaps and run at most two follow-up searches.  It cannot
        # bypass body/topic/date/identity gates; default off for old installs.
        "adaptive_research": False,
        "adaptive_max_queries": 2,
        # One bounded evidence-gap rescue when the writer refuses for thin
        # evidence.  Its separate physical budget is small and auditable; it
        # still uses the normal body/date/identity gates.
        "auto_rescue": False,
        "rescue_max_fetch": 2,
        # S3: news stays 72h; analysis is an explicit owner request for old
        # material and never rewrites original dates into "today".
        "analysis_max_age_hours": 24 * 365 * 5,
        # AIHot is a discovery feed, not an official source.  Auto-match is
        # deliberately lexical and conservative; a weak match falls back to
        # ordinary search.  Original links are always fetched first.
        "aihot_enabled": False,
        "aihot_auto_match": False,
        "aihot_feed_url": "https://aihot.news/feed.xml",
        "aihot_max_items": 50,
    },
    # revision：修改稿的验收边界。  A revision gets at most one generation
    # call and one bounded semantic-review call through the normal budget.
    # Deterministic checks can reject without any review call.
    "revision": {
        "review_max_output_tokens": 700,
        "max_review_problems": 5,
    },
    # audience：账号定位。keywords 用来算“这条新闻和我的读者相关吗”，
    # utility_keywords 用来算“这条新闻对读者有没有实际用处”。
    "audience": {
        "description": "给中文普通用户和独立开发者讲明白 AI 工具变化",
        "keywords": [
            "AI",
            "人工智能",
            "模型",
            "大模型",
            "Agent",
            "智能体",
            "DeepSeek",
            "OpenAI",
            "Claude",
            "Gemini",
            "开源",
            "API",
            "工具",
            "教程",
        ],
        "utility_keywords": [
            "免费",
            "开源",
            "教程",
            "发布",
            "上线",
            "API",
            "价格",
            "降价",
            "功能",
            "更新",
            "插件",
            "工作流",
        ],
    },
    # scoring：选题门槛。min_score 以下宁可不写；
    # title_similarity_threshold 控制“标题多像才算同一件事”（用于聚类）。
    "scoring": {
        "min_score": 0.45,
        "title_similarity_threshold": 0.56,
    },
    # sources：来源列表，来自 TOML 的 [[sources]] 数组。
    # 每一项至少要 name 和 url；kind 支持 rss / fixture。
    "sources": [],
}


class ConfigError(RuntimeError):
    # 配置错误单独成类，命令行入口可以捕获它并返回退出码 2，
    # 而不是打印一大段 Python 堆栈吓到用户。
    pass


@dataclass
class Secrets:
    # 密钥容器。字段默认 None，表示“没有配置”。
    # source 记录每个密钥来自环境变量还是 secrets.toml，方便 doctor 排查，
    # 但同样只记来源、不记值。
    deepseek_api_key: str | None = None
    lark_app_id: str | None = None
    lark_app_secret: str | None = None
    lark_owner_open_id: str | None = None
    lark_chat_id: str | None = None
    xiaohongshu_token: str | None = None
    console_token: str | None = None
    source: dict[str, str] = field(default_factory=dict)

    def status(self) -> dict[str, str]:
        """Presence only; never return values."""
        # 只返回 "set"/"missing"，这是给 doctor 和控制台看的。
        # 任何情况下都不要把密钥值放进返回值，避免它被日志或网页带出去。
        return {
            "deepseek_api_key": "set" if self.deepseek_api_key else "missing",
            "lark_app_id": "set" if self.lark_app_id else "missing",
            "lark_app_secret": "set" if self.lark_app_secret else "missing",
            "lark_owner_open_id": "set" if self.lark_owner_open_id else "missing",
            "lark_chat_id": "set" if self.lark_chat_id else "missing",
            "xiaohongshu_token": "set" if self.xiaohongshu_token else "missing",
            "console_token": "set" if self.console_token else "missing",
        }


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    # 深度合并：如果两边同一个键都是 dict，就递归合并；否则用 overlay 的值。
    # 这样用户只写想改的几项，不必把整个默认配置抄一遍。
    # deepcopy 保证不会意外修改 DEFAULTS 或用户传入的原始 dict。
    merged = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _load_toml(path: Path) -> dict[str, Any]:
    # 读 TOML。文件不存在不算错误（返回空 dict，后面用默认值）；
    # 但文件存在却格式错误必须明确报错，不能悄悄忽略用户写错的内容。
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config root must be a table: {path}")
    return data


@dataclass
class Config:
    # data 是合并后的完整配置字典；root 是项目根目录，
    # 所有相对路径（var/、fixtures/）都以它为基准；
    # path 是实际加载的 TOML 路径（可能为 None）；secrets 单独存放。
    data: dict[str, Any]
    root: Path
    path: Path | None = None
    secrets: Secrets = field(default_factory=Secrets)

    # -- access ---------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        # 用点号路径取值，例如 get("publisher.live_enabled")。
        # 这样调用方不必关心嵌套字典有多深；任何一层缺失都返回默认值。
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        # 取一个配置小节，并确认它确实是表（dict）。
        # 如果用户把 [publisher] 写成了字符串，这里会给出清晰错误。
        value = self.data.get(name, {})
        if not isinstance(value, dict):
            raise ConfigError(f"config section {name!r} must be a table")
        return value

    @property
    def mode(self) -> str:
        return str(self.get("app.mode", "offline")).lower()

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def timezone_name(self) -> str:
        return str(self.get("app.timezone", "Asia/Shanghai"))

    @property
    def data_dir(self) -> Path:
        return self.resolve_path(str(self.get("app.data_dir", "var")))

    @property
    def db_path(self) -> Path:
        return self.resolve_path(str(self.get("app.db_path", "var/bot.sqlite3")))

    @property
    def log_dir(self) -> Path:
        return self.resolve_path(str(self.get("app.log_dir", "var/logs")))

    def resolve_path(self, value: str | Path) -> Path:
        # 相对路径一律相对项目根目录解析，而不是当前工作目录。
        # 这样从任何目录启动脚本，var/ 和 fixtures/ 都能找对。
        path = Path(value)
        return path if path.is_absolute() else (self.root / path).resolve()

    def account(self) -> dict[str, Any]:
        return self.section("account")

    def enabled_sources(self) -> list[dict[str, Any]]:
        # 返回启用的来源。这里会校验每一项至少有 name 和 url，
        # 避免配置写漏后在采集阶段才出现难以理解的 NoneType 错误。
        raw = self.get("sources", [])
        if not isinstance(raw, list):
            raise ConfigError("[[sources]] must be an array of tables")
        out = []
        for entry in raw:
            if not isinstance(entry, dict) or not entry.get("name") or not (
                entry.get("url") or entry.get("query")
            ):
                raise ConfigError("every [[sources]] entry needs name and url (or query)")
            if entry.get("enabled", True):
                out.append(entry)
        return out

    # -- guards ---------------------------------------------------------
    def validate(self) -> list[str]:
        """Structural validation.  Returns non-fatal warnings."""
        # 两类问题分开处理：
        #   - 会让程序行为不可预测的 -> 抛 ConfigError（例如未知的 provider）
        #   - 只是“不太对但还能跑”的 -> 返回警告，让 doctor/用户自己判断
        # 例如 live 模式配了 fake LLM：不会崩，但必须明确警告“不会调用 DeepSeek”。
        warnings: list[str] = []
        if self.mode not in {"offline", "live"}:
            raise ConfigError("app.mode must be 'offline' or 'live'")
        llm = str(self.get("llm.provider"))
        notify = str(self.get("notify.provider"))
        publisher = str(self.get("publisher.provider"))
        if llm not in {"fake", "deepseek"}:
            raise ConfigError("llm.provider must be fake or deepseek")
        if notify not in {"fake", "lark"}:
            raise ConfigError("notify.provider must be fake or lark")
        if publisher not in {"mock", "xiaohongshu"}:
            raise ConfigError("publisher.provider must be mock or xiaohongshu")
        for key in ("max_drafts_per_day", "max_pending_approvals", "max_publish_per_day", "max_manual_publish_per_day", "max_manual_drafts_per_day"):
            if int(self.get(f"limits.{key}")) < 0:
                raise ConfigError(f"limits.{key} must be >= 0")
        for key in ("max_input_chars", "max_pending", "max_per_day"):
            if int(self.get(f"topics.{key}")) < 0:
                raise ConfigError(f"topics.{key} must be >= 0")
        confidence = float(self.get("topics.model_source_min_confidence", 0.7))
        if not 0.0 <= confidence <= 1.0:
            raise ConfigError("topics.model_source_min_confidence must be between 0 and 1")
        if int(self.get("topics.model_source_max_domains", 3)) < 0:
            raise ConfigError("topics.model_source_max_domains must be >= 0")
        if self.mode == "live":
            if llm == "fake" and not self.get("app.allow_fake_in_live", False):
                warnings.append("live mode with llm.provider=fake (explicitly allowed) will not call DeepSeek")
            if self.demo_pipeline:
                warnings.append(
                    "live mode uses a demo/fixture pipeline: runs are read-only and real publishing is force-disabled"
                )
            if not self.is_live_publish_armed():
                warnings.append("live mode but real publishing is not armed; publish stays simulated/failed")
        host = str(self.get("console.host", "127.0.0.1"))
        if host not in {"127.0.0.1", "localhost", "::1"} and not self.get(
            "console.allow_non_loopback", False
        ):
            raise ConfigError("console.host must stay loopback unless console.allow_non_loopback=true")
        return warnings

    @property
    def demo_pipeline(self) -> bool:
        """True when drafts could be produced from fake LLM or fixture sources.

        Such a pipeline may run in live mode only as a read-only rehearsal; it
        must never be able to call a real publisher.
        """
        # 判断“这条生产管线是不是假的”。只要满足任一条件就算：
        #   - 模型不是 deepseek；
        #   - 没有任何来源；
        #   - 有一个 fixture（本地样例）来源。
        # 一旦是演示管线，真实发布会被强制关闭，防止把样例内容发出去。
        if str(self.get("llm.provider", "fake")) != "deepseek":
            return True
        try:
            sources = self.enabled_sources()
        except ConfigError:
            return True
        if not sources:
            return True
        return any(str(s.get("kind", "rss")).lower() == "fixture" for s in sources)

    def is_live_publish_armed(self) -> bool:
        # “武装”= 四道开关全部打开，少一道都返回 False：
        #   1. app.mode == live
        #   2. publisher.live_enabled == true
        #   3. publisher.provider == xiaohongshu
        #   4. 环境变量 BOT_ALLOW_REAL_PUBLISH 是 1/true/yes
        # 另外还要求不是演示管线（fixture/fake 不能真实发布）。
        # 这相当于导弹发射需要两把钥匙同时转动，防止误操作。
        env = os.environ.get("BOT_ALLOW_REAL_PUBLISH", "").strip().lower()
        env_ok = env in {"1", "true", "yes"}
        return (
            self.is_live
            and bool(self.get("publisher.live_enabled", False))
            and str(self.get("publisher.provider")) == "xiaohongshu"
            and env_ok
            and not self.demo_pipeline
        )

    def lark_gateway_state(self) -> dict[str, Any]:
        """Whether start/stop/health should manage the long-connection gateway."""
        provider = str(self.get("notify.provider", "fake")).lower()
        app_id = self.secrets.lark_app_id
        app_secret = self.secrets.lark_app_secret
        owner = self.secrets.lark_owner_open_id or str(
            self.get("notify.lark.owner_open_id", "") or ""
        ).strip()
        required = provider == "lark"
        configured = bool(required and app_id and app_secret and owner)
        if not required:
            reason = "notify.provider is not lark"
        elif configured:
            reason = "notify=lark with app credentials and owner configured"
        else:
            missing = []
            if not app_id:
                missing.append("LARK_APP_ID")
            if not app_secret:
                missing.append("LARK_APP_SECRET")
            if not owner:
                missing.append("LARK_OWNER_OPEN_ID")
            reason = "notify=lark but missing " + ", ".join(missing)
        return {"required": required, "configured": configured, "reason": reason}

    def runtime_guards(self) -> list[str]:
        """Fatal guard violations for a live worker."""
        # 和 validate() 的区别：这里的每一项都是“不能继续运行”的致命问题。
        # Worker.startup() 会调用它，有问题直接拒绝启动，而不是带病运行。
        problems: list[str] = []
        if not self.is_live:
            return problems
        if self.get("llm.provider") == "fake" and not self.get("app.allow_fake_in_live", False):
            problems.append("live mode refuses implicit fake LLM; set app.allow_fake_in_live=true only for dry runs")
        if self.get("notify.provider") == "fake" and not self.get("app.allow_fake_in_live", False):
            problems.append("live mode requires a real notification channel (notify.provider=lark)")
        if self.get("publisher.live_enabled") and self.demo_pipeline:
            problems.append("demo/fixture pipeline cannot enable real publishing; use real sources and DeepSeek")
        return problems

    # -- helpers --------------------------------------------------------
    def redacted(self) -> dict[str, Any]:
        """Config view safe to log: secret-bearing keys replaced."""
        # 深拷贝后把可能含密钥的字段替换成 ***。
        # 任何要展示完整配置的地方都应该先经过这里，避免手滑打日志泄露。
        data = copy.deepcopy(self.data)
        for key in ("xiaohongshu",):
            section = data.get("publisher", {}).get(key)
            if isinstance(section, dict):
                for secret in ("token", "cookie", "password"):
                    if secret in section:
                        section[secret] = "***"
        return data


def _env_secrets(root: Path, config: dict[str, Any]) -> Secrets:
    # 读取密钥，优先级：环境变量 > secrets.toml。
    # 环境变量优先是为了方便临时切换/容器部署，也避免把密钥落到磁盘。
    secrets_file = config.get("app", {}).get("secrets_file", "config/secrets.toml")
    path = Path(str(secrets_file))
    if not path.is_absolute():
        path = root / path
    file_values = _load_toml(path) if path.exists() else {}
    source: dict[str, str] = {}

    def pick(env_name: str, file_key: str) -> str | None:
        # 先看环境变量；有值就记来源为 "env"。
        # 再看 secrets.toml；有值就记来源为文件路径。
        # 都没有返回 None，调用方据此判断是否缺配置。
        env_value = os.environ.get(env_name)
        if env_value:
            source[file_key] = "env"
            return env_value.strip()
        value = file_values.get(file_key)
        if isinstance(value, str) and value.strip():
            source[file_key] = str(path)
            return value.strip()
        return None

    secrets = Secrets(
        deepseek_api_key=pick("DEEPSEEK_API_KEY", "deepseek_api_key"),
        lark_app_id=pick("LARK_APP_ID", "lark_app_id"),
        lark_app_secret=pick("LARK_APP_SECRET", "lark_app_secret"),
        lark_owner_open_id=pick("LARK_OWNER_OPEN_ID", "lark_owner_open_id"),
        lark_chat_id=pick("LARK_CHAT_ID", "lark_chat_id"),
        # The community Xiaohongshu bridge verifies a bearer token.  It is kept
        # separate from news HTTP credentials and only read for loopback calls.
        xiaohongshu_token=(
            pick("XHS_MCP_AUTH_TOKEN", "xiaohongshu_token")
            or pick("XHS_AUTH_TOKEN", "xiaohongshu_token")
        ),
        console_token=pick("BOT_CONSOLE_TOKEN", "console_token"),
        source=source,
    )
    return secrets


def load_config(
    config_path: str | Path | None = None,
    *,
    root: Path | None = None,
    require_file: bool = False,
) -> Config:
    """Load configuration.  Missing file is allowed (defaults) unless required."""
    # 配置文件的选择顺序（越靠前优先级越高）：
    #   1. 函数参数 config_path
    #   2. 环境变量 BOT_CONFIG
    #   3. 项目根目录下的 config/bot.toml
    #   4. 代码所在项目的默认 config/bot.toml
    #   5. 都没有 -> 只用 DEFAULTS（离线演示仍能跑）
    # require_file=True 时，找不到文件直接报错，适合正式部署。
    env_path = os.environ.get("BOT_CONFIG")
    base_root = Path(root) if root else PROJECT_ROOT
    chosen: Path | None
    if config_path:
        chosen = Path(config_path)
    elif env_path:
        chosen = Path(env_path)
    elif (base_root / "config" / "bot.toml").exists():
        chosen = base_root / "config" / "bot.toml"
    elif DEFAULT_CONFIG.exists():
        chosen = DEFAULT_CONFIG
    else:
        chosen = None

    if chosen is not None and not chosen.is_absolute():
        chosen = (Path.cwd() / chosen).resolve()

    data = copy.deepcopy(DEFAULTS)
    if chosen is not None and chosen.exists():
        data = _deep_merge(data, _load_toml(chosen))
    elif require_file:
        raise ConfigError(f"config file not found: {chosen or DEFAULT_CONFIG}")

    if root is None:
        if chosen is not None and chosen.parent.name == "config":
            root = chosen.parent.parent
        elif chosen is not None:
            root = chosen.parent
        else:
            root = PROJECT_ROOT
    root = Path(root).resolve()

    # Environment overrides are intentionally narrow and explicit.
    # 只允许这几个明确列出的环境变量覆盖配置，而不是“任意 BOT_* 都生效”。
    # 范围越小，越不容易因为环境里某个无关变量导致行为突变。
    env_map = {
        "BOT_MODE": ("app", "mode"),
        "BOT_LLM_PROVIDER": ("llm", "provider"),
        "BOT_NOTIFY_PROVIDER": ("notify", "provider"),
        "BOT_PUBLISHER_PROVIDER": ("publisher", "provider"),
        "BOT_DB_PATH": ("app", "db_path"),
        "BOT_DATA_DIR": ("app", "data_dir"),
        "BOT_CONSOLE_PORT": ("console", "port"),
        "BOT_PUBLISHER_URL": ("publisher", "xiaohongshu", "base_url"),
    }
    for env_name, path in env_map.items():
        value = os.environ.get(env_name)
        if value is None or value == "":
            continue
        node = data
        for part in path[:-1]:
            node = node.setdefault(part, {})
        leaf = path[-1]
        if leaf == "port":
            try:
                value = int(value)
            except ValueError as exc:
                raise ConfigError(f"{env_name} must be an integer") from exc
        node[leaf] = value

    if "BOT_PAUSE_ALL" in os.environ:
        data.setdefault("app", {})["pause_all"] = os.environ["BOT_PAUSE_ALL"].strip().lower() in {
            "1",
            "true",
            "yes",
        }

    cfg = Config(data=data, root=root, path=chosen)
    cfg.secrets = _env_secrets(root, data)
    # Secrets may also be supplied directly in the (untracked) config file for
    # Lark IDs, but never for API keys.
    notify_cfg = cfg.data.setdefault("notify", {}).setdefault("lark", {})
    if not notify_cfg.get("owner_open_id") and cfg.secrets.lark_owner_open_id:
        notify_cfg["owner_open_id"] = cfg.secrets.lark_owner_open_id
    if not notify_cfg.get("chat_id") and cfg.secrets.lark_chat_id:
        notify_cfg["chat_id"] = cfg.secrets.lark_chat_id
    return cfg

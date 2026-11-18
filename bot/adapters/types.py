"""Shared adapter types.

Adapters never mutate persistent state.  Business rules live in services.
"""

# 中文说明：
# 这个文件定义适配器之间通用的“数据形状”和“异常类型”。可以把它想成
# 快递行业的标准包装箱和标准异常标签：
#   - SourceEntry / SourceFetch   一篇新闻、一次抓取结果
#   - LLMResponse / TokenUsage    模型返回的 JSON、用了多少 token
#   - PublishOutcome              发布结果（已发布/模拟/失败/未知）
#   - AdapterError 家族           不同错误，调用方据此决定“能不能重试”
#
# 最关键的区分是发布结果：
#   published  平台给了真实 post_id 或 URL —— 只有这种才算真发布
#   simulated  只写了本地模拟文件 —— 绝不能对外说“已发布”
#   failed     确定没发出去 —— 可以记录失败
#   unknown    可能已经发出去了 —— 绝不能自动重试，必须人工核对
# 这个区分是整个项目最重要的安全设计之一。

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class AdapterError(RuntimeError):
    # retryable 是“这个错误重试有没有意义”的默认标记。
    # 例如网络超时可以重试；缺少 API key 重试一万次也没用。
    retryable = True

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        status: int | None = None,
        code: str | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        # Stable, machine-readable diagnostic code (never user-facing text).
        # Existing call sites keep working because this is optional.
        self.code = code


class SourceError(AdapterError):
    # 抓取来源时的错误（网络、HTTP 状态、XML 解析等）。
    pass


class NotConfigured(AdapterError):
    # 配置缺失，例如没装 lark-oapi、没有 API key、真实发布开关没打开。
    # 这类错误永远不该自动重试，必须由人去补配置。
    retryable = False

    def __init__(self, message: str):
        super().__init__(message, retryable=False)


class LLMError(AdapterError):
    # 调用大模型失败（网络、服务端错误、返回格式不对等）。
    pass


class BudgetExceeded(LLMError):
    # 当天预算/调用次数用完。不是故障，而是保护机制生效。
    retryable = False


class LLMOutputError(LLMError):
    # 模型返回的内容不符合要求（不是合法 JSON、缺章节、引用不存在等）。
    # 重试可能有用，但继续重试也可能持续烧钱，所以这里标为不可重试，
    # 交给上层决定是否换一轮生成。
    retryable = False


class PublishFailed(AdapterError):
    """The request definitely did not create a post."""
    # 注意 docstring：能确定“没有创建帖子”才用这个异常。


class PublishUnknown(AdapterError):
    """A request may have reached the platform; never auto-retry."""
    # 请求可能已经到了平台，只是我们没拿到结果。自动重试可能造成重复发帖。


@dataclass
class SourceEntry:
    # 来源里的一篇文章。这里刻意只保留纯文本字段：
    # HTML 在采集阶段就会被清洗掉，后续环节不再接触网页指令。
    title: str
    url: str
    summary: str = ""
    content: str = ""
    author: str = ""
    published_raw: str = ""  # 原始时间字符串，交给 timeutil 解析


@dataclass
class SourceFetch:
    # 一次抓取的完整结果。
    entries: list[SourceEntry]
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False  # 服务端返回 304，内容没变
    status: int = 200
    note: str = ""
    # Optional, backward-compatible structured diagnostic payload.  Adapters
    # must keep secrets and long raw responses out of this dictionary; it is
    # counts/stages/codes only.  Existing adapters may leave it empty.
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass
class TokenUsage:
    # 大模型的用量。字段可能是 None——表示“服务端没告诉我们”，
    # 这时预算模块会按保守金额预留，而不是当成 0 花费。
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

    @property
    def known(self) -> bool:
        # 只要总量已知，或输入/输出都已知，就算“用量已知”。
        return self.total_tokens is not None or (
            self.prompt_tokens is not None and self.completion_tokens is not None
        )


@dataclass
class LLMResponse:
    # data 是已经解析好的 JSON 字典，raw_text 是模型原始文本。
    # 两者都保留，方便出错时审计。
    data: dict[str, Any]
    raw_text: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: int = 0
    model: str = ""
    provider: str = ""


@dataclass
class PublishOutcome:
    # published: a real platform receipt; simulated: local export only;
    # failed: definitely not sent; unknown: possibly sent, needs reconciliation.
    state: str
    detail: str = ""
    external_post_id: str | None = None
    external_url: str | None = None
    receipt: dict[str, Any] = field(default_factory=dict)
    # Structured failure metadata.  The agent must not infer retryability
    # from human-readable error strings.  ``failure_class`` is one of
    # not_dispatched/login_expired/asset_missing/rejected/unknown_result.
    failure_class: str = ""
    retryable: bool = False

    @property
    def is_real(self) -> bool:
        # 只有 published 才算真实发布；simulated 永远不算。
        return self.state == "published"

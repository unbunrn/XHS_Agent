"""Feishu/Lark long-connection approval gateway.

This uses the official ``lark-oapi`` SDK in WebSocket/long-connection mode, so
no public inbound port is required.  The SDK is optional: if it is absent the
gateway reports ``NotConfigured`` instead of falling back to a webhook or
pretending to be connected.

The callback path deliberately reuses the same approval service as the local
console and the tests: owner open_id, p2p chat, message-id dedupe, bound
artifact, expiry, and exactly-once publish job.
"""

# =============================================================================
# 中文导读：飞书长连接审批网关
# =============================================================================
# 前面的 LarkNotifier 负责“把审批消息发出去”；
# 这个文件负责反方向：“把用户在飞书里的回复收进来”。
#
# 用的是飞书官方 SDK 的“长连接”模式：程序主动连出去，
# 不需要公网 IP、不需要开放入站端口、不需要 webhook 回调地址。
# 这对本机运行的小工具非常重要——不用把家里的电脑暴露到公网。
#
# 三条安全边界：
#   1. lark-oapi 是可选依赖。没装就明确报 NotConfigured，
#      绝不退回 webhook 或假装“已连接”；
#   2. 收到消息后不自己判断权限，而是原样交给
#      services.approvals.handle_inbound_message：
#      owner open_id、私聊 p2p、消息 ID 幂等、审批绑定、有效期，
#      全部复用同一套逻辑；
#   3. SDK 回调里任何异常都必须吞掉并记日志，
#      否则一条坏消息会把整个接收循环搞崩。
# =============================================================================

from __future__ import annotations

import importlib.util
import json
from typing import Any

from .adapters.types import NotConfigured
from .config import Config
from .db import Database
from .services.approvals import configured_owner, handle_inbound_message
from .timeutil import Clock, SystemClock


def parse_message_event(event: Any) -> dict[str, Any] | None:
    """Extract the fields we need from a lark-oapi P2ImMessageReceiveV1 event.

    The function accepts either SDK objects or plain dicts (the latter is handy
    for replaying a captured event in tests without installing the SDK).
    """

    # 中文：飞书 SDK 给的是嵌套对象，测试里常用普通字典。
    # 这个小工具同时支持两种：字典用 .get，对象用 getattr，
    # 一层层往下走，任何一层缺失都返回默认值，不会抛异常。
    def get(obj: Any, *path: str, default: Any = None) -> Any:
        node = obj
        for part in path:
            if node is None:
                return default
            if isinstance(node, dict):
                node = node.get(part, default)
            else:
                node = getattr(node, part, default)
        return node

    content = get(event, "event", "message", "content") or get(event, "message", "content") or "{}"
    # 飞书消息的 content 是一个 JSON 字符串，例如 {"text": "批准 XH-ABC123"}。
    # 解析失败就当作空内容，让上层按“非文本消息”忽略掉。
    try:
        parsed_content = json.loads(content) if isinstance(content, str) else content
    except json.JSONDecodeError:
        parsed_content = {}
    text = ""
    if isinstance(parsed_content, dict):
        text = str(parsed_content.get("text") or "")
    sender = get(event, "event", "sender", "sender_id", "open_id") or get(
        event, "sender", "sender_id", "open_id"
    )
    message = get(event, "event", "message") or get(event, "message") or {}
    if not message:
        # 没有 message 字段说明这不是一条可处理的消息事件。
        return None
    return {
        "message_id": str(get(message, "message_id", default="") or ""),
        "chat_id": str(get(message, "chat_id", default="") or ""),
        "chat_type": str(get(message, "chat_type", default="") or ""),
        "sender_id": str(sender or ""),
        "text": text.strip(),
        "create_time": str(get(message, "create_time", default="") or ""),
    }


class LarkLongConnection:
    name = "lark-long-connection"

    def __init__(self, config: Config, db: Database, clock: Clock | None = None, llm: Any | None = None):
        # owner 用 configured_owner 统一解析（secrets 优先，其次普通配置）。
        # 没有 owner 时网关仍然能创建，但 start() 会拒绝启动，
        # 因为群里的任何人都可能发“批准”，那是绝对不行的。
        self.config = config
        self.db = db
        self.clock = clock or SystemClock()
        self.llm = llm
        self.app_id = config.secrets.lark_app_id
        self.app_secret = config.secrets.lark_app_secret
        self.owner_open_id = configured_owner(config)
        self._sdk = None
        self._sdk_error = None
        if importlib.util.find_spec("lark_oapi") is None:
            # 先探测再导入：没装 SDK 时给出明确原因，
            # 而不是让 ImportError 在某个深层调用里突然冒出来。
            self._sdk_error = "lark-oapi is not installed (install it to enable the long connection)"
        else:
            try:
                import lark_oapi as lark  # type: ignore

                self._sdk = lark
            except Exception as exc:  # noqa: BLE001
                self._sdk_error = f"lark-oapi import failed: {exc}"

    def health(self) -> dict[str, Any]:
        # ok 只表示“具备启动条件”，不表示“已经连上飞书”。
        # verified 固定 False：这次检查没有建立任何连接。
        return {
            "ok": bool(self._sdk is not None and self.app_id and self.app_secret and self.owner_open_id),
            "sdk_installed": self._sdk is not None,
            "app_configured": bool(self.app_id and self.app_secret),
            "owner_bound": bool(self.owner_open_id),
            "mode": "long_connection",
            "verified": False,
            "detail": self._sdk_error
            or (
                "long connection can start; no connection has been opened by this check"
                if self.app_id and self.app_secret and self.owner_open_id
                else "app credentials or owner open_id are missing"
            ),
        }

    def handle_event(self, event: Any, *, channel: str = "lark") -> dict[str, Any]:
        # 把 SDK 事件转成统一输入，再交给审批服务。
        # 没有 message_id 就无法做幂等，不能处理；
        # 空文本/非文本消息（图片、表情等）直接忽略。
        parsed = parse_message_event(event)
        if parsed is None or not parsed["message_id"]:
            return {"ok": False, "outcome": "invalid_event", "detail": "missing message_id"}
        if not parsed["text"]:
            return {"ok": False, "outcome": "ignored", "detail": "non-text or empty message"}
        return handle_inbound_message(
            self.db,
            self.config,
            self.clock,
            message_id=parsed["message_id"],
            sender_id=parsed["sender_id"],
            chat_id=parsed["chat_id"],
            chat_type=parsed["chat_type"],
            text=parsed["text"],
            channel=channel,
            llm=self.llm,
        )

    def _on_message(self, data: Any) -> None:
        # 这是注册给 SDK 的回调。它运行在 SDK 的接收循环里，
        # 所以绝不能把异常抛出去：一条坏消息不能让整个网关停摆。
        try:
            result = self.handle_event(data)
            # Do not log message text or detailed error/help bodies: a bad URL
            # or a writer/model reason can contain user content.  Outcome and
            # opaque request identifiers are enough for operational tracing.
            safe = {
                key: result[key]
                for key in (
                    "ok",
                    "outcome",
                    "duplicate",
                    "request_code",
                    "request_id",
                    "job_id",
                )
                if key in result
            }
            self._log.info("lark message outcome: %s", safe)
            try:
                from .adapters.notify import build_notifier
                from .services.notifications import deliver_pending

                notifier = getattr(self, "_notifier", None)
                if notifier is None:
                    notifier = build_notifier(self.config, self.clock)
                    self._notifier = notifier
                deliver_pending(self.db, self.config, self.clock, notifier)
            except Exception as notify_exc:  # noqa: BLE001 - worker will retry pending notifications
                self._log.debug("immediate notification flush skipped: %s", type(notify_exc).__name__)
        except Exception as exc:  # noqa: BLE001 - callback must never crash the SDK loop
            self._log.error("lark message handling failed: %s", type(exc).__name__)

    def start(self) -> None:
        # 启动前把三个必要条件一次查清：SDK、应用凭据、owner。
        # 任何一项缺失都抛 NotConfigured，绝不在“半配置”状态下连接。
        if self._sdk is None:
            raise NotConfigured(self._sdk_error or "lark-oapi is not installed")
        if not self.app_id or not self.app_secret:
            raise NotConfigured("LARK_APP_ID / LARK_APP_SECRET are not configured")
        if not self.owner_open_id:
            raise NotConfigured("LARK_OWNER_OPEN_ID is not configured; group approvals are refused")
        import logging

        self._log = logging.getLogger("bot.lark")
        lark = self._sdk
        try:
            # 注册“收到私聊消息”的回调，然后建立 WebSocket 长连接。
            # client.start() 是阻塞调用，会一直运行到进程退出或连接失败。
            handler = (
                lark.EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(self._on_message)
                .build()
            )
            client = lark.ws.Client(
                self.app_id,
                self.app_secret,
                event_handler=handler,
                log_level=getattr(lark.LogLevel, "INFO", 20),
            )
            client.start()
        except NotConfigured:
            raise
        except Exception as exc:  # noqa: BLE001
            raise NotConfigured(f"failed to start Feishu long connection: {exc}") from exc


def build_gateway(
    config: Config, db: Database, clock: Clock | None = None, llm: Any | None = None
) -> LarkLongConnection:
    # 工厂函数：方便测试/脚本替换依赖，也保持调用方不需要知道构造细节。
    return LarkLongConnection(config, db, clock=clock, llm=llm)


def main(config: Config, db: Database, clock: Clock | None = None, llm: Any | None = None) -> int:
    # 先打印健康信息（能不能启动），再真正启动并阻塞。
    gateway = LarkLongConnection(config, db, clock=clock, llm=llm)
    print(json.dumps(gateway.health(), ensure_ascii=False, indent=2))
    gateway.start()
    return 0

"""Notification adapters (approval messages).

Fake mode writes the exact message to a local directory so the offline demo can
show what the operator would receive.  Lark mode uses the official SDK and only
sends to the configured owner open_id in a p2p chat.  A missing SDK or missing
credentials is reported as ``NotConfigured``; we never claim Lark is connected
without a real send receipt.
"""

# =============================================================================
# 中文导读：把审批单送到人手里
# =============================================================================
# 两个通道：
#   FakeNotifier  演示用：把消息写成 var/notifications/*.json 并在控制台打印，
#                 根本不会联系飞书；
#   LarkNotifier  真飞书：用官方 SDK，只发给配置里绑定的 owner open_id，
#                 并且是私聊（p2p），不是群聊。
#
# 关键原则：没拿到真实回执，就不能说“发送成功”。
#   - 没装 lark-oapi -> NotConfigured；
#   - 缺 app_id/secret/owner -> NotConfigured；
#   - 发送返回 success=False -> 报错。
#   演示模式会明确写“未发送到飞书”，避免用户误以为已经通知到了。
#
# 审批消息为什么把哈希也发出来？
#   用户批准的是“这一版正文 + 这一组图片”。哈希写在消息里，
#   之后任何改动都会让指纹对不上，审批自动失效。
# =============================================================================

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Config
from ..db import json_dumps
from ..timeutil import Clock, local_iso, to_iso
from .types import NotConfigured


@dataclass
class NotificationResult:
    # 一次发送的结果。ok=False 表示没发完整；
    # message_id 是平台回执（飞书返回的消息 ID），用于幂等和审计。
    # 图片附件是审批的一部分：正文成功但图片失败时 ok 必须为 False，
    # 这样通知队列会重试，飞书审批也不能在缺图时生效。
    ok: bool
    channel: str
    message_id: str | None = None
    detail: str = ""
    path: str | None = None
    images_sent: int = 0
    images_total: int = 0
    failed_images: tuple[str, ...] = ()


class Notifier:
    # 通知适配器接口：发送审批消息、报告自身健康状态。
    name = "base"

    def send_approval(self, message: dict[str, Any]) -> NotificationResult:  # pragma: no cover
        raise NotImplementedError

    def send_text(self, text: str, *, image_paths: list[str] | None = None) -> NotificationResult:
        # Compatibility fallback for simple test doubles.  Production notifiers
        # override this with a text-only send to the bound owner.
        return self.send_approval({"text": text, "image_paths": list(image_paths or []), "code": "outcome"})

    def health(self) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError


class FakeNotifier(Notifier):
    name = "fake"

    def __init__(self, config: Config, clock: Clock):
        self.config = config
        self.clock = clock
        self.out_dir = config.resolve_path(str(config.get("notify.fake_dir", "var/notifications")))

    def send_approval(self, message: dict[str, Any]) -> NotificationResult:
        # 写本地 JSON 文件，文件名带审批码和时间戳，避免覆盖。
        # 同时 print 到控制台，演示时用户能直接看到审批内容。
        self.out_dir.mkdir(parents=True, exist_ok=True)
        stamp = to_iso(self.clock.now()).replace(":", "").replace("-", "")[:15]
        path = self.out_dir / f"approval-{message['code']}-{stamp}.json"
        payload = {
            "channel": "fake",
            "created_at": to_iso(self.clock.now()),
            "notice": "本地假通知；没有发送到飞书。",
            "message": message,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            "\n--- 本地审批通知（未发送到飞书）---\n"
            + str(message["text"])
            + f"\n保存于: {path}\n"
        )
        return NotificationResult(
            ok=True,
            channel="fake",
            message_id=f"fake-{message['code']}-{stamp}",
            detail="written locally; Feishu was not contacted",
            path=str(path),
        )

    def send_text(self, text: str, *, image_paths: list[str] | None = None) -> NotificationResult:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        stamp = to_iso(self.clock.now()).replace(":", "").replace("-", "")[:15]
        path = self.out_dir / f"outcome-{stamp}-{uuid.uuid4().hex[:6]}.json"
        imgs = [str(p) for p in (image_paths or []) if str(p).strip()]
        path.write_text(
            json.dumps(
                {
                    "channel": "fake",
                    "created_at": to_iso(self.clock.now()),
                    "notice": "本地假通知；没有发送到飞书。",
                    "text": text,
                    "image_paths": imgs,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return NotificationResult(
            ok=True,
            channel="fake",
            message_id=f"fake-outcome-{uuid.uuid4().hex[:6]}",
            detail="written locally; Feishu was not contacted",
            path=str(path),
            images_sent=len(imgs),
            images_total=len(imgs),
        )

    def health(self) -> dict[str, Any]:
        # 假通知永远“健康”，因为它不依赖任何外部服务；
        # detail 里再次说明没有联系飞书。
        return {
            "ok": True,
            "mode": "fake",
            "configured": True,
            "detail": "local notification files; Feishu is not contacted",
        }


class LarkNotifier(Notifier):
    name = "lark"

    def __init__(self, config: Config, clock: Clock):
        # 凭据优先从 secrets 读，其次从普通配置读（方便示例配置）。
        # 注意：真实使用时 app_secret 应该只放在环境变量或 secrets.toml。
        self.config = config
        self.clock = clock
        self.app_id = config.secrets.lark_app_id
        self.app_secret = config.secrets.lark_app_secret
        self.owner_open_id = config.secrets.lark_owner_open_id or str(
            config.get("notify.lark.owner_open_id", "")
        )
        self.chat_id = config.secrets.lark_chat_id or str(config.get("notify.lark.chat_id", ""))
        self._client = None
        self._sdk_error: str | None = None
        try:
            import lark_oapi as lark  # type: ignore

            self._sdk = lark
        except Exception as exc:  # noqa: BLE001
            # SDK 是可选的。没装不影响程序启动，只在真正要发飞书时报错。
            self._sdk = None
            self._sdk_error = f"lark-oapi is not installed: {exc}"

    def _ensure(self) -> None:
        # 发消息前的三项检查：SDK、应用凭据、owner open_id。
        # 任何一项缺失都抛 NotConfigured，绝不“试着发一下看看”。
        if self._sdk is None:
            raise NotConfigured(self._sdk_error or "lark-oapi is not installed")
        if not self.app_id or not self.app_secret:
            raise NotConfigured("LARK_APP_ID / LARK_APP_SECRET are not configured")
        if not self.owner_open_id:
            raise NotConfigured("LARK_OWNER_OPEN_ID is not configured (owner binding is required)")
        if self._client is None:
            builder = self._sdk.Client.builder().app_id(self.app_id).app_secret(self.app_secret)
            self._client = builder.build()

    def _send_text(self, text: str) -> str | None:
        # 发送纯文本消息。receive_id_type="open_id" 表示按用户 open_id 发给个人，
        # 不是发到群里。返回飞书的消息 ID，供审计和幂等使用。
        try:
            from lark_oapi.api.im.v1 import (  # type: ignore
                CreateMessageRequest,
                CreateMessageRequestBody,
            )
        except Exception as exc:  # noqa: BLE001
            raise NotConfigured(f"lark-oapi IM API is unavailable: {exc}") from exc
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(self.owner_open_id)
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(body)
            .build()
        )
        response = self._client.im.v1.message.create(request)
        success = bool(getattr(response, "success", lambda: False)())
        if not success:
            code = getattr(response, "code", None)
            msg = getattr(response, "msg", None)
            raise NotConfigured(f"Feishu send failed: code={code} msg={msg}")
        data = getattr(response, "data", None)
        message_id = getattr(data, "message_id", None) if data is not None else None
        return str(message_id) if message_id else None

    def _upload_image(self, path: str) -> str:
        # 先把本地 PNG 上传到飞书，拿到 image_key，再用它发图片消息。
        # 用 with 打开文件，确保无论成功失败都关闭句柄。
        try:
            from lark_oapi.api.im.v1 import (  # type: ignore
                CreateImageRequest,
                CreateImageRequestBody,
            )
        except Exception as exc:  # noqa: BLE001
            raise NotConfigured(f"lark-oapi image API is unavailable: {exc}") from exc
        try:
            handle = open(path, "rb")
        except OSError as exc:
            raise NotConfigured(f"cannot open approval image: {exc}") from exc
        with handle:
            body = (
                CreateImageRequestBody.builder()
                .image_type("message")
                .image(handle)
                .build()
            )
            request = CreateImageRequest.builder().request_body(body).build()
            response = self._client.im.v1.image.create(request)
        success = bool(getattr(response, "success", lambda: False)())
        if not success:
            code = getattr(response, "code", None)
            msg = getattr(response, "msg", None)
            raise NotConfigured(f"Feishu image upload failed: code={code} msg={msg}")
        data = getattr(response, "data", None)
        image_key = getattr(data, "image_key", None) if data is not None else None
        if not image_key:
            raise NotConfigured("Feishu image upload returned no image_key")
        return str(image_key)

    def _send_image(self, image_key: str) -> str | None:
        # 用上面拿到的 image_key 发一条图片消息。
        try:
            from lark_oapi.api.im.v1 import (  # type: ignore
                CreateMessageRequest,
                CreateMessageRequestBody,
            )
        except Exception as exc:  # noqa: BLE001
            raise NotConfigured(f"lark-oapi IM API is unavailable: {exc}") from exc
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(self.owner_open_id)
            .msg_type("image")
            .content(json.dumps({"image_key": image_key}))
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(body)
            .build()
        )
        response = self._client.im.v1.message.create(request)
        success = bool(getattr(response, "success", lambda: False)())
        if not success:
            code = getattr(response, "code", None)
            msg = getattr(response, "msg", None)
            raise NotConfigured(f"Feishu image message failed: code={code} msg={msg}")
        data = getattr(response, "data", None)
        message_id = getattr(data, "message_id", None) if data is not None else None
        return str(message_id) if message_id else None

    def send_approval(self, message: dict[str, Any]) -> NotificationResult:
        # 先发文字（审批码、哈希、命令），再逐张发图片。
        # 关键：正文和每一张图片都属于审批材料。任何一张图失败，
        # ok 必须为 False，让通知队列保持 retry/可见错误；
        # 飞书审批也会检查通知是否完整送达后才允许生效。
        # 这里不重发文字（避免用户收到重复审批提示），只把失败原因返回。
        self._ensure()
        message_id = self._send_text(str(message["text"]))
        image_paths = list(message.get("image_paths") or [])
        sent_images: list[str] = []
        failures: list[str] = []
        for index, path in enumerate(image_paths, start=1):
            try:
                image_key = self._upload_image(str(path))
                image_message_id = self._send_image(image_key)
                if not image_message_id:
                    failures.append(f"image {index}: no message id returned")
                else:
                    sent_images.append(image_message_id)
            except Exception as exc:  # noqa: BLE001 - report, do not claim images were sent
                failures.append(f"image {index}: {type(exc).__name__}: {exc}")
        complete = not failures and len(sent_images) == len(image_paths)
        detail = (
            "sent final text and every image to owner open_id via Feishu p2p chat"
            if complete
            else "INCOMPLETE delivery: final text may have been sent but not every image was confirmed"
        )
        if sent_images:
            detail += f"; image messages: {len(sent_images)}/{len(image_paths)}"
        if failures:
            detail += "; image failures: " + "; ".join(failures)
        return NotificationResult(
            ok=complete,
            channel="lark",
            message_id=message_id,
            detail=detail,
            images_sent=len(sent_images),
            images_total=len(image_paths),
            failed_images=tuple(failures),
        )

    def send_text(self, text: str, *, image_paths: list[str] | None = None) -> NotificationResult:
        # Text (plus optional images such as login QR) to the same bound owner
        # open_id (p2p).  This method deliberately has no recipient argument,
        # so a callback cannot choose an arbitrary sender/chat.
        self._ensure()
        message_id = self._send_text(text)
        paths = [str(p) for p in (image_paths or []) if str(p).strip()]
        if not paths:
            return NotificationResult(
                ok=True,
                channel="lark",
                message_id=message_id,
                detail="sent text to owner open_id via Feishu p2p chat",
            )
        sent_images: list[str] = []
        failures: list[str] = []
        for index, path in enumerate(paths, start=1):
            try:
                image_key = self._upload_image(path)
                image_message_id = self._send_image(image_key)
                if not image_message_id:
                    failures.append(f"image {index}: no message id returned")
                else:
                    sent_images.append(image_message_id)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"image {index}: {type(exc).__name__}: {exc}")
        complete = not failures and len(sent_images) == len(paths)
        detail = (
            "sent text and images to owner open_id via Feishu p2p chat"
            if complete
            else "INCOMPLETE delivery: text sent but image delivery failed: " + "; ".join(failures)
        )
        return NotificationResult(
            ok=complete,
            channel="lark",
            message_id=message_id,
            detail=detail,
            images_sent=len(sent_images),
            images_total=len(paths),
            failed_images=tuple(failures),
        )

    def health(self) -> dict[str, Any]:
        # 注意 ok 固定为 False、verified 固定为 False：
        # 健康检查没有真的发消息，就不能声称飞书通道已验证可用。
        # 它只告诉你“SDK 装了没、凭据齐不齐”。
        installed = self._sdk is not None
        configured = bool(self.app_id and self.app_secret and self.owner_open_id)
        detail = "SDK installed and credentials present; no message sent by this check"
        if not installed:
            detail = self._sdk_error or "lark-oapi is not installed"
        elif not configured:
            detail = "SDK installed, but app credentials or owner open_id are missing"
        return {
            "ok": False,
            "mode": "lark",
            "configured": configured,
            "sdk_installed": installed,
            "owner_bound": bool(self.owner_open_id),
            "chat_id_present": bool(self.chat_id),
            "detail": detail,
            "verified": False,
        }


def build_notifier(config: Config, clock: Clock) -> Notifier:
    # 工厂函数。未知 provider 直接报配置错误，不默认退回 fake。
    provider = str(config.get("notify.provider", "fake")).lower()
    if provider == "fake":
        return FakeNotifier(config, clock)
    if provider == "lark":
        return LarkNotifier(config, clock)
    raise NotConfigured(f"unsupported notify.provider: {provider}")


def approval_message(
    *,
    code: str,
    platform: str,
    account_id: str,
    draft_id: int,
    draft_version: int,
    title: str,
    body_sha256: str,
    images_sha256: str,
    scheduled_at: str,
    expires_at: str,
    timezone_name: str,
    preview_url: str | None = None,
    simulated_note: str | None = None,
    body: str = "",
    image_paths: list[str] | None = None,
    sources: list[str] | None = None,
    uncertainties: list[str] | None = None,
    review_note: str = "",
) -> dict[str, Any]:
    # 拼审批消息文本。包含：
    #   - 审批码、标题、平台/账号、草稿版本；
    #   - 正文和图片的哈希前缀（让用户知道批准的是哪一版）；
    #   - 计划发布时间、审批过期时间；
    #   - 三条命令：批准 / 拒绝 / 修改；
    #   - 一句重要提醒：改内容会让审批失效，超时不会发布。
    image_paths = list(image_paths or [])
    lines = [
        "【待审批】AI 工具观察",
        f"审批码：{code}",
        f"标题：{title}",
        f"平台/账号：{platform} / {account_id}",
        f"草稿：#{draft_id} v{draft_version}",
        f"正文 SHA256：{body_sha256[:16]}…",
        f"图片 SHA256：{images_sha256[:16]}…",
        f"计划发布：{local_iso(_parse(scheduled_at), timezone_name)}",
        f"审批过期：{local_iso(_parse(expires_at), timezone_name)}",
        f"图片：{len(image_paths)} 张（随后以飞书图片消息发送；正文与图片哈希均已绑定）",
    ]
    if preview_url:
        lines.append(f"本机预览（仅本机浏览器可打开）：{preview_url}")
    if simulated_note:
        lines.append(simulated_note)
    if review_note:
        lines.append(review_note)
    if body:
        lines.extend(["", "【正文】", body])
    if sources and "【来源】" not in body:
        lines.extend(["", "【来源】"] + [f"- {item}" for item in sources])
    if uncertainties and "【限制与不确定性】" not in body:
        lines.extend(["", "【限制与不确定性】"] + [f"- {item}" for item in uncertainties])
    lines.extend(
        [
            "",
            f"批准：批准 {code}",
            f"拒绝：拒绝 {code}",
            f"修改：修改 {code} 你的修改意见",
            "注意：修改正文或图片会使本次审批失效；超时或未回复不会发布。",
            "本消息由机器人发送；外部网页内容仅作资料，不会改变权限。",
        ]
    )
    return {
        "code": code,
        "platform": platform,
        "account_id": account_id,
        "draft_id": draft_id,
        "draft_version": draft_version,
        "title": title,
        "body_sha256": body_sha256,
        "images_sha256": images_sha256,
        "scheduled_at": scheduled_at,
        "expires_at": expires_at,
        "text": "\n".join(lines),
        "body": body,
        "image_paths": image_paths,
        "sources": list(sources or []),
        "uncertainties": list(uncertainties or []),
        "created_at": to_iso(_parse(scheduled_at)),
    }


def _parse(value: str):
    # 把 ISO 时间字符串解析成 datetime；格式不对就抛 ValueError。
    # 审批消息里的时间必须能解析，否则宁可报错也不显示一个错误时间。
    from ..timeutil import parse_iso

    parsed = parse_iso(value)
    if parsed is None:
        raise ValueError(f"invalid timestamp: {value}")
    return parsed

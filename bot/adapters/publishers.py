"""Publishing adapters.

There are two deliberately separate worlds:

* ``MockPublisher`` never calls the network.  It writes a clearly-labelled
  simulated export and always returns ``state='simulated'``.  Nothing in the
  database or UI may call that a published post.
* ``XiaohongshuPublisher`` calls an explicitly configured **local** bridge
  (community browser automation, not an official API).  A publish is
  ``published`` only when the bridge returns a real post id/URL, or when the
  authenticated account feed shows exactly one brand-new note whose title
  matches the approved draft after a 2xx-without-id response.  A request whose
  outcome cannot be confirmed that way is ``unknown`` and is never retried
  automatically.

The publisher is constructed only inside the publish gate; no CLI or model
tool exposes a generic "publish anything" call.
"""

# =============================================================================
# 中文导读：发布适配器——全项目最需要谨慎的地方
# =============================================================================
# 两种“发布”必须严格区分：
#
#   1. MockPublisher（模拟）
#      不联网，只在 var/exports 下写一个 JSON + 文本 + 图片副本。
#      返回的状态永远是 simulated。数据库和界面都不能把它叫“已发布”。
#
#   2. XiaohongshuPublisher（真实）
#      调用本机运行的小红书浏览器自动化桥接（不是官方 API）。
#      只有桥接返回真实的 post_id 或 URL，才返回 published。
#
# 发布结果的四种状态（再强调一次）：
#   published  拿到真实回执，确实发出去了
#   simulated  只是本地导出，没有对外发布
#   failed     确定没发出去，可以记录失败
#   unknown    可能发出去了，也可能没发；绝不自动重试，必须人工核对
#
# 为什么本机桥接也只用 loopback？
#   桥接需要 bearer token。如果 base_url 指向外部主机，token 就可能
#   被发到别人机器上。所以这里强制只允许 127.0.0.1/localhost/::1。
# =============================================================================

from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..config import Config
from ..httpclient import HttpClient, HttpTransportError
from ..security import SSRFGuard
from ..timeutil import Clock, to_iso
from .types import NotConfigured, PublishFailed, PublishOutcome, PublishUnknown


@dataclass
class PublishRequest:
    # 一次发布请求的完整快照。哈希字段让桥接端可以再次核对
    # “你发的正文/图片，是不是审批时那一版”。
    draft_id: int
    draft_version: int
    title: str
    body: str
    body_sha256: str
    images: list[dict[str, Any]]  # {ordinal,path,sha256,width,height}
    platform: str
    account_id: str
    approval_code: str
    scheduled_at: str
    tags: list[str]


_URL_OR_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validated_host_root(config: Config, value: str) -> Path:
    """Resolve the host image root and keep it inside the project.

    ``resolve(strict=False)`` follows existing symlinks/junctions, so a root
    that itself points outside the project is rejected.  The directory may not
    exist yet; each image is required to exist and resolve under it at publish
    time.
    """
    if not isinstance(value, str) or not value.strip():
        raise NotConfigured("publisher.xiaohongshu.image_host_root must be a non-empty path")
    if value != value.strip():
        raise NotConfigured("publisher.xiaohongshu.image_host_root must not have outer whitespace")
    if "\x00" in value or "://" in value:
        raise NotConfigured("publisher.xiaohongshu.image_host_root must be a local filesystem path")
    project_root = config.root.resolve()
    try:
        resolved = config.resolve_path(value).resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise NotConfigured(
            f"publisher.xiaohongshu.image_host_root cannot be resolved: {type(exc).__name__}"
        ) from exc
    if not _is_within(resolved, project_root):
        raise NotConfigured(
            "publisher.xiaohongshu.image_host_root must resolve inside the project root"
        )
    if resolved.exists() and not resolved.is_dir():
        raise NotConfigured("publisher.xiaohongshu.image_host_root must be a directory")
    return resolved


def _validated_container_root(value: str) -> str:
    """Return a strict, normalized absolute POSIX container path.

    The bridge runs on Linux, so Windows drive paths, UNC/backslash paths,
    URLs and ``..`` traversal are refused before any request can be sent.
    """
    if not isinstance(value, str) or not value:
        raise NotConfigured("publisher.xiaohongshu.image_container_root must be a non-empty path")
    if value != value.strip():
        raise NotConfigured("publisher.xiaohongshu.image_container_root must not have outer whitespace")
    if "\\" in value:
        raise NotConfigured("publisher.xiaohongshu.image_container_root must use POSIX separators")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise NotConfigured("publisher.xiaohongshu.image_container_root must not contain control characters")
    if _WINDOWS_DRIVE_RE.match(value):
        raise NotConfigured("publisher.xiaohongshu.image_container_root must not have a drive prefix")
    if "://" in value or _URL_OR_SCHEME_RE.match(value):
        raise NotConfigured("publisher.xiaohongshu.image_container_root must not be a URL")
    if not value.startswith("/") or value.startswith("//"):
        raise NotConfigured("publisher.xiaohongshu.image_container_root must be an absolute POSIX path")
    if any(part in {".", ".."} for part in value.split("/")):
        raise NotConfigured("publisher.xiaohongshu.image_container_root must not contain traversal")
    if posixpath.normpath(value) != value:
        raise NotConfigured("publisher.xiaohongshu.image_container_root must be normalized")
    return value


class Publisher:
    # 发布器接口。mode 用于区分 mock / live / disabled。
    name = "base"
    mode = "mock"

    def publish(self, request: PublishRequest) -> PublishOutcome:  # pragma: no cover - interface
        raise NotImplementedError

    def health(self) -> dict[str, Any]:  # pragma: no cover - interface
        raise NotImplementedError


class MockPublisher(Publisher):
    name = "mock"
    mode = "mock"

    def __init__(self, config: Config, clock: Clock):
        self.config = config
        self.clock = clock
        self.export_dir = config.resolve_path(str(config.get("publisher.export_dir", "var/exports")))

    def publish(self, request: PublishRequest) -> PublishOutcome:
        # 写三种文件：
        #   .json 清单（结构化，方便程序读）
        #   .txt  纯文本（方便人直接看）
        #   图片副本（与审批时字节完全一致）
        # manifest 里明确写 simulated=True 和“不是真实发布”，
        # 防止日后有人误把导出文件当成已发布证据。
        self.export_dir.mkdir(parents=True, exist_ok=True)
        stamp = to_iso(self.clock.now()).replace(":", "").replace("-", "")[:15]
        key = f"draft-{request.draft_id}-v{request.draft_version}-{stamp}"
        manifest_path = self.export_dir / f"{key}.json"
        text_path = self.export_dir / f"{key}.txt"
        manifest = {
            "simulated": True,
            "notice": "这是本地模拟发布/导出，不是小红书真实发布，没有平台 post_id 或 URL。",
            "created_at": to_iso(self.clock.now()),
            "draft_id": request.draft_id,
            "draft_version": request.draft_version,
            "title": request.title,
            "body": request.body,
            "body_sha256": request.body_sha256,
            "images": request.images,
            "platform": request.platform,
            "account_id": request.account_id,
            "approval_code": request.approval_code,
            "scheduled_at": request.scheduled_at,
            "tags": request.tags,
            "manifest_path": str(manifest_path),
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        text_path.write_text(
            "\n".join(
                [
                    "【模拟导出，不是真实发布】",
                    f"标题：{request.title}",
                    "",
                    request.body,
                    "",
                    "图片：",
                    *[f"- {img['path']} sha256={img['sha256']}" for img in request.images],
                ]
            ),
            encoding="utf-8",
        )
        # Copy the exact approved bytes next to the manifest for manual review.
        # 复制而不是移动：原始图片仍留在草稿目录，审批哈希仍然有效。
        # 复制的是“已批准的字节”，不重新渲染、不压缩，保证可核对。
        for image in request.images:
            source = Path(str(image["path"]))
            target = self.export_dir / f"{key}-{int(image['ordinal']):02d}{source.suffix or '.png'}"
            if source.exists():
                shutil.copyfile(source, target)
        return PublishOutcome(
            state="simulated",
            detail="local mock export written; no external platform call was made",
            receipt={
                "simulated": True,
                "manifest_path": str(manifest_path),
                "text_path": str(text_path),
                "image_sha256": [img["sha256"] for img in request.images],
            },
        )

    def health(self) -> dict[str, Any]:
        # 模拟发布器永远是“健康”的，因为它不依赖网络；
        # detail 里明确写“绝不联系小红书”。
        return {
            "ok": True,
            "mode": "mock",
            "configured": True,
            "detail": "offline simulator; never contacts Xiaohongshu",
            "export_dir": str(self.export_dir),
        }


class XiaohongshuPublisher(Publisher):
    name = "xiaohongshu"
    mode = "live"

    def __init__(self, config: Config, clock: Clock, *, allow_unarmed: bool = False):
        # 初始化阶段就把所有“不安全/未配置”的情况挡住：
        #   1. 真实发布时四道开关必须全部打开；
        #   2. 必须有桥接 token；
        #   3. 必须有独立的“期望平台用户 ID”，不能用本地别名；
        #   4. base_url 必须是 http(s) 且主机是本机回环地址。
        # allow_unarmed=True 只用于只读健康检查（doctor）：它不会允许 publish()。
        self.config = config
        self.clock = clock
        base_url = str(config.get("publisher.xiaohongshu.base_url", "")).rstrip("/")
        self.base_url = base_url
        parts = urlsplit(base_url)
        self.host = parts.hostname or ""
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.token = config.secrets.xiaohongshu_token
        self.read_only = True
        self.armed = bool(config.is_live_publish_armed())
        # The upstream /api/v1/user/me documents data.data.userBasicInfo.redId
        # (the public account number), not an immutable internal user_id.
        expected = str(config.get("publisher.xiaohongshu.expected_red_id", "") or "").strip()
        if not expected:
            expected = os.environ.get("XHS_EXPECTED_RED_ID", "").strip()
        self.expected_red_id = expected
        legacy_expected = str(config.get("publisher.xiaohongshu.expected_user_id", "") or "").strip()
        if not self.expected_red_id and legacy_expected:
            raise NotConfigured(
                "expected_user_id is not documented by the bridge; configure expected_red_id "
                "(the public redId) instead"
            )
        self.login_status_path = str(
            config.get("publisher.xiaohongshu.login_status_path", "/api/v1/login/status")
        )
        self.user_me_path = str(
            config.get("publisher.xiaohongshu.user_me_path", "/api/v1/user/me")
        )
        # Snapshot of the authenticated account feed taken by _identity_check()
        # immediately before POST.  After a 2xx response without a receipt, a
        # brand-new note with this exact title is strong read-only evidence of
        # success; an unchanged/older feed never counts.
        self._pre_publish_feed_ids: set[str] | None = None
        # Optional Docker bridge path mapping.  Absent means the historical
        # native-Windows behaviour: send the absolute host path unchanged.
        self.image_host_root: Path | None = None
        self.image_container_root: str | None = None
        self._configure_image_mapping()
        if not allow_unarmed:
            if not self.armed:
                raise NotConfigured(
                    "real Xiaohongshu publishing is not armed "
                    "(requires app.mode=live, publisher.live_enabled=true, "
                    "publisher.provider=xiaohongshu, BOT_ALLOW_REAL_PUBLISH=1, real sources+DeepSeek)"
                )
            if not self.token:
                raise NotConfigured("XHS_MCP_AUTH_TOKEN / secrets.xiaohongshu_token is missing")
            if not self.expected_red_id:
                raise NotConfigured(
                    "publisher.xiaohongshu.expected_red_id (or XHS_EXPECTED_RED_ID) "
                    "is required before real publishing; the local account alias is not enough"
                )
            alias = str(config.account().get("id", "") or "").strip()
            if alias and self.expected_red_id == alias:
                raise NotConfigured(
                    "expected_red_id must be the public redId from /api/v1/user/me, "
                    "not the local account alias"
                )
            self.read_only = False
        if parts.scheme not in {"http", "https"} or not self.host:
            raise NotConfigured("publisher.xiaohongshu.base_url must be an absolute http(s) URL")
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            # The bridge is browser automation on this machine; refuse to send a
            # bearer token to any other host even if "armed".
            # 即使所有开关都打开了，也不允许把 token 发到非本机地址。
            # 这是“纵深防御”：不指望用户永远不写错配置。
            raise NotConfigured("publisher.xiaohongshu.base_url must point at loopback")
        guard = SSRFGuard(allow_private_hosts=[self.host])
        guard.validate_local_service(base_url)
        # max_retries=0：发布请求绝不能由 HTTP 层自动重试。
        # 重试可能造成重复发帖；失败后的处理交给 services.publish 的
        # “unknown 不自动重试”规则。
        self.http = HttpClient(
            guard=guard,
            user_agent="BotOperator/0.1 (xhs-local-bridge)",
            timeout=float(config.get("publisher.xiaohongshu.timeout_seconds", 60)),
            max_bytes=2_000_000,
            min_host_interval=0.0,
            max_retries=0,
        )

    def _configure_image_mapping(self) -> None:
        raw_host = self.config.get("publisher.xiaohongshu.image_host_root")
        raw_container = self.config.get("publisher.xiaohongshu.image_container_root")
        if raw_host is None and raw_container is None:
            return
        if raw_host is None or raw_container is None:
            raise NotConfigured(
                "publisher.xiaohongshu.image_host_root and image_container_root "
                "must be configured together"
            )
        self.image_host_root = _validated_host_root(self.config, raw_host)
        self.image_container_root = _validated_container_root(raw_container)

    def _resolve_mapped_image(self, image_path: str) -> tuple[Path, str]:
        """Resolve one existing image and translate only its relative path.

        The configured host root is compared after ``resolve(strict=True)``, so
        ``..`` escapes, prefix collisions, missing files, directories and
        symlink/junction escapes are refused before the publish POST.
        """
        if self.image_host_root is None or self.image_container_root is None:
            raise PublishFailed("image path mapping is not configured", retryable=False)
        try:
            actual = self.config.resolve_path(image_path).resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise PublishFailed(
                f"approved image file is missing or cannot be resolved: {image_path}",
                retryable=False,
            ) from exc
        if not _is_within(actual, self.image_host_root):
            raise PublishFailed(
                f"approved image file is outside the configured image host root: {image_path}",
                retryable=False,
            )
        if not actual.is_file():
            raise PublishFailed(
                f"approved image path is not a regular file: {image_path}",
                retryable=False,
            )
        relative = actual.relative_to(self.image_host_root)
        mapped = posixpath.join(self.image_container_root, relative.as_posix())
        if posixpath.normpath(mapped) != mapped:
            # Defensive only: a resolved descendant cannot contain traversal.
            raise PublishFailed("mapped image path is not normalized", retryable=False)
        return actual, mapped

    def _prepare_images(self, request: PublishRequest) -> list[str]:
        image_mode = str(self.config.get("publisher.xiaohongshu.image_mode", "path"))
        images: list[str] = []
        for image in request.images:
            image_path = str(image["path"])
            if self.image_host_root is not None:
                source, outgoing = self._resolve_mapped_image(image_path)
            else:
                source, outgoing = Path(image_path), image_path
            if image_mode == "base64":
                import base64

                images.append(
                    "data:image/png;base64,"
                    + base64.b64encode(source.read_bytes()).decode("ascii")
                )
            else:
                images.append(outgoing)
        return images

    def _headers(self) -> dict[str, str]:
        # 每次请求都带 bearer token。这个值不能写进日志。
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    def _get_json(self, path: str) -> tuple[int, dict[str, Any]]:
        response = self.http.get(
            self.base_url + path,
            headers=self._headers(),
            allow_private=True,
        )
        try:
            payload = response.json()
        except Exception:
            payload = {}
        return int(response.status), payload if isinstance(payload, dict) else {}

    def _login_check(self) -> tuple[bool, str, bool | None]:
        """Read-only login check.  Never sends content to the bridge."""
        if not self.token:
            return False, "bridge token is missing; cannot verify login", None
        try:
            status, payload = self._get_json(self.login_status_path)
        except Exception as exc:  # noqa: BLE001 - report failure without secrets
            return False, f"bridge login check failed: {type(exc).__name__}: {exc}", None
        if status in {401, 403}:
            return False, "bridge rejected the bearer token during login check", None
        if status != 200:
            return False, f"bridge login check returned HTTP {status}", None
        logged_in = _extract_logged_in(payload)
        if logged_in is False:
            return False, "bridge reports the Xiaohongshu account is not logged in", False
        # An absent/unknown login flag is not treated as success by itself;
        # /api/v1/user/me below must still return a concrete user id.
        return True, "login check did not report a failure", logged_in

    def _identity_check(self) -> tuple[bool, str, str | None]:
        """Ask the bridge which account is logged in, before any publish POST."""
        if not self.token:
            return False, "bridge token is missing; cannot verify account identity", None
        try:
            status, payload = self._get_json(self.user_me_path)
        except Exception as exc:  # noqa: BLE001 - do not send content after a failed GET
            return False, f"identity check failed: {type(exc).__name__}: {exc}", None
        if status in {401, 403}:
            return False, "bridge rejected the bearer token during identity check", None
        if status != 200:
            return False, f"identity check returned HTTP {status}", None
        if payload.get("success") is False:
            return False, "bridge reported identity check failure", None
        red_id = _extract_red_id(payload)
        if not red_id:
            # No documented identity field: refuse rather than guess a user_id
            # from an npm-style shape or a nickname.
            return False, "bridge /api/v1/user/me returned no data.data.userBasicInfo.redId", None
        if not self.expected_red_id:
            return False, "expected public redId is not configured", red_id
        if red_id != self.expected_red_id:
            return False, "logged-in redId does not match expected_red_id", red_id
        notes = _feed_notes(payload)
        if notes is not None:
            self._pre_publish_feed_ids = {note["id"] for note in notes}
        else:
            self._pre_publish_feed_ids = None
        return True, "identity matches expected public redId (not an immutable user_id)", red_id

    def health(self) -> dict[str, Any]:
        # 健康检查只读：查桥接的登录状态和当前账号身份，不发任何内容。
        # allow_unarmed=True 时即使真实发布开关关闭也可以做这项检查；
        # publish_ready 明确区分“桥接健康”与“允许真实发布”。
        result: dict[str, Any] = {
            "ok": False,
            "mode": "live",
            "configured": bool(self.token),
            "armed": self.armed,
            "read_only": self.read_only,
            "publish_ready": False,
            "base_url": self.base_url,
            "expected_red_id_configured": bool(self.expected_red_id),
            "identity_kind": "red_id",
            "logged_in": None,
            "identity_ok": None,
            "user_me_path": self.user_me_path,
        }
        if not self.token:
            result["detail"] = "bridge token is missing; cannot check login or identity"
            return result
        login_ok, login_detail, logged_in = self._login_check()
        result["logged_in"] = logged_in
        result["login_detail"] = login_detail
        if not login_ok:
            result["detail"] = login_detail
            return result
        identity_ok, identity_detail, red_id = self._identity_check()
        result["identity_ok"] = identity_ok
        result["identity_detail"] = identity_detail
        # Do not echo the full public account number; a short fingerprint is
        # enough for an operator to compare with the expected value out-of-band.
        if red_id:
            result["observed_red_id_sha256_12"] = _short_hash(red_id)
        result["ok"] = bool(identity_ok)
        result["publish_ready"] = bool(identity_ok and self.armed and not self.read_only)
        result["detail"] = (
            "bridge login and account identity verified"
            if identity_ok
            else identity_detail
        )
        return result

    def lookup_published_notes(self, *, since: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        """Read the authenticated account's recent note feed.

        The bridge's publish response often omits the note id, but the
        documented ``/api/v1/user/me`` response exposes the account's newest
        note cards.  ``since`` is accepted for the adapter interface but the
        feed itself is ordered newest-first; callers must compare against a
        pre-publish snapshot rather than trusting list position alone.
        """
        del since, limit
        status, payload = self._get_json(self.user_me_path)
        if status != 200 or payload.get("success") is False:
            raise NotConfigured("bridge account feed is unavailable")
        notes = _feed_notes(payload)
        if notes is None:
            raise NotConfigured("bridge account feed did not contain note cards")
        return notes

    def _feed_confirmation(
        self,
        request: PublishRequest,
        *,
        http_status: int,
        response_payload: Any,
    ) -> tuple[bool, PublishOutcome | None]:
        """Check the authenticated account feed for a new matching note.

        Returns ``(feed_read_ok, published_outcome)``.  ``feed_read_ok`` is
        False only when the feed itself could not be read; that distinction is
        what lets the caller keep an unreadable feed as ``unknown`` while
        treating a readable feed with no new note as a definite failure.

        Safety rules: a pre-POST snapshot must exist, exactly one new note id
        may appear, and its title must exactly match the draft.  An old note
        with the same title is not new and cannot confirm anything.
        """
        before = self._pre_publish_feed_ids
        if before is None:
            return False, None
        try:
            status, payload = self._get_json(self.user_me_path)
        except Exception:  # noqa: BLE001 - network failure stays unknown
            return False, None
        if status != 200 or payload.get("success") is False:
            return False, None
        notes = _feed_notes(payload)
        if notes is None:
            return False, None
        new_notes = [note for note in notes if note["id"] not in before]
        matches = [
            note for note in new_notes if _title_key(note["title"]) == _title_key(request.title)
        ]
        if len(matches) != 1:
            return True, None
        note = matches[0]
        return True, PublishOutcome(
            state="published",
            detail="bridge returned without a usable id; exactly one new matching note appeared on the account feed",
            external_post_id=note["id"],
            external_url=note["url"],
            receipt={
                "http_status": http_status,
                "response": response_payload,
                "verified_from_feed": True,
                "new_note": note,
                "confirmed_at": to_iso(self.clock.now()),
            },
        )

    def _confirm_new_note(
        self,
        request: PublishRequest,
        *,
        http_status: int,
        response_payload: Any,
    ) -> PublishOutcome | None:
        """2xx-without-id compatibility wrapper around ``_feed_confirmation``."""
        _feed_ok, outcome = self._feed_confirmation(
            request, http_status=http_status, response_payload=response_payload
        )
        return outcome

    def publish(self, request: PublishRequest) -> PublishOutcome:
        # 发布流程：
        #   0. 只读模式、未武装、缺少期望用户 ID 时直接拒绝（POST 之前）；
        #   1. 先确认桥接已登录，再查 /api/v1/user/me 确认当前账号身份；
        #   2. 把图片转成桥接要求的格式（本地路径或 base64 data URL）；
        #   3. 组装请求体，附带正文/图片哈希和审批码，供桥接端核对；
        #   4. POST 到本机桥接；
        #   5. 网络异常 -> unknown（请求可能已经到达浏览器）；
        #   6. 5xx -> unknown；4xx -> failed；success=false -> failed；
        #   7. 2xx 但没有 post_id/URL -> 先用发布前快照和账号 feed 做只读核对：
        #      只有“新增且标题完全一致”的唯一笔记才算成功；否则仍为 unknown。
        if self.read_only or not self.armed:
            raise NotConfigured("publisher is in read-only health-check mode; real publishing is disabled")
        if not self.expected_red_id:
            raise NotConfigured("expected public redId is not configured")
        login_ok, login_detail, _ = self._login_check()
        if not login_ok:
            # The POST has not been attempted, so this is a definite failure,
            # not an unknown publish.
            raise PublishFailed(login_detail, retryable=False)
        identity_ok, identity_detail, _user_id = self._identity_check()
        if not identity_ok:
            # Fail before POST: no content has been sent to the platform.
            raise PublishFailed(identity_detail, retryable=False)
        path = str(self.config.get("publisher.xiaohongshu.publish_path", "/api/v1/publish"))
        # Only outgoing image paths are translated.  The request metadata,
        # approval binding and local content hashes are left untouched.
        images = self._prepare_images(request)
        body = {
            "title": request.title,
            "content": request.body,
            "images": images,
            "tags": request.tags,
            "draft_id": request.draft_id,
            "draft_version": request.draft_version,
            "body_sha256": request.body_sha256,
            "image_sha256": [img["sha256"] for img in request.images],
            "approval_code": request.approval_code,
            "scheduled_at": request.scheduled_at,
        }
        try:
            response = self.http.post_json(
                self.base_url + path,
                body,
                headers=self._headers(),
                allow_private=True,
            )
        except HttpTransportError as exc:
            # The request may have reached the browser bridge before the
            # connection died.  We must not claim either success or failure.
            # 连接断了不代表请求没到。既不能说成功，也不能说失败，
            # 只能标为 unknown，交给人工核对。
            raise PublishUnknown(f"publish request outcome is unknown: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise PublishUnknown(f"publish request outcome is unknown: {type(exc).__name__}: {exc}") from exc

        try:
            payload = response.json()
        except Exception:
            payload = {}
        if response.status >= 500:
            # 服务端 5xx：它可能已经开始发布，也可能没有。先给平台/账号
            # feed 一个短暂确认窗口；只有唯一新增且标题完全一致才算发布。
            # feed 可读且始终无新增时，才把这次 5xx 视为平台拒绝；
            # feed 本身读不到时必须保持 unknown，不能猜。
            feed_read_ok, confirmed = self._feed_confirmation(
                request, http_status=response.status, response_payload=payload
            )
            if confirmed is not None:
                return confirmed
            for _ in range(5):
                if not feed_read_ok:
                    break
                time.sleep(5)
                feed_read_ok, confirmed = self._feed_confirmation(
                    request, http_status=response.status, response_payload=payload
                )
                if confirmed is not None:
                    return confirmed
            if feed_read_ok:
                raise PublishFailed(
                    "bridge returned HTTP 500 and the authenticated account feed "
                    "has no new matching note after 25s; platform likely rejected "
                    "the publish (no automatic retry)",
                    retryable=False,
                    status=response.status,
                )
            raise PublishUnknown(
                f"bridge returned HTTP {response.status}; publish state unknown "
                "(account feed could not be read)"
            )
        if response.status in {401, 403}:
            raise PublishFailed(
                "bridge rejected the request (login/token may have expired); check the local bridge",
                retryable=False,
                status=response.status,
            )
        if response.status >= 400:
            raise PublishFailed(
                f"bridge rejected the publish request with HTTP {response.status}",
                retryable=False,
                status=response.status,
            )
        if isinstance(payload, dict) and payload.get("success") is False:
            raise PublishFailed(f"bridge reported failure: {payload.get('message') or payload}", retryable=False)

        post_id, url = _extract_post_receipt(payload)
        if not post_id and not url:
            # A 2xx alone is not proof, but the bridge's documented account
            # feed can prove it read-only: only a *new* note id with the exact
            # draft title since the pre-POST snapshot is accepted.
            confirmed = self._confirm_new_note(
                request, http_status=response.status, response_payload=payload
            )
            if confirmed is not None:
                return confirmed
            # 这是最关键的一条：HTTP 200 不等于发布成功。
            # 没有平台回执，就只能算 unknown。
            raise PublishUnknown(
                "bridge returned 2xx without a post id or URL; refusing to mark published"
            )
        return PublishOutcome(
            state="published",
            detail="local bridge returned a post receipt",
            external_post_id=post_id,
            external_url=url,
            receipt={
                "http_status": response.status,
                "response": payload,
                "confirmed_at": to_iso(self.clock.now()),
            },
        )


class DisabledPublisher(Publisher):
    """A stand-in that explains why no publisher is available."""
    # 占位发布器：不做事，只说明“为什么没有可用的发布器”。
    # 它的存在让上层代码不必到处写 if publisher is None。

    name = "disabled"
    mode = "disabled"

    def __init__(self, reason: str):
        self.reason = reason

    def publish(self, request: PublishRequest) -> PublishOutcome:
        raise PublishFailed(self.reason, retryable=False)

    def health(self) -> dict[str, Any]:
        return {"ok": False, "mode": "disabled", "configured": False, "detail": self.reason}


def build_publisher(config: Config, clock: Clock, *, health_only: bool = False) -> Publisher:
    # 工厂函数。注意：live 模式下绝不允许 mock 发布器——
    # 那会让用户以为在做真实运营，实际只是写本地文件。
    # health_only=True 仅供 doctor 做只读桥接/身份检查；该实例的 publish()
    # 会拒绝执行，真实发布仍必须走默认的 armed 构造。
    provider = str(config.get("publisher.provider", "mock")).lower()
    if provider == "mock":
        if config.is_live:
            raise NotConfigured("mock publisher is not allowed in live mode; use the dry-run/demo commands")
        return MockPublisher(config, clock)
    if provider == "xiaohongshu":
        return XiaohongshuPublisher(config, clock, allow_unarmed=health_only)
    raise NotConfigured(f"unsupported publisher.provider: {provider}")


def _extract_logged_in(payload: Any) -> bool | None:
    # 不同版本的桥接可能用不同的字段名。这里逐个尝试常见写法，
    # 只接受真正的布尔值；找不到就返回 None（表示“不知道”），
    # 不把字符串 "true" 之类猜成已登录。
    if not isinstance(payload, dict):
        return None
    for key in ("logged_in", "loggedIn", "is_logged_in", "isLoggedIn", "login"):
        if key in payload and isinstance(payload[key], bool):
            return payload[key]
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("logged_in", "loggedIn", "is_logged_in", "isLoggedIn", "login"):
            if key in data and isinstance(data[key], bool):
                return data[key]
    return None


def _short_hash(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _extract_red_id(payload: Any) -> str | None:
    """Read only the documented ``data.data.userBasicInfo.redId`` path.

    The upstream API notes explicitly say this is the public account number,
    not an immutable internal user_id.  Unknown/guessed shapes fail closed.
    """
    node: Any = payload
    for key in ("data", "data", "userBasicInfo"):
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    if not isinstance(node, dict):
        return None
    value = node.get("redId")
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _extract_post_receipt(payload: Any) -> tuple[str | None, str | None]:
    if not isinstance(payload, dict):
        return None, None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    post_id = None
    url = None
    for key in ("post_id", "postId", "note_id", "noteId"):
        value = data.get(key) if isinstance(data, dict) else None
        if value not in (None, ""):
            post_id = str(value)
            break
    for key in ("url", "post_url", "postUrl", "note_url", "noteUrl", "share_url", "shareUrl"):
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            url = value
            break
    return post_id, url


def _feed_notes(payload: Any) -> list[dict[str, str]] | None:
    """Read note cards from the authenticated account feed.

    ``None`` means the response did not contain a feed list at all; an empty
    list means the feed was present but empty.  That distinction matters before
    the first post on a new account.
    """
    if not isinstance(payload, dict):
        return None
    node: Any = payload.get("data")
    if isinstance(node, dict):
        node = node.get("data", node)
    if not isinstance(node, dict):
        return None
    feeds = node.get("feeds")
    if not isinstance(feeds, list):
        return None
    notes: list[dict[str, str]] = []
    for item in feeds:
        if not isinstance(item, dict):
            continue
        card = item.get("noteCard") if isinstance(item.get("noteCard"), dict) else {}
        note_id = item.get("id") or card.get("noteId") or card.get("id")
        title = card.get("displayTitle") or card.get("title") or ""
        xsec = item.get("xsecToken") or item.get("xsec_token") or ""
        if not note_id:
            continue
        notes.append(
            {
                "id": str(note_id),
                "title": str(title),
                "xsec_token": str(xsec),
                "url": _note_url(str(note_id), str(xsec)),
            }
        )
    return notes


def _note_url(note_id: str, xsec_token: str) -> str:
    url = f"https://www.xiaohongshu.com/explore/{note_id}"
    if xsec_token:
        url += f"?xsec_token={xsec_token}"
    return url


def _title_key(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()

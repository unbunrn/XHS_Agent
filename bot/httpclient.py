"""Small, bounded HTTP client on top of ``urllib``.

Deliberate properties:
* no proxy use (avoids environment proxies bypassing the SSRF check),
* redirects are re-validated against the SSRF guard,
* responses are size capped,
* 429/5xx/timeouts retry with bounded exponential backoff,
* per-host rate limiting.
"""

# =============================================================================
# 中文导读：一个“自带安全带的”网页请求客户端
# =============================================================================
# 为什么不直接用 requests 之类的库？
#   这个项目只需要很小的 HTTP 能力，而且每条请求都必须经过 SSRF 检查、
#   限制响应大小、限速和重试。自己包一层 urllib，规则就集中在这一个文件里，
#   不会出现“某个地方绕过了检查”的漏洞。
#
# 五条设计规则（对应上面英文 docstring）：
#   1. 禁用系统代理：否则代理会替我们去访问目标，SSRF 检查就白做了；
#   2. 重定向后重新检查：第一次是公网地址，跳转后可能变成 127.0.0.1；
#   3. 响应有大小上限：防止一个超大文件把内存吃光；
#   4. 只对“值得重试”的错误重试：429/5xx/超时，且指数退避；
#   5. 同一主机限速：不要高频打扰别人的网站。
#
# 生活化类比：寄快递时，快递员每到一个中转站都要重新安检（重定向复检），
# 包裹超过尺寸就拒收（大小上限），同一栋楼一天只送几趟（限速），
# 遇到“暂时没人收”才过一会儿再来（重试）。
# =============================================================================

from __future__ import annotations

import email.utils
import json
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .security import SSRFBlocked, SSRFGuard


class HttpTransportError(RuntimeError):
    # 网络层重试全部失败后抛出的错误。
    # retryable 告诉上层“任务队列还要不要再排一次”。
    def __init__(self, message: str, *, url: str, retryable: bool = True):
        super().__init__(message)
        self.url = url
        self.retryable = retryable


class FetchTooLarge(RuntimeError):
    # 响应超过上限。重试只会再下载一次同样大的内容，所以直接失败。
    pass


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str

    def text(self) -> str:
        # 从 Content-Type 里找 charset，例如 "text/xml; charset=gbk"。
        # 找不到就按 UTF-8 解码；解码遇到坏字节用 replace 替换，不抛异常。
        content_type = self.headers.get("content-type", "")
        charset = "utf-8"
        for part in content_type.split(";")[1:]:
            key, _, value = part.strip().partition("=")
            if key.lower() == "charset" and value:
                charset = value.strip().strip('"')
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def json(self):
        # 只负责解析；解析失败会抛 JSONDecodeError，由调用方决定怎么处理。
        return json.loads(self.text())


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    # urllib 遇到 301/302/303/307/308 会自动跟到新地址。
    # 这里在“跟过去之前”拦截一次，把新 URL 再交给 SSRFGuard 检查。
    # 最多跟 5 跳，防止无限重定向。
    max_redirections = 5

    def __init__(self, guard: SSRFGuard, allow_private: bool):
        self.guard = guard
        self.allow_private = allow_private

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 注意：allow_private 默认 False。只有发布适配器明确允许本机服务时，
        # 跳转到本机地址才会被放行；新闻采集永远不走这条豁免。
        self.guard.validate(newurl, allow_private=self.allow_private)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpClient:
    def __init__(
        self,
        *,
        guard: SSRFGuard,
        user_agent: str = "BotOperator/0.1",
        timeout: float = 20.0,
        max_bytes: int = 2_097_152,
        min_host_interval: float = 2.0,
        max_retries: int = 2,
        backoff_base: float = 1.0,
        backoff_max: float = 20.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ):
        # 这些参数都有保守的默认值；config.network 会把用户配置传进来。
        # sleep / monotonic / rng 可注入，测试里就能“假装过了 2 秒”，
        # 不用真的等待。
        self.guard = guard
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.min_host_interval = max(0.0, min_host_interval)
        self.max_retries = max(0, max_retries)
        self.backoff_base = backoff_base
        self.backoff_max = min(backoff_max, 60.0)
        self._sleep = sleep
        self._monotonic = monotonic
        self._rng = rng or random.Random()
        self._last_host_request: dict[str, float] = {}
        # ProxyHandler({}) 传入空字典 = 明确禁用所有代理。
        # 这是安全要求，不是性能优化：代理会让 SSRFGuard 检查的地址
        # 与实际连接的地址不一致。
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _SafeRedirectHandler(guard, allow_private=False),
        )

    # -- public API -----------------------------------------------------
    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        max_bytes: int | None = None,
        retries: int | None = None,
        allow_private: bool = False,
        allowed_ports: tuple[int, ...] | None = None,
    ) -> HttpResponse:
        # GET 只是 request() 的快捷方式；所有安全检查都在 request() 里统一做。
        return self.request(
            "GET",
            url,
            headers=headers,
            timeout=timeout,
            max_bytes=max_bytes,
            retries=retries,
            allow_private=allow_private,
            allowed_ports=allowed_ports,
        )

    def post_json(
        self,
        url: str,
        payload: dict,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        max_bytes: int | None = None,
        retries: int | None = None,
        allow_private: bool = False,
    ) -> HttpResponse:
        # 把 Python 字典序列化成 UTF-8 JSON 字节。
        # ensure_ascii=False 让中文保持原样，而不是变成 \uXXXX。
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        merged = {"Content-Type": "application/json; charset=utf-8"}
        merged.update(headers or {})
        return self.request(
            "POST",
            url,
            headers=merged,
            body=body,
            timeout=timeout,
            max_bytes=max_bytes,
            retries=retries,
            allow_private=allow_private,
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float | None = None,
        max_bytes: int | None = None,
        retries: int | None = None,
        allow_private: bool = False,
        allowed_ports: tuple[int, ...] | None = None,
        retry_statuses: tuple[int, ...] = (429, 500, 502, 503, 504),
    ) -> HttpResponse:
        # 整个客户端的总调度：
        #   1. 请求发出前先做 SSRF 检查；
        #   2. 每次尝试前先限速等待；
        #   3. 网络错误/超时 -> 退避后重试；
        #   4. 429/5xx -> 也重试，优先遵守服务器给的 Retry-After；
        #   5. 其他状态码（包括 404）直接返回，交给调用方判断。
        # 注意：这里不重试 POST，除非调用方明确传 retries；发布适配器
        # 自己会处理“结果未知”，绝不在这里盲目重发。
        self.guard.validate(url, allow_private=allow_private, allowed_ports=allowed_ports)
        limit = max_bytes or self.max_bytes
        attempts = self.max_retries if retries is None else max(0, retries)
        last_error: Exception | None = None
        for attempt in range(attempts + 1):
            self._respect_rate_limit(url)
            try:
                response = self._once(method, url, headers or {}, body, timeout or self.timeout, limit, allow_private)
            except FetchTooLarge:
                raise
            except SSRFBlocked:
                raise
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                self._sleep(self._delay(attempt, None))
                continue
            if response.status in retry_statuses and attempt < attempts:
                self._sleep(self._delay(attempt, response.headers.get("retry-after")))
                continue
            return response
        raise HttpTransportError(f"{method} failed after {attempts + 1} attempt(s): {last_error}", url=url)

    # -- internals ------------------------------------------------------
    def _once(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
        limit: int,
        allow_private: bool,
    ) -> HttpResponse:
        request = urllib.request.Request(url, data=body, method=method)
        # 默认请求头：说明自己是谁（User-Agent），并声明能接受 RSS/Atom/XML。
        # 部分网站会根据 Accept 返回不同格式。
        request.add_header("User-Agent", self.user_agent)
        request.add_header("Accept", "application/rss+xml, application/atom+xml, application/xml, text/xml, */*")
        for key, value in headers.items():
            request.add_header(key, value)
        opener = self._opener
        if allow_private:
            # Rebuild so redirects into the configured local service are
            # validated with the same explicit allowance.
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                _SafeRedirectHandler(self.guard, allow_private=True),
            )
        try:
            with opener.open(request, timeout=timeout) as response:
                # 只多读 1 个字节：如果读到了 limit+1，就说明超过上限。
                # 这样无需先看 Content-Length（它可能缺失或撒谎）。
                raw = response.read(limit + 1)
                if len(raw) > limit:
                    raise FetchTooLarge(f"response exceeds {limit} bytes")
                return HttpResponse(
                    status=int(response.status),
                    headers={k.lower(): v for k, v in response.headers.items()},
                    body=raw,
                    url=response.geturl(),
                )
        except urllib.error.HTTPError as exc:
            # urllib 默认把 4xx/5xx 当异常抛出。但本项目希望把状态码和
            # 响应体交给调用方处理（例如 RSS 源返回 304 是正常情况），
            # 所以这里把它转成普通 HttpResponse 返回。
            raw = exc.read(limit + 1) if exc.fp else b""
            if len(raw) > limit:
                raise FetchTooLarge(f"error response exceeds {limit} bytes") from exc
            return HttpResponse(
                status=int(exc.code),
                headers={k.lower(): v for k, v in (exc.headers or {}).items()},
                body=raw,
                url=exc.geturl() or url,
            )

    def _respect_rate_limit(self, url: str) -> None:
        # 按“主机名”记录上次请求时间，两次之间至少间隔 min_host_interval 秒。
        # 用 monotonic 时钟而不是系统时间：用户改系统时间不会影响限速。
        # 首次访问某主机时不等待。
        if not self.min_host_interval:
            return
        try:
            host = urllib.parse.urlsplit(url).netloc.lower()
        except ValueError:
            return
        now = self._monotonic()
        last = self._last_host_request.get(host)
        if last is not None:
            wait = self.min_host_interval - (now - last)
            if wait > 0:
                self._sleep(min(wait, self.min_host_interval))
        self._last_host_request[host] = self._monotonic()

    def _delay(self, attempt: int, retry_after: str | None) -> float:
        # 如果服务器明确说了 Retry-After，就听它的（但仍限制最大等待）。
        # 否则用“指数退避 + 随机抖动”：
        #   第 0 次失败等约 1 秒，第 1 次约 2 秒，第 2 次约 4 秒……
        # 随机抖动避免多个客户端在同一时刻一起重试（惊群效应）。
        if retry_after:
            seconds = _parse_retry_after(retry_after)
            if seconds is not None:
                return min(max(seconds, 0.0), self.backoff_max)
        base = min(self.backoff_max, self.backoff_base * (2**attempt))
        return min(self.backoff_max, base + self._rng.uniform(0, max(0.05, base * 0.2)))


def _parse_retry_after(value: str) -> float | None:
    # HTTP 规范允许两种写法：
    #   1. 秒数，例如 "120"；
    #   2. HTTP 日期，例如 "Wed, 21 Oct 2026 07:28:00 GMT"。
    # 第二种要减去当前时间，得到还要等多少秒。
    text = (value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())

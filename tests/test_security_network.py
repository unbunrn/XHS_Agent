"""SSRF, redirect, and bounded HTTP retry tests."""

# =============================================================================
# 中文导读：网络层要防“你让它抓 A，它被带去 B”
# =============================================================================
# SSRF（服务端请求伪造）是指：攻击者在网页里放一个链接，
# 诱导服务器去访问内网、本机、云厂商元数据地址（169.254.169.254）。
# 所以：
#   - 私网/回环/元数据地址、带用户名密码的 URL、非 http(s) 协议一律拒绝；
#   - 域名要先解析，解析结果是内网 IP 也拒绝（防止 DNS 把公网域名指到内网）；
#   - 放行本机服务必须精确到 IP，不能整个 127.0.0.0/8 都放开；
#   - 重定向到新地址时要重新检查（第一跳合法不代表第二跳合法）；
#   - 重试有次数上限，响应体有大小上限，避免被慢接口/超大文件拖死。
# =============================================================================

from __future__ import annotations

import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bot.httpclient import FetchTooLarge, HttpClient, HttpTransportError
from bot.security import SSRFBlocked, SSRFGuard


class _Handler(BaseHTTPRequestHandler):
    # 一个本地“坏服务器”，故意制造各种网络情况：
    #   /retry        第一次 429（要求稍后重试），第二次 200
    #   /fail         永远 503
    #   /large        返回 2KB，用来撞响应大小上限
    #   /redirect-meta 302 跳转到云厂商元数据地址（SSRF 经典攻击）
    #   /redirect-ok  302 跳到同站 /ok（合法，应跟随）
    counts: dict[str, int] = {}

    def do_GET(self) -> None:  # noqa: N802
        self.counts[self.path] = self.counts.get(self.path, 0) + 1
        if self.path == "/retry":
            if self.counts[self.path] == 1:
                self.send_response(429)
                self.send_header("Retry-After", "0")
                self.end_headers()
                self.wfile.write(b"slow down")
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"recovered")
            return
        if self.path == "/fail":
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"unavailable")
            return
        if self.path == "/large":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"x" * 2048)
            return
        if self.path == "/redirect-meta":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
            return
        if self.path == "/redirect-ok":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"hello")

    def log_message(self, fmt: str, *args) -> None:
        pass


class SSRFGuardTests(unittest.TestCase):
    def test_blocks_private_loopback_metadata_and_non_http(self) -> None:
        # 这一组都是“绝不该访问”的目标：
        # 回环、localhost、云元数据、私有网段、IPv6 回环、
        # 带凭据的 URL、file/ftp 协议、内网专用域名后缀。
        guard = SSRFGuard()
        blocked = [
            "http://127.0.0.1/",
            "http://localhost:8765/",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.1/",
            "http://192.168.1.1/",
            "http://[::1]/",
            "http://user:pass@example.com/",
            "file:///etc/passwd",
            "ftp://example.com/x",
            "http://service.internal/",
            "http://printer.local/",
        ]
        for url in blocked:
            with self.subTest(url=url):
                with self.assertRaises(SSRFBlocked):
                    guard.validate(url)

    def test_public_resolution_allowed_and_private_resolution_blocked(self) -> None:
        # 用假的 DNS 解析器模拟两种情况：
        # 公网域名解析到公网 IP -> 放行，并返回解析结果；
        # 公网域名解析到 10.1.2.3（内网）-> 拒绝。
        # 这能防住“域名看起来正常、实际指向内网”的绕过。
        def public_resolver(host, port, type=None):  # noqa: A002
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

        def private_resolver(host, port, type=None):  # noqa: A002
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", port))]

        self.assertEqual(
            SSRFGuard(resolver=public_resolver).validate("https://news.example.com/x"),
            ["93.184.216.34"],
        )
        with self.assertRaises(SSRFBlocked):
            SSRFGuard(resolver=private_resolver).validate("https://news.example.com/x")

    def test_explicit_local_service_allowance_is_narrow(self) -> None:
        # 只放行写进配置的那一个地址 127.0.0.1；
        # 127.0.0.2 属于同一网段，但不在名单里，仍然拒绝。
        # 说明放行名单是“精确匹配”，不是“整段放开”。
        guard = SSRFGuard(["127.0.0.1"])
        guard.validate("http://127.0.0.1:18060/api/v1/login/status")
        with self.assertRaises(SSRFBlocked):
            guard.validate("http://127.0.0.2:18060/api/v1/login/status")


class HttpClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def _client(self, *, max_bytes: int = 10000, retries: int = 2) -> HttpClient:
        # 测试用客户端：默认不等待、不限速，但保留真实的 SSRFGuard。
        # sleep=lambda _: None 让重试立即发生，测试不会真的等几秒。
        return HttpClient(
            guard=SSRFGuard(),
            timeout=3,
            max_bytes=max_bytes,
            min_host_interval=0,
            max_retries=retries,
            sleep=lambda _: None,
        )

    def test_loopback_requires_explicit_allowance(self) -> None:
        # 默认 SSRFGuard 不放行 127.0.0.1；即使 allow_private=True 也必须
        # 只在显式允许时才生效——这里先验证没带 allow_private 会被拦。
        with self.assertRaises(SSRFBlocked):
            self._client().get(f"http://127.0.0.1:{self.port}/ok")

    def test_bounded_retry_on_429_then_success(self) -> None:
        # 第一次 429，客户端按 Retry-After 等待后重试，第二次成功。
        # 总共正好 2 次请求，证明“重试确实发生，但没有无限重试”。
        _Handler.counts.pop("/retry", None)
        response = self._client().get(
            f"http://127.0.0.1:{self.port}/retry", allow_private=True
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.text(), "recovered")
        self.assertEqual(_Handler.counts["/retry"], 2)

    def test_repeated_503_is_bounded_and_returned_as_last_response(self) -> None:
        # 一直 503：初始请求 + 2 次重试 = 共 3 次，然后放弃，
        # 把最后一次 503 响应原样返回给调用方判断。
        _Handler.counts.pop("/fail", None)
        response = self._client().get(
            f"http://127.0.0.1:{self.port}/fail", allow_private=True
        )
        self.assertEqual(response.status, 503)
        self.assertEqual(_Handler.counts["/fail"], 3)

    def test_response_size_cap(self) -> None:
        # 上限设成 100 字节，服务器却返回 2048 字节：
        # 必须在读超限时抛 FetchTooLarge，不能把整个响应读进内存。
        with self.assertRaises(FetchTooLarge):
            self._client(max_bytes=100).get(
                f"http://127.0.0.1:{self.port}/large", allow_private=True
            )

    def test_redirect_to_metadata_is_blocked(self) -> None:
        # 第一跳是允许访问的本机地址，但 302 指向 169.254.169.254。
        # 说明重定向后的地址会重新过 SSRFGuard，而不是盲目跟随。
        with self.assertRaises((SSRFBlocked, HttpTransportError)):
            self._client().get(
                f"http://127.0.0.1:{self.port}/redirect-meta", allow_private=True
            )

    def test_safe_same_host_redirect_is_followed(self) -> None:
        # 同站重定向是正常需求，应该跟随并拿到最终 200 内容。
        response = self._client().get(
            f"http://127.0.0.1:{self.port}/redirect-ok", allow_private=True
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.text(), "hello")


if __name__ == "__main__":
    unittest.main()

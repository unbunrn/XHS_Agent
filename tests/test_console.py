"""Local console: loopback-only auth, CSRF, read-only GET, and approval."""

# =============================================================================
# 中文导读：控制台的安全边界测试
# =============================================================================
# 每个测试都启动一个真实的 HTTP 服务（端口 0 = 系统随便分配），
# 然后用 http.client 发真实请求，验证：
#   - /healthz 公开但不泄露口令；
#   - 没登录访问页面/图片 -> 303 去登录；
#   - 错误口令 -> 401；
#   - 登录后能看审批页和 PNG；
#   - 没 CSRF 的 POST -> 403；
#   - 带 CSRF 的批准 -> 303，并且只改审批状态、不调用发布器；
#   - 重复提交同一个决定是无害的。
# 关键理念：控制台只负责“记录决定”，发布仍然由 worker/发布闸门完成。
# =============================================================================

from __future__ import annotations

import http.client
import json
import re
import threading
import unittest
from urllib.parse import urlencode

from bot.console import ConsoleApp
from tests.helpers import cleanup, make_draft, new_env


class ConsoleTests(unittest.TestCase):
    def setUp(self) -> None:
        # 每个测试都单独起一套环境和服务，测试之间不共享 session/数据库。
        self.env = new_env("console")
        self.draft = make_draft(self.env)
        self.env.config.data["console"]["port"] = 0
        self.app = ConsoleApp(
            self.env.config,
            clock=self.env.clock,
            db=self.env.db,
            login_token="console-test-token",
        )
        self.server = self.app.make_server()
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = ""

    def tearDown(self) -> None:
        # 先关服务器再删目录，避免后台线程还在读文件。
        self.server.shutdown()
        self.server.server_close()
        cleanup(self.env)

    def _request(
        self,
        method: str,
        path: str,
        body: str | None = None,
        *,
        cookie: str | None = None,
        content_type: str = "application/x-www-form-urlencoded",
    ) -> tuple[int, dict, bytes]:
        # 极简 HTTP 客户端：发一个请求，读完响应，返回
        # (状态码, 小写响应头字典, 响应体)。
        # 不自动处理 cookie/跳转，测试才能看到原始的 303/403。
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if cookie:
            headers["Cookie"] = cookie
        if body is not None:
            headers["Content-Type"] = content_type
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read()
        headers_out = {k.lower(): v for k, v in response.getheaders()}
        conn.close()
        return response.status, headers_out, data

    def _login(self) -> str:
        # 登录成功后从 Set-Cookie 里取出 "bot_session=..."，
        # 返回给后续请求当 Cookie 头用。
        status, headers, _ = self._request(
            "POST", "/login", urlencode({"token": "console-test-token"})
        )
        self.assertEqual(status, 303)
        cookie = headers["set-cookie"].split(";", 1)[0]
        self.assertTrue(cookie.startswith("bot_session="))
        return cookie

    def test_health_is_public_and_contains_no_secret(self) -> None:
        # /healthz 不需要登录（给启动脚本用），但绝不能把口令带出来。
        status, _, data = self._request("GET", "/healthz")
        self.assertEqual(status, 200)
        payload = json.loads(data)
        self.assertTrue(payload["ok"])
        self.assertNotIn("console-test-token", data.decode())

    def test_requires_login_and_rejects_bad_token(self) -> None:
        # 未登录 -> 303 /login；错误口令 -> 401。
        status, headers, _ = self._request("GET", "/")
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/login")
        bad, _, _ = self._request("POST", "/login", urlencode({"token": "nope"}))
        self.assertEqual(bad, 401)

    def test_approval_page_and_media_require_session(self) -> None:
        # 审批页和图片都属于敏感内容：没有 session 时都必须跳登录，
        # 登录后才返回 200，图片的 Content-Type 是 image/png 且以 PNG 签名开头。
        approval_id = self.draft["approval_id"]
        image_id = self.env.db.scalar(
            "SELECT id FROM draft_images WHERE draft_id=? AND ordinal=1",
            (self.draft["draft"]["id"],),
        )
        self.assertEqual(self._request("GET", f"/approval/{approval_id}")[0], 303)
        self.assertEqual(self._request("GET", f"/media/{image_id}")[0], 303)
        cookie = self._login()
        status, _, page = self._request("GET", f"/approval/{approval_id}", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn(self.draft["approval_code"].encode(), page)
        media_status, media_headers, media = self._request(
            "GET", f"/media/{image_id}", cookie=cookie
        )
        self.assertEqual(media_status, 200)
        self.assertEqual(media_headers["content-type"], "image/png")
        self.assertTrue(media.startswith(b"\x89PNG"))

    def test_csrf_is_required_and_approval_uses_the_same_gate(self) -> None:
        # 三步：
        #   1. 登录并从审批页取出 csrf token；
        #   2. 不带 csrf 的批准 -> 403；
        #   3. 带 csrf 的批准 -> 303，审批变 approved，但没有发布尝试。
        # 最后重复提交一次，确认不会产生副作用。
        cookie = self._login()
        approval_id = self.draft["approval_id"]
        status, _, page = self._request("GET", f"/approval/{approval_id}", cookie=cookie)
        self.assertEqual(status, 200)
        match = re.search(rb'name="csrf" value="([^"]+)"', page)
        self.assertIsNotNone(match)
        csrf = match.group(1).decode()
        no_csrf, _, _ = self._request(
            "POST",
            f"/approval/{approval_id}/decision",
            urlencode({"action": "approve"}),
            cookie=cookie,
        )
        self.assertEqual(no_csrf, 403)
        approved, _, _ = self._request(
            "POST",
            f"/approval/{approval_id}/decision",
            urlencode({"action": "approve", "csrf": csrf}),
            cookie=cookie,
        )
        self.assertEqual(approved, 303)
        row = self.env.db.fetchone("SELECT status FROM approvals WHERE id=?", (approval_id,))
        self.assertEqual(row["status"], "approved")
        # No publisher call happened: the console only records the decision.
        self.assertEqual(self.env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 0)
        # Replaying the decision is harmless.
        again, _, _ = self._request(
            "POST",
            f"/approval/{approval_id}/decision",
            urlencode({"action": "approve", "csrf": csrf}),
            cookie=cookie,
        )
        self.assertEqual(again, 303)
        self.assertEqual(self.env.db.scalar("SELECT COUNT(*) FROM publish_attempts"), 0)


if __name__ == "__main__":
    unittest.main()

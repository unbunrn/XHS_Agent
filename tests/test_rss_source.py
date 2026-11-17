"""RSS conditional fetch and parser tests against a local fixture server."""

# =============================================================================
# 中文导读：抓 RSS 时既要省流量，也要把脏数据洗干净
# =============================================================================
# 用本机起一个假的 RSS 服务器，验证：
#   - 第一次返回 200 + ETag，第二次带 If-None-Match 时服务器返回 304，
#     客户端应识别“没更新”，而不是把旧内容再处理一遍；
#   - HTML 实体（&amp;）要还原成正常字符；
#   - <script> 标签和里面的代码必须被清掉；
#   - 相对链接（/posts/relative）要补全成完整 URL；
#   - fixture 来源只给离线演示用，live 模式下必须拒绝；
#   - 不认识的来源类型要报 NotConfigured，而不是猜着处理。
# =============================================================================

from __future__ import annotations

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bot.adapters.sources import FixtureSource, RssSource, build_source
from bot.adapters.types import NotConfigured
from bot.httpclient import HttpClient
from bot.security import SSRFGuard
from tests.helpers import cleanup, new_env

FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Example AI News</title>
<item>
  <title>New &amp; faster model &lt;beta&gt;</title>
  <link>https://example.com/posts/new-model?utm_source=rss</link>
  <description>&lt;p&gt;A &lt;b&gt;faster&lt;/b&gt; model.&lt;/p&gt;&lt;script&gt;alert(1)&lt;/script&gt;</description>
  <pubDate>Wed, 05 Nov 2026 08:00:00 +0800</pubDate>
</item>
<item>
  <title>Relative link item</title>
  <link>/posts/relative</link>
  <description>Relative link body</description>
</item>
</channel></rss>
"""


class _FeedHandler(BaseHTTPRequestHandler):
    # 极简 RSS 服务器，只服务 /feed.xml：
    #   - 请求带 If-None-Match: "v1" -> 304（内容没变，别重复下载）
    #   - 否则 -> 200，并带上 ETag 和 Last-Modified 供下次条件请求使用
    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/feed.xml":
            self.send_response(404)
            self.end_headers()
            return
        if self.headers.get("If-None-Match") == '"v1"':
            self.send_response(304)
            self.send_header("ETag", '"v1"')
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
        self.send_header("ETag", '"v1"')
        self.send_header("Last-Modified", "Wed, 05 Nov 2026 00:00:00 GMT")
        self.end_headers()
        self.wfile.write(FEED)

    def log_message(self, fmt: str, *args) -> None:
        pass


class RssSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # 端口写 0，让系统分配一个空闲端口，避免和本机其他程序冲突。
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FeedHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_conditional_get_parsing_and_relative_links(self) -> None:
        env = new_env("rss")
        try:
            # 测试里关闭限速和重试，让测试又快又稳定；
            # 但仍然使用真实的 SSRFGuard，只放行精确的 127.0.0.1。
            http = HttpClient(
                guard=SSRFGuard(["127.0.0.1"]),
                timeout=3,
                max_bytes=100_000,
                min_host_interval=0,
                max_retries=0,
            )
            source = RssSource(
                {"name": "Local feed", "url": f"http://127.0.0.1:{self.port}/feed.xml"},
                env.config,
                http,
                env.clock,
            )
            first = source.fetch()
            self.assertEqual(first.status, 200)
            self.assertEqual(first.etag, '"v1"')
            self.assertEqual(len(first.entries), 2)
            # XML 实体被还原；<script> 及其内容被移除，而不是原样带进正文。
            self.assertEqual(first.entries[0].title, "New & faster model <beta>")
            self.assertNotIn("<script", first.entries[0].content)
            self.assertNotIn("alert(1)", first.entries[0].content)
            # 相对链接按 feed 地址补全成绝对 URL。
            self.assertTrue(first.entries[1].url.endswith("/posts/relative"))

            # 带上上次的 ETag 再请求：服务器回 304，本地应该认为“没更新”。
            second = source.fetch(etag=first.etag)
            self.assertTrue(second.not_modified)
            self.assertEqual(second.entries, [])

            # Fixture sources are for the offline demo only.
            # 中文：fixture 来源只服务于离线演示，切到 live 后构造/抓取都必须被拒。
            env.config.data["app"]["mode"] = "live"
            with self.assertRaises(NotConfigured):
                FixtureSource(
                    {"name": "Fixture", "url": "feed.json"},
                    env.config,
                    http,
                    env.clock,
                ).fetch()
            with self.assertRaises(NotConfigured):
                # 未知 kind 不能“猜一个解析器”，必须明确报错。
                build_source(
                    {"name": "x", "url": "x", "kind": "unknown"},
                    env.config,
                    http,
                    env.clock,
                )
        finally:
            cleanup(env)

    def test_fixture_source_refused_in_live_mode(self) -> None:
        # 单独再测一次：即使不经过 HttpClient，fixture 来源在 live 下也拒绝抓取。
        env = new_env("rss-fixture-live")
        try:
            env.config.data["app"]["mode"] = "live"
            source = FixtureSource(
                {"name": "Fixture", "url": "feed.json"}, env.config, None, env.clock
            )
            with self.assertRaises(NotConfigured):
                source.fetch()
        finally:
            cleanup(env)


if __name__ == "__main__":
    unittest.main()

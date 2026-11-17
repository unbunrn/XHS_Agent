"""Console smoke test: unauthenticated redirect, CSRF approval, image access."""

# =============================================================================
# 中文导读：控制台安全行为的冒烟测试
# =============================================================================
# 这个脚本把控制台真的启动在一个随机端口上，然后用 HTTP 客户端逐项验证：
#   1. 未登录访问首页 -> 303 跳转到 /login；
#   2. 错误口令 -> 401；
#   3. 正确口令 -> 303 并拿到 session cookie；
#   4. 登录后能打开审批页、能看到图片（PNG magic）；
#   5. 不带 CSRF 的批准请求 -> 403；
#   6. 带 CSRF 的批准 -> 303，worker tick 后只产生一条 simulated 尝试。
# 它验证的是“安全边界真的生效”，而不只是页面长得对。
# =============================================================================

from __future__ import annotations

import http.cookiejar
import os
import re
import shutil
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from bot.config import load_config
from bot.console import ConsoleApp
from bot.timeutil import FrozenClock
from bot.worker import Worker

root = Path.cwd()
tmp = root / "var" / "smoke-console"
# 一次性数据库目录，先清空再建，避免上次运行的审批干扰断言。
shutil.rmtree(tmp, ignore_errors=True)
tmp.mkdir(parents=True)
# 同样必须在 load_config 之前设置环境变量。
os.environ["BOT_DB_PATH"] = str(tmp / "bot.sqlite3")
os.environ["BOT_CONSOLE_TOKEN"] = "console-smoke-token"
config = load_config(require_file=True)
# 端口 0 让操作系统分配一个空闲端口，避免和本机已有服务冲突；
# 实际端口从 server.server_address 里读。
config.data["console"]["port"] = 0
clock = FrozenClock()
worker = Worker(config, clock=clock)
worker.startup()
summary = worker.tick()
print("tick", summary)
# tick 之后应该已经有一条待审批的审批和一张渲染好的图片。
approval = worker.db.fetchone("SELECT * FROM approvals WHERE status='pending' ORDER BY id DESC LIMIT 1")
assert approval is not None, "no pending approval"
image = worker.db.fetchone("SELECT * FROM draft_images WHERE draft_id=? ORDER BY ordinal", (approval["draft_id"],))
assert image is not None, "no image"

app = ConsoleApp(config, clock=clock, db=worker.db, login_token="console-smoke-token", llm=worker.llm)
server = app.make_server()
# 后台线程跑 HTTP 服务；daemon=True 保证脚本退出时不会卡住。
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
# server_address[1] 是实际绑定的端口（因为上面写的是 0）。
base = f"http://127.0.0.1:{server.server_address[1]}"
jar = http.cookiejar.CookieJar()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # 默认的 urllib 遇到 303 会自动跟随跳转，那样就看不到“是否跳转”这件事了。
    # 返回 None 表示不自动跟随，让测试能断言 303 和 Location。
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect())

try:
    try:
        # 第一条断言：没有 session 时不能看到仪表盘数据。
        opener.open(base + "/", timeout=5)
        raise SystemExit("unauthenticated dashboard unexpectedly allowed")
    except urllib.error.HTTPError as exc:
        print("unauth GET /", exc.code, exc.headers.get("Location"))
        assert exc.code == 303 and exc.headers.get("Location") == "/login"

    login = urllib.parse.urlencode({"token": "bad"}).encode()
    try:
        # 错误口令必须被拒绝，而且不能种下 session cookie。
        opener.open(urllib.request.Request(base + "/login", data=login, method="POST"), timeout=5)
        raise SystemExit("bad login unexpectedly allowed")
    except urllib.error.HTTPError as exc:
        print("bad login", exc.code)
        assert exc.code == 401

    login = urllib.parse.urlencode({"token": "console-smoke-token"}).encode()
    try:
        # 正确口令返回 303 到首页，并在 Set-Cookie 里种下 bot_session。
        opener.open(urllib.request.Request(base + "/login", data=login, method="POST"), timeout=5)
    except urllib.error.HTTPError as exc:
        assert exc.code == 303, exc
        print("login", exc.code, exc.headers.get("Set-Cookie")[:20])
    page = opener.open(base + f"/approval/{approval['id']}", timeout=5).read().decode("utf-8")
    print("approval page", len(page), "图片" in page, approval["code"] in page)
    # 从页面表单里取出 CSRF token，后面批准时要用。
    # 这也顺便证明：没有 token 的客户端拿不到它。
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    image_resp = opener.open(base + f"/media/{image['id']}", timeout=5)
    data = image_resp.read()
    print("image", image_resp.status, image_resp.headers.get("Content-Type"), len(data), data[:8])
    # PNG 文件的前 8 个字节是固定签名，用来确认返回的确实是图片而不是错误页。
    assert data[:8] == b"\x89PNG\r\n\x1a\n"

    # CSRF must be required.
    # 中文：只带 action、不带 csrf 的 POST 必须被 403 拒绝。
    try:
        opener.open(
            urllib.request.Request(
                base + f"/approval/{approval['id']}/decision",
                data=urllib.parse.urlencode({"action": "approve"}).encode(),
                method="POST",
            ),
            timeout=5,
        )
        raise SystemExit("missing CSRF unexpectedly allowed")
    except urllib.error.HTTPError as exc:
        print("no csrf", exc.code)
        assert exc.code == 403

    decision = urllib.parse.urlencode({"action": "approve", "csrf": csrf}).encode()
    try:
        # 带正确 CSRF 的批准应返回 303（POST 后跳回审批页）。
        opener.open(
            urllib.request.Request(base + f"/approval/{approval['id']}/decision", data=decision, method="POST"),
            timeout=5,
        )
    except urllib.error.HTTPError as exc:
        assert exc.code == 303, exc
    print("approved via console")
    worker.tick()
    # 最终断言：只有一条发布尝试，且状态是 simulated。
    # 如果出现两条，说明去重失效；如果状态是 published，说明 mock 冒充了真实发布。
    attempts = worker.db.fetchall("SELECT state,mode FROM publish_attempts ORDER BY id")
    print("attempts", [dict(r) for r in attempts])
    assert len(attempts) == 1 and attempts[0]["state"] == "simulated"
finally:
    # 无论断言是否失败，都要关掉服务器和后台线程，避免脚本挂住。
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)

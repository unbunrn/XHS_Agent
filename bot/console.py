"""Loopback-only approval console.

Security properties:
* bound to loopback by default and rejects non-loopback Host headers;
* every page except ``/healthz`` and ``/login`` needs a session cookie;
* every state-changing request needs the per-session CSRF token;
* GET never changes state;
* approvals still flow through the same binding/owner checks as Feishu;
* images are served only after authentication, from rows in the database.
"""

# =============================================================================
# 中文导读：只在本机运行的审批控制台
# =============================================================================
# 这是一个用 Python 标准库 http.server 写的小网页，默认只监听 127.0.0.1。
# 它的作用是让人在浏览器里看审批、看图、点批准/拒绝/修改。
#
# 安全规则（每一条都对应代码里的一处检查）：
#   1. 只绑定回环地址，并检查 Host 头，防止 DNS rebinding 把本机服务
#      暴露给恶意网页；
#   2. 除 /healthz 和 /login 外，所有页面都要登录后的 session cookie；
#   3. 所有会改状态的操作（批准、拒绝、修改、对账、退出）都是 POST，
#      并且必须带 CSRF token；
#   4. GET 永远只读，不会因为“点了个链接”就批准或发布；
#   5. 批准仍然走 handle_inbound_message，和飞书消息完全同一条路径，
#      不会因为换了界面就跳过 owner/绑定/有效期检查；
#   6. 图片只从数据库记录的行里取，登录后才提供，不能拿 URL 读任意文件。
#
# 为什么 session 存在内存里？
#   控制台重启后所有人需要重新登录，这是可以接受的；
#   换来的是不用把会话密钥写进磁盘，也不会污染业务数据库。
# =============================================================================

from __future__ import annotations

import html
import json
import secrets
import threading
import time
import uuid
from datetime import datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from .config import Config, load_config
from .db import Database, json_loads
from .security import constant_time_equals, sha256_file
from .services.approvals import (
    configured_owner,
    get_approval,
    handle_inbound_message,
    verify_approval_artifact,
)
from .services.publish import reconcile_unknown
from .timeutil import SystemClock, human_delta, local_iso
from .config import ConfigError

# 请求体上限 64 KiB。审批表单最多几百字节，
# 设上限是为了防止有人用超大 POST 把内存吃光。
MAX_BODY = 64 * 1024


class ConsoleError(RuntimeError):
    # 控制台自身的配置/启动错误，比如被要求绑定到非回环地址。
    pass


class SessionStore:
    # 进程内的会话表：session token -> {csrf, expires}。
    # 用线程锁保护，因为 ThreadingHTTPServer 会并发处理请求。
    def __init__(self, ttl_seconds: int = 7200):
        self.ttl = ttl_seconds
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}

    def create(self) -> tuple[str, str]:
        # 生成两个随机值：
        #   token 放进 HttpOnly cookie，证明“你登录过”；
        #   csrf 放进页面表单，证明“这个请求来自我们的页面”。
        # 两者分开，是为了防 CSRF：别的网站能带上 cookie，
        # 但读不到页面里的 csrf 值。
        token = secrets.token_urlsafe(24)
        csrf = secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            self._sessions = {
                key: value
                for key, value in self._sessions.items()
                if value["expires"] > now
            }
            self._sessions[token] = {"csrf": csrf, "expires": now + self.ttl}
        return token, csrf

    def get(self, token: str | None) -> dict[str, Any] | None:
        # 取会话时顺便检查过期。过期的立刻删掉并当作未登录。
        if not token:
            return None
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            if session["expires"] <= time.time():
                self._sessions.pop(token, None)
                return None
            return session

    def drop(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._sessions.pop(token, None)


def _esc(value: Any) -> str:
    # 所有要拼进 HTML 的动态值都必须过这里。
    # html.escape 会把 < > & " ' 转成实体，
    # 这样即使标题或修改意见里藏了 <script>，也只会当文字显示。
    return html.escape("" if value is None else str(value), quote=True)


def _fmt_time(value: str | None, timezone_name: str) -> str:
    # 把数据库里的 UTC ISO 时间转成配置时区的可读时间。
    # 转换失败时退回原始字符串：显示得难看总比页面报错好。
    if not value:
        return "—"
    try:
        return local_iso(str(value), timezone_name)
    except Exception:  # noqa: BLE001 - display fallback only
        return str(value)


class ConsoleHandler(BaseHTTPRequestHandler):
    # 继承标准库的请求处理器，重写 do_GET / do_POST 两个入口。
    # 每个请求都会新建一个 handler 实例，但 server.app 指向同一个 ConsoleApp。
    server_version = "BotConsole/0.1"
    sys_version = ""

    # -- plumbing -------------------------------------------------------
    @property
    def app(self) -> "ConsoleApp":
        # 从 server 上取共享的应用对象（数据库、配置、会话表都在里面）。
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # 覆盖标准库默认的 stderr 打印，改走项目统一的 logger，
        # 这样访问日志会进日志文件，而不是散落在终端。
        self.app.logger.info("console %s - %s", self.address_string(), fmt % args)

    def _cookie_token(self) -> str | None:
        # 从 Cookie 头里取 bot_session。解析失败一律当“没登录”，
        # 不把异常抛给服务器。
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:  # noqa: BLE001
            return None
        morsel = cookie.get("bot_session")
        return morsel.value if morsel else None

    def _session(self) -> dict[str, Any] | None:
        # cookie -> session 的便捷封装，无效/过期都返回 None。
        return self.app.sessions.get(self._cookie_token())

    def _host_ok(self) -> bool:
        # 检查 Host 头是不是本机。
        # 为什么需要它？恶意网站可以把域名解析到 127.0.0.1（DNS rebinding），
        # 让浏览器以为在访问自己的站点，实际请求打到了本控制台。
        # 只允许 localhost/127.0.0.1/::1，可以挡住这类请求。
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return False
        name = host.rsplit(":", 1)[0] if ":" in host and not host.endswith("]") else host
        return name in {"127.0.0.1", "localhost", "::1", "[::1]"}

    def _send(
        self,
        status: int,
        body: str | bytes,
        *,
        content_type: str = "text/html; charset=utf-8",
        headers: dict[str, str] | None = None,
    ) -> None:
        # 统一的响应出口。除了发正文，还固定加一组安全响应头：
        #   CSP             只允许本站样式/图片，禁止脚本和外部资源；
        #   X-Frame-Options 禁止被别的页面用 iframe 嵌套（防点击劫持）；
        #   nosniff         不让浏览器猜 Content-Type；
        #   no-referrer     跳转时不泄露本页地址；
        #   no-store        审批页面不被浏览器缓存。
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; form-action 'self'; base-uri 'none'",
        )
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _redirect(self, location: str, *, headers: dict[str, str] | None = None) -> None:
        # 用 303 See Other：浏览器会把 POST 之后的跳转改成 GET，
        # 避免用户刷新页面时重复提交批准/拒绝。
        data = b""
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _page(self, title: str, body: str, *, session: dict[str, Any] | None = None) -> str:
        # 页面骨架：演示横幅 + 导航 + 标题 + 正文。
        # 所有动态内容都用 _esc 转义；style 是固定字符串，不含用户输入。
        nav = ""
        if session is not None:
            nav = (
                '<div class="nav"><a href="/">仪表盘</a> '
                '<form method="post" action="/logout" class="inline">'
                f'<input type="hidden" name="csrf" value="{_esc(session["csrf"])}">'
                '<button class="link" type="submit">退出</button></form></div>'
            )
        banner = ""
        if self.app.config.demo_pipeline or self.app.config.mode != "live":
            banner = (
                '<div class="banner">本地离线演示：来源/模型/发布均为模拟，'
                "不会连接小红书，也不会真实发帖。</div>"
            )
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>
<style>
body{{font-family:system-ui,"Microsoft YaHei",sans-serif;max-width:960px;margin:24px auto;padding:0 18px;color:#1f2328;background:#fbfaf7;line-height:1.55}}
h1,h2{{line-height:1.25}} .card{{background:#fff;border:1px solid #e5e0d8;border-radius:10px;padding:16px 18px;margin:14px 0}}
.banner{{background:#fff4d6;border:1px solid #e7c873;padding:10px 14px;border-radius:8px;margin:12px 0}}
.muted{{color:#656d76;font-size:13px}} .mono{{font-family:ui-monospace,Consolas,monospace;font-size:12px;word-break:break-all}}
table{{border-collapse:collapse;width:100%;font-size:14px}} th,td{{text-align:left;border-bottom:1px solid #eee;padding:6px 8px;vertical-align:top}}
pre.body{{white-space:pre-wrap;background:#faf8f3;border:1px solid #eee;padding:14px;border-radius:8px;font-family:inherit}}
img.preview{{max-width:100%;border:1px solid #ddd;border-radius:8px;margin:8px 0}}
button{{background:#1f6feb;color:#fff;border:0;border-radius:7px;padding:8px 14px;cursor:pointer}}
button.reject{{background:#b42318}} button.link{{background:none;color:#1f6feb;padding:0;text-decoration:underline}}
input,select,textarea{{font:inherit;padding:7px;border:1px solid #ccc;border-radius:6px;max-width:100%}}
.nav{{display:flex;gap:16px;align-items:center}} .inline{{display:inline}}
.ok{{color:#1a7f37}} .warn{{color:#9a6700}} .bad{{color:#b42318}}
</style></head><body>{banner}{nav}<h1>{_esc(title)}</h1>{body}
<p class="muted">本地控制台 · {_esc(self.app.config.mode)} · {_esc(datetime.now().astimezone().isoformat(timespec="seconds"))}</p>
</body></html>"""

    def _login_page(self, error: str = "") -> str:
        # 登录页说明口令来源：环境变量或 secrets.toml。
        # 页面上绝不回显口令本身，只显示“输错了”这类提示。
        msg = f'<p class="bad">{_esc(error)}</p>' if error else ""
        body = f"""<div class="card">{msg}
<p>输入本地管理员口令。口令来自 <span class="mono">BOT_CONSOLE_TOKEN</span> 环境变量或
<span class="mono">config/secrets.toml</span> 的 <span class="mono">console_token</span>。
首次启动若未配置，会在启动终端打印一次性口令。</p>
<form method="post" action="/login">
<input type="password" name="token" autocomplete="current-password" autofocus size="36">
<button type="submit">进入</button></form></div>"""
        return self._page("本地审批控制台", body)

    # -- GET ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        # GET 只读：这里没有任何“批准/拒绝/发布”的副作用。
        # 先查 Host，再区分公开页面（/healthz、/login）和需要登录的页面。
        if not self._host_ok():
            self._send(HTTPStatus.FORBIDDEN, "Host not allowed")
            return
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._health()
            return
        if path == "/login":
            if self._session() is not None:
                self._redirect("/")
            else:
                self._send(HTTPStatus.OK, self._login_page())
            return
        session = self._session()
        if session is None:
            self._redirect("/login")
            return
        if path == "/":
            self._dashboard(session)
            return
        if path.startswith("/approval/"):
            self._approval_page(path, session)
            return
        if path.startswith("/media/"):
            self._media(path)
            return
        self._send(HTTPStatus.NOT_FOUND, self._page("未找到", "<p>页面不存在。</p>", session=session))

    def _health(self) -> None:
        # 健康检查故意不需要登录，方便本机的启动脚本/监控调用。
        # 它只返回计数和模式，不包含正文、密钥或任何可操作入口；
        # 而且整个服务只监听回环地址，外部本来就访问不到。
        ok = True
        try:
            self.app.db.scalar("SELECT 1")
        except Exception:  # noqa: BLE001
            ok = False
        payload = {
            "ok": ok,
            "mode": self.app.config.mode,
            "demo": self.app.config.demo_pipeline,
            "publisher": self.app.config.get("publisher.provider"),
            "pending_approvals": self.app.count("SELECT COUNT(*) FROM approvals WHERE status='pending'"),
            "publish_unknown": self.app.count("SELECT COUNT(*) FROM publish_attempts WHERE state='unknown'"),
            "real_publish_armed": self.app.config.is_live_publish_armed(),
        }
        self._send(
            HTTPStatus.OK if ok else HTTPStatus.SERVICE_UNAVAILABLE,
            json.dumps(payload, ensure_ascii=False),
            content_type="application/json; charset=utf-8",
        )

    def _dashboard(self, session: dict[str, Any]) -> None:
        # 仪表盘只做查询和展示：最近 20 条审批、最近 12 次发布尝试、
        # 后台任务计数，以及待审批/发布未知两个关键数字。
        # 所有值拼进 HTML 前都经过 _esc。
        db = self.app.db
        approvals = db.fetchall(
            "SELECT a.*, d.title FROM approvals a JOIN drafts d ON d.id=a.draft_id "
            "ORDER BY a.id DESC LIMIT 20"
        )
        attempts = db.fetchall(
            "SELECT * FROM publish_attempts ORDER BY id DESC LIMIT 12"
        )
        jobs = db.fetchall(
            "SELECT kind,state,COUNT(*) n FROM jobs GROUP BY kind,state ORDER BY kind,state"
        )
        cfg = self.app.config
        approval_rows = "".join(
            f"<tr><td><a href=\"/approval/{int(a['id'])}\">{_esc(a['code'])}</a></td>"
            f"<td>{_esc(a['title'])}</td><td>{_esc(a['status'])}</td>"
            f"<td>{_esc(_fmt_time(a['scheduled_at'], cfg.timezone_name))}</td>"
            f"<td>{_esc(_fmt_time(a['expires_at'], cfg.timezone_name))}</td></tr>"
            for a in approvals
        ) or '<tr><td colspan="5" class="muted">暂无审批。</td></tr>'
        attempt_rows = "".join(
            f"<tr><td>#{int(a['id'])}</td><td>{_esc(a['mode'])}</td><td>{_esc(a['state'])}</td>"
            f"<td class=\"mono\">{_esc(a['external_post_id'] or a['external_url'] or '')}</td>"
            f"<td>{_esc(_fmt_time(a['created_at'], cfg.timezone_name))}</td>"
            f"<td>{_esc(a['error'] or a['reconcile_note'] or '')}</td></tr>"
            for a in attempts
        ) or '<tr><td colspan="6" class="muted">尚无发布尝试。</td></tr>'
        job_rows = "".join(
            f"<tr><td>{_esc(j['kind'])}</td><td>{_esc(j['state'])}</td><td>{int(j['n'])}</td></tr>"
            for j in jobs
        ) or '<tr><td colspan="3" class="muted">无任务。</td></tr>'
        pending = self.app.count("SELECT COUNT(*) FROM approvals WHERE status='pending'")
        unknown = self.app.count("SELECT COUNT(*) FROM publish_attempts WHERE state='unknown'")
        body = f"""<div class="card">
<p>模式：<b>{_esc(cfg.mode)}</b>　发布适配器：<b>{_esc(cfg.get('publisher.provider'))}</b>　
真实发布开关：<b>{'已武装' if cfg.is_live_publish_armed() else '未武装'}</b></p>
<p>待审批：<b>{pending}</b>　发布结果未知：<b class="{ 'bad' if unknown else 'ok'}">{unknown}</b></p>
<p class="muted">数据库：{_esc(cfg.db_path)}</p></div>
<h2>审批</h2><div class="card"><table>
<tr><th>审批码</th><th>标题</th><th>状态</th><th>计划发布</th><th>过期</th></tr>{approval_rows}</table></div>
<h2>发布尝试</h2><div class="card"><table>
<tr><th>#</th><th>模式</th><th>状态</th><th>回执</th><th>时间</th><th>说明</th></tr>{attempt_rows}</table></div>
<h2>后台任务</h2><div class="card"><table><tr><th>类型</th><th>状态</th><th>数量</th></tr>{job_rows}</table></div>"""
        self._send(HTTPStatus.OK, self._page("AI 工具观察 · 审批控制台", body, session=session))

    def _approval_page(self, path: str, session: dict[str, Any]) -> None:
        # 审批详情页：正文、图片、哈希完整性、发布尝试，以及可执行的操作。
        # 先解析 URL 里的审批 ID；非法 ID 直接 404，不抛异常。
        try:
            approval_id = int(path.split("/")[2])
        except (IndexError, ValueError):
            self._send(HTTPStatus.NOT_FOUND, self._page("未找到", "<p>审批不存在。</p>", session=session))
            return
        approval = get_approval(self.app.db, approval_id=approval_id)
        if approval is None:
            self._send(HTTPStatus.NOT_FOUND, self._page("未找到", "<p>审批不存在。</p>", session=session))
            return
        draft = self.app.db.fetchone("SELECT * FROM drafts WHERE id=?", (approval["draft_id"],))
        if draft is None:
            self._send(HTTPStatus.NOT_FOUND, self._page("未找到", "<p>草稿不存在。</p>", session=session))
            return
        images = self.app.db.fetchall(
            "SELECT * FROM draft_images WHERE draft_id=? ORDER BY ordinal", (draft["id"],)
        )
        ok, reason, _ = verify_approval_artifact(self.app.db, approval)
        # 展示页也重新核对一遍正文/图片哈希。
        # 这样如果文件被替换，页面会直接显示“完整性不通过”，
        # 而不是让用户在不知情的情况下批准一个已经变样的作品。
        attempt = self.app.db.fetchone(
            "SELECT * FROM publish_attempts WHERE approval_id=? ORDER BY id DESC LIMIT 1",
            (approval["id"],),
        )
        # The operator can see whether the Feishu prompt (text + every image)
        # was fully delivered.  A partial delivery keeps the approval pending
        # and blocks phone approval until a retry succeeds.
        notice = self.app.db.fetchone(
            "SELECT state, attempts, error, sent_at FROM notifications WHERE dedupe_key=?",
            (f"approval:{approval['id']}",),
        )
        img_html = "".join(
            f'<figure><img class="preview" src="/media/{int(i["id"])}" alt="第 {int(i["ordinal"])} 页">'
            f'<figcaption class="muted">第 {int(i["ordinal"])} 页 · {int(i["width"])}×{int(i["height"])} · '
            f'<span class="mono">{_esc(i["sha256"])}</span></figcaption></figure>'
            for i in images
        )
        form = ""
        # 只有 pending 的审批才显示“批准/拒绝/修改”按钮。
        # 已批准、已发布、已过期的审批都不能在这里再做决定。
        if approval["status"] == "pending":
            form = f"""<div class="card"><h2>决定</h2>
<form method="post" action="/approval/{approval_id}/decision">
<input type="hidden" name="csrf" value="{_esc(session["csrf"])}">
<p><button name="action" value="approve" type="submit">批准并进入发布门</button>
<button name="action" value="reject" type="submit" class="reject">拒绝</button></p>
<p><label>修改意见（仅“修改”使用）<br>
<textarea name="instruction" rows="3" cols="70"></textarea></label></p>
<p><button name="action" value="revise" type="submit">提交修改，生成新版本</button></p>
</form><p class="muted">批准绑定正文与图片的内容哈希、账号、平台、计划发布时间和过期时间。修改任一内容都会使旧审批失效；未批准或超时不会发布。</p></div>"""
        elif approval["status"] == "publish_unknown" and attempt is not None:
            # 发布结果未知时，页面提供“人工对账”表单：
            # 确认已发 / 确认没发 / 仍然未知。绝不提供“重试发布”按钮。
            form = self._reconcile_form(int(attempt["id"]), session)
        status_class = "bad" if not ok and approval["status"] == "pending" else ""
        attempt_html = ""
        if attempt is not None:
            attempt_html = f"""<div class="card"><h2>发布尝试</h2>
<p>状态：<b>{_esc(attempt['state'])}</b>　模式：{_esc(attempt['mode'])}</p>
<p>外部回执：<span class="mono">{_esc(attempt['external_post_id'] or attempt['external_url'] or '无（模拟/未返回）')}</span></p>
<p>说明：{_esc(attempt['error'] or attempt['reconcile_note'] or '—')}</p>
{f'<div class="card">{self._reconcile_form(int(attempt["id"]), session)}</div>' if attempt['state'] == 'unknown' else ''}</div>"""
        notice_html = ""
        if notice is not None:
            notice_class = "ok" if notice["state"] == "sent" else "warn"
            notice_html = (
                f'<div class="card"><h2>审批通知投递</h2>'
                f'<p>状态：<b class="{notice_class}">{_esc(notice["state"])}</b>　'
                f'尝试：{int(notice["attempts"])}　'
                f'完整送达：{_esc(_fmt_time(notice["sent_at"], self.app.config.timezone_name))}</p>'
                f'<p>说明：{_esc(notice["error"] or "无")}</p>'
                f'<p class="muted">飞书审批只有在正文和全部图片都确认送达后才生效；'
                f'未完整送达会保留重试，不会重复已接受的本地决定。</p></div>'
            )
        body = f"""<div class="card">
<p>审批码：<b>{_esc(approval['code'])}</b>　状态：<b>{_esc(approval['status'])}</b></p>
<p>标题：{_esc(draft['title'])}</p>
<p>平台/账号：{_esc(approval['platform'])} / {_esc(approval['account_id'])}　草稿：#{int(draft['id'])} v{int(draft['version'])}</p>
<p>计划发布：{_esc(_fmt_time(approval['scheduled_at'], self.app.config.timezone_name))}　
过期：{_esc(_fmt_time(approval['expires_at'], self.app.config.timezone_name))}</p>
<p>正文 SHA256：<span class="mono">{_esc(approval['body_sha256'])}</span></p>
<p>图片 SHA256：<span class="mono">{_esc(approval['images_sha256'])}</span></p>
<p class="{status_class}">完整性：{_esc(reason if not ok else '通过：正文、图片、账号、版本与审批绑定一致')}</p>
</div>
{attempt_html}
{notice_html}
<h2>正文（将发布的内容）</h2><pre class="body">{_esc(draft['body'])}</pre>
<h2>图片（最终版本）</h2>{img_html or '<p class="muted">无图片。</p>'}
{form}"""
        self._send(HTTPStatus.OK, self._page(f"审批 {approval['code']}", body, session=session))

    def _reconcile_form(self, attempt_id: int, session: dict[str, Any]) -> str:
        # 对账表单。注意三个选项的文字都写明了后果，
        # 尤其是“确认未发布也不会自动重发”，避免用户误点。
        return f"""<form method="post" action="/reconcile/{attempt_id}">
<input type="hidden" name="csrf" value="{_esc(session['csrf'])}">
<p><label>处理结果
<select name="resolution">
<option value="still_unknown">仍然未知（保持 publish_unknown）</option>
<option value="confirmed_not_published">已确认没有发布（允许重新生成草稿/审批）</option>
<option value="confirmed_published">已确认发布成功（必须填写平台回执）</option>
</select></label></p>
<p><label>平台 post id：<input name="external_post_id" size="30"></label>
<label>URL：<input name="external_url" size="50"></label></p>
<p><label>备注：<input name="note" size="60"></label></p>
<p><button type="submit">提交核对结果</button></p>
<p class="muted">只有平台真实回执才可标记为已发布；确认未发布也不会自动重发，需要重新生成内容与审批。</p>
</form>"""

    def _media(self, path: str) -> None:
        # 图片接口。调用前 do_GET 已经确认过登录 session。
        # 只按数据库里的 draft_images.id 查记录，再用记录里的 path 读文件；
        # 用户不能通过 URL 指定任意路径，所以不存在路径穿越。
        try:
            image_id = int(path.split("/")[2].split(".")[0])
        except (IndexError, ValueError):
            self._send(HTTPStatus.NOT_FOUND, "not found")
            return
        row = self.app.db.fetchone(
            "SELECT i.* FROM draft_images i JOIN drafts d ON d.id=i.draft_id WHERE i.id=?", (image_id,)
        )
        if row is None:
            self._send(HTTPStatus.NOT_FOUND, "not found")
            return
        file_path = Path(str(row["path"]))
        try:
            data = file_path.read_bytes()
        except OSError:
            self._send(HTTPStatus.NOT_FOUND, "image missing", content_type="text/plain; charset=utf-8")
            return
        self._send(HTTPStatus.OK, data, content_type="image/png")

    # -- POST -----------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        # 所有会改变状态的操作都从这里进来。
        # 顺序很重要：Host 检查 -> 请求体大小 -> 解析表单 ->
        # 登录例外 -> session -> CSRF -> 具体动作。
        if not self._host_ok():
            self._send(HTTPStatus.FORBIDDEN, "Host not allowed")
            return
        path = urlsplit(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        # 负数或超过 64 KiB 的请求体直接拒绝，避免内存被撑爆。
        if length < 0 or length > MAX_BODY:
            self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
            return
        raw = self.rfile.read(length).decode("utf-8", "replace")
        form = {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}
        if path == "/login":
            # 登录是唯一不需要 session/CSRF 的 POST：
            # 用户正是通过它来获得 session。
            self._login(form)
            return
        session = self._session()
        if session is None:
            self._redirect("/login")
            return
        if not constant_time_equals(str(form.get("csrf", "")), str(session["csrf"])):
            # 用常量时间比较，避免攻击者通过响应时间逐位猜 token。
            # 比较失败说明请求不是从我们的页面发出的，直接 403。
            self._send(HTTPStatus.FORBIDDEN, self._page("拒绝", "<p>CSRF 校验失败，请重新登录。</p>", session=session))
            return
        if path == "/logout":
            self.app.sessions.drop(self._cookie_token())
            self._redirect("/login", headers={"Set-Cookie": "bot_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"})
            return
        if path.startswith("/approval/") and path.endswith("/decision"):
            self._decision(path, form, session)
            return
        if path.startswith("/reconcile/"):
            self._reconcile(path, form, session)
            return
        self._send(HTTPStatus.NOT_FOUND, self._page("未找到", "<p>页面不存在。</p>", session=session))

    def _login(self, form: dict[str, str]) -> None:
        # 口令用常量时间比较，防止时序侧信道。
        # 输错后故意 sleep 0.4 秒：拖慢暴力猜口令的速度。
        token = str(form.get("token", ""))
        if not constant_time_equals(token, self.app.login_token):
            self.app.logger.warning("failed console login from %s", self.address_string())
            time.sleep(0.4)
            self._send(HTTPStatus.UNAUTHORIZED, self._login_page("口令不正确。"))
            return
        session_token, _csrf = self.app.sessions.create()
        # cookie 加三个关键属性：
        #   HttpOnly   JavaScript 读不到，降低 XSS 后会话被盗的风险；
        #   SameSite=Strict  别的站点发起的请求不会带上它；
        #   Path=/    整个控制台可用。
        self._redirect(
            "/",
            headers={
                "Set-Cookie": f"bot_session={session_token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={self.app.sessions.ttl}",
            },
        )

    def _decision(self, path: str, form: dict[str, str], session: dict[str, Any]) -> None:
        # 控制台里的批准/拒绝/修改。
        # 注意它没有直接改审批表，而是把动作拼成一条“像飞书消息一样”的文本，
        # 交给 handle_inbound_message。这样两条通道的校验完全一致。
        try:
            approval_id = int(path.split("/")[2])
        except (IndexError, ValueError):
            self._send(HTTPStatus.BAD_REQUEST, "bad approval id")
            return
        action = str(form.get("action", ""))
        if action not in {"approve", "reject", "revise"}:
            self._send(HTTPStatus.BAD_REQUEST, "bad action")
            return
        approval = get_approval(self.app.db, approval_id=approval_id)
        if approval is None:
            self._send(HTTPStatus.NOT_FOUND, "approval not found")
            return
        word = {"approve": "批准", "reject": "拒绝", "revise": "修改"}[action]
        text = f"{word} {approval['code']}"
        instruction = str(form.get("instruction", "")).strip()
        if action == "revise" and not instruction:
            self._send(
                HTTPStatus.BAD_REQUEST,
                self._page("修改意见为空", f'<p><a href="/approval/{approval_id}">返回审批</a></p>', session=session),
            )
            return
        if instruction:
            text += " " + instruction[:500]
        owner = configured_owner(self.app.config)
        # 和 CLI 一样：没配 owner 就不允许做决定。
        # 控制台登录成功不代表可以绕过 owner 绑定。
        if not owner:
            self._send(
                HTTPStatus.CONFLICT,
                self._page("未配置决策人", "<p>请先配置 notify.lark.owner_open_id 或 LARK_OWNER_OPEN_ID。</p>", session=session),
            )
            return
        try:
            result = handle_inbound_message(
                self.app.db,
                self.app.config,
                self.app.clock,
                # 每次点击生成唯一 message_id，作为幂等键；
                # 网络重试/双击提交也不会执行两次决定。
                message_id=f"console:{uuid.uuid4().hex}",
                sender_id=owner,
                chat_id=owner,
                chat_type="p2p",
                text=text,
                channel="console",
                llm=self.app.llm,
            )
        except Exception as exc:  # noqa: BLE001 - display a safe error
            self.app.logger.exception("console decision failed")
            self._send(HTTPStatus.CONFLICT, self._page("决定未执行", f"<p>{_esc(exc)}</p>", session=session))
            return
        self.app.logger.info("console decision approval=%s outcome=%s", approval_id, result.get("outcome"))
        self._redirect(f"/approval/{approval_id}")

    def _reconcile(self, path: str, form: dict[str, str], session: dict[str, Any]) -> None:
        # 控制台里的人工对账。只记录人的判断，不触发任何发布。
        try:
            attempt_id = int(path.split("/")[2])
        except (IndexError, ValueError):
            self._send(HTTPStatus.BAD_REQUEST, "bad attempt id")
            return
        attempt = self.app.db.fetchone("SELECT * FROM publish_attempts WHERE id=?", (attempt_id,))
        if attempt is None:
            self._send(HTTPStatus.NOT_FOUND, "attempt not found")
            return
        approval = get_approval(self.app.db, approval_id=attempt["approval_id"])
        if approval is None:
            self._send(HTTPStatus.NOT_FOUND, "approval not found")
            return
        resolution = str(form.get("resolution", ""))
        try:
            result = reconcile_unknown(
                self.app.db,
                self.app.config,
                self.app.clock,
                approval_code=approval["code"],
                resolution=resolution,
                note=str(form.get("note", ""))[:500],
                external_post_id=str(form.get("external_post_id", "")).strip() or None,
                external_url=str(form.get("external_url", "")).strip() or None,
                actor="console-owner",
            )
        except Exception as exc:  # noqa: BLE001
            self._send(HTTPStatus.CONFLICT, self._page("核对失败", f"<p>{_esc(exc)}</p>", session=session))
            return
        self.app.logger.info("console reconcile attempt=%s result=%s", attempt_id, result)
        self._redirect(f"/approval/{approval['id']}")


class ConsoleApp:
    def __init__(
        self,
        config: Config,
        *,
        clock: Any | None = None,
        db: Database | None = None,
        login_token: str | None = None,
        llm: Any | None = None,
    ):
        self.config = config
        self.clock = clock or SystemClock()
        self.db = db or Database(config.db_path)
        self.llm = llm
        self.login_token = login_token or config.secrets.console_token
        self.generated_token = False
        if not self.login_token:
            # 没有配置控制台口令时，生成一个一次性随机口令，
            # 在启动终端打印出来。它不会写进配置文件，
            # 控制台重启后旧口令立即失效。
            self.login_token = secrets.token_urlsafe(18)
            self.generated_token = True
        self.sessions = SessionStore(ttl_seconds=int(config.get("console.session_ttl_minutes", 120)) * 60)
        self.logger = __import__("logging").getLogger("bot.console")

    def count(self, sql: str, params: tuple = ()) -> int:
        try:
            return int(self.db.scalar(sql, params, 0) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def make_server(self) -> ThreadingHTTPServer:
        # 绑定地址检查：默认必须是回环地址。
        # 只有配置里显式写 console.allow_non_loopback=true 才允许对外监听；
        # 即使如此，Host 头和登录/CSRF 仍然生效。
        host = str(self.config.get("console.host", "127.0.0.1"))
        if host not in {"127.0.0.1", "localhost", "::1"} and not self.config.get(
            "console.allow_non_loopback", False
        ):
            raise ConsoleError("console must bind to loopback unless allow_non_loopback=true")
        port = int(self.config.get("console.port", 8765))
        httpd = ThreadingHTTPServer((host, port), ConsoleHandler)
        httpd.daemon_threads = True
        httpd.app = self  # type: ignore[attr-defined]
        return httpd

    def token_hint(self) -> str:
        # 启动时告诉用户口令从哪来。只在“临时生成”时打印口令本身，
        # 配置好的口令绝不回显。
        if self.generated_token:
            return (
                "控制台口令未配置，已生成一次性口令："
                f"{self.login_token}\n请把它写入 BOT_CONSOLE_TOKEN 或 config/secrets.toml 的 console_token。"
            )
        return "控制台口令来自配置。"


def main(config_path: str | None = None) -> int:
    # 控制台进程入口：加载配置 -> 初始化日志 -> 创建并启动 HTTP 服务。
    # Ctrl+C 时安静退出，最后一定关闭监听端口。
    from .logging_setup import setup_logging

    config = load_config(config_path)
    setup_logging(
        config.log_dir,
        level=str(config.get("app.log_level", "INFO")),
        max_bytes=int(config.get("app.log_max_bytes", 5_000_000)),
        backups=int(config.get("app.log_backups", 5)),
    )
    app = ConsoleApp(config)
    server = app.make_server()
    print(app.token_hint())
    print(f"审批控制台: http://{config.get('console.host')}:{config.get('console.port')}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0

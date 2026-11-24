"""RSS/Atom and local fixture sources.

RSS is fetched conditionally (ETag / Last-Modified) through the guarded HTTP
client.  Fixture sources exist only for the offline demo and are refused in
live mode.
"""

# =============================================================================
# 中文导读：新闻“进货口”
# =============================================================================
# 这个文件负责把外部的 RSS/Atom 订阅，或者本地的演示 JSON，
# 统一变成项目内部的 SourceEntry 列表。
#
# RSS 是什么？
#   网站提供的一种“订阅格式”，通常是 XML，里面按时间列出最新文章。
#   Atom 是 RSS 的另一种规范，结构不同但作用一样。
#
# 条件请求（ETag / Last-Modified）是什么？
#   第一次抓取时，服务器会返回 ETag（内容版本号）和 Last-Modified（更新时间）。
#   下次请求带上它们，如果内容没变，服务器只回一个很小的 304，
#   不用重新传整篇文章。既省流量，也少打扰对方。
#
# 为什么要有 FixtureSource？
#   演示和测试不能依赖外网。本地 JSON 可以稳定地模拟“抓到了几篇新闻”。
#   但它只允许在演示模式使用；一旦是 live 模式，直接拒绝。
# =============================================================================

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from ..budget import BudgetGuard
from ..config import Config
from ..httpclient import FetchTooLarge, HttpClient, HttpTransportError, safe_http_error_text
from ..security import SSRFBlocked, SSRFGuard, canonicalize_url, normalize_ws, strip_html, truncate
from ..timeutil import Clock, parse_feed_datetime, to_iso
from .types import BudgetExceeded, NotConfigured, SourceEntry, SourceError, SourceFetch

MAX_TEXT = 20_000
DEEPSEEK_BASE = "https://api.deepseek.com"
DEEPSEEK_SEARCH_PATH = "/v1/tools/search"
DEEPSEEK_FETCH_PATH = "/v1/tools/fetch"
DEEPSEEK_CALL_USD = 0.00075  # official per successful nonempty search/fetch
# DeepSeek Search rejects queries longer than 128 characters.  Counting is in
# characters (Python len), and the generated " after:YYYY-MM-DD" suffix is part
# of that limit.  The adapter shortens deterministically before any reservation
# or network call, so an accidental long configured query cannot spend a call.
MAX_SEARCH_QUERY_CHARS = 128


class FetchLimit(SourceError):
    """The bounded physical-fetch allowance for one research task is spent."""

    def __init__(self, message: str = "research physical fetch attempt limit reached"):
        super().__init__(message, retryable=False, code="fetch_limit")


class PhysicalFetchQuota:
    """One hard physical-request budget shared by every research round.

    The quota lives at the task level.  Search rounds, the direct link and all
    transport retries draw from the same counter, so a second model-expanded
    round cannot silently restart the six-attempt allowance.
    """

    HARD_LIMIT = 6

    def __init__(self, limit: int = HARD_LIMIT):
        self.limit = max(0, min(self.HARD_LIMIT, int(limit)))
        self.attempts = 0
        self.retries = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.attempts)

    def ensure_available(self) -> None:
        if self.attempts >= self.limit:
            raise FetchLimit()

    def record_send(self, *, retry: bool = False) -> None:
        """Call immediately before a request is handed to the transport."""
        self.ensure_available()
        self.attempts += 1
        if retry:
            self.retries += 1


def build_search_query(
    base_query: str,
    after_date: str | None = None,
    *,
    limit: int = MAX_SEARCH_QUERY_CHARS,
) -> str:
    """Build a bounded search query with an optional date filter.

    When ``after_date`` is omitted the caller deliberately asks for an
    undated discovery pass (analysis/archive mode); it must not be silently
    turned back into a recent-news query.  The final evidence freshness gate
    remains separate and is never relaxed here.
    """
    text = " ".join(str(base_query or "").split())
    if not text:
        raise ValueError("search query is empty")
    after_date = str(after_date or "").strip()
    suffix = f" after:{after_date}" if after_date else ""
    if len(suffix) >= limit:
        raise ValueError("search query limit is too small for the date filter")
    budget = limit - len(suffix)
    if len(text) > budget:
        text = text[:budget].rstrip()
    query = f"{text}{suffix}"
    # Defensive: character slicing is exact, but keep the invariant explicit.
    if len(query) > limit:
        raise ValueError("search query exceeds its configured limit")
    return query



class SourceAdapter:
    # 所有来源适配器的基类。子类只需要实现 fetch()。
    # kind 是内部类型名，配置文件里用 kind="rss" / "fixture" 选择。
    kind = "base"

    def __init__(self, entry: dict, config: Config, http: HttpClient, clock: Clock):
        # entry 是 config 里的一个来源配置；http/clock 由 worker 注入，
        # 适配器自己不去创建网络客户端或读系统时间。
        self.entry = entry
        self.config = config
        self.http = http
        self.clock = clock
        self.name = str(entry["name"])
        self.url = str(entry.get("url") or entry.get("query") or "")

    def fetch(self, *, etag: str | None = None, last_modified: str | None = None) -> SourceFetch:
        # 基类只定义接口；真正实现由子类提供。
        raise NotImplementedError


class RssSource(SourceAdapter):
    kind = "rss"

    def fetch(self, *, etag: str | None = None, last_modified: str | None = None) -> SourceFetch:
        # 带上上次保存的 ETag / Last-Modified 做条件请求。
        # 服务器如果认为内容没变，会返回 304，我们就得到空列表。
        headers: dict[str, str] = {"Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        try:
            response = self.http.get(
                self.url,
                headers=headers,
                timeout=float(self.config.get("network.timeout_seconds", 20)),
                max_bytes=int(self.config.get("network.max_fetch_bytes", 2_097_152)),
            )
        except FetchTooLarge as exc:
            # 源文件太大 / 被 SSRF 拦截：重试也没用，标为不可重试。
            raise SourceError(f"feed too large: {exc}", retryable=False) from exc
        except SSRFBlocked as exc:
            raise SourceError(f"blocked feed URL: {exc}", retryable=False) from exc
        except HttpTransportError as exc:
            # 网络错误可能是一过性的，允许任务队列稍后重试。
            raise SourceError(f"feed fetch failed: {exc}", retryable=True) from exc

        if response.status == 304:
            # 内容没变，不需要解析。
            return SourceFetch(entries=[], not_modified=True, status=304)
        if response.status >= 500 or response.status == 429:
            # 服务器临时故障或限流：可以重试。
            raise SourceError(f"feed HTTP {response.status}", retryable=True, status=response.status)
        if response.status >= 400:
            # 404/403 之类：地址错了或没权限，重试无意义。
            raise SourceError(f"feed HTTP {response.status}", retryable=False, status=response.status)
        entries = self._parse(response.body, base_url=response.url)
        return SourceFetch(
            entries=entries,
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
            status=response.status,
        )

    def _parse(self, body: bytes, *, base_url: str) -> list[SourceEntry]:
        # 解析 XML。注意安全：这里用标准库 ElementTree，不执行 XML 里的
        # 任何外部实体，也不把内容当代码。
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            # XML 坏了可能是传输截断，允许重试。
            raise SourceError(f"feed XML parse error: {exc}", retryable=True) from exc
        # RSS/Atom 常带命名空间，例如 {http://www.w3.org/2005/Atom}title。
        # 这里把命名空间前缀去掉，后面找标签就简单了。
        for element in root.iter():
            if "}" in element.tag:
                element.tag = element.tag.split("}", 1)[1]
        items: list[ET.Element]
        if root.tag == "feed":
            # Atom 格式：<feed> 下面直接是 <entry>。
            items = list(root.findall("entry"))
        elif root.tag == "rss":
            # RSS 2.0：<rss><channel><item>。
            channel = root.find("channel")
            items = list(channel.findall("item")) if channel is not None else []
        else:
            # RSS 1.0 (RDF) 等冷门格式，尽量兜底找 item/entry。
            items = list(root.findall(".//item")) or list(root.findall(".//entry"))
        max_items = int(self.config.get("limits.max_items_per_source", 30))
        parsed: list[SourceEntry] = []
        seen: set[str] = set()
        # 多读一倍再截断：有些条目缺少标题或链接会被跳过，
        # 多留一些余量，最终仍能凑够 max_items。
        for item in items[: max_items * 2]:
            entry = self._parse_item(item, base_url)
            if entry is None:
                continue
            canonical = canonicalize_url(entry.url)
            if canonical in seen:
                continue
            seen.add(canonical)
            parsed.append(entry)
            if len(parsed) >= max_items:
                break
        return parsed

    def _parse_item(self, item: ET.Element, base_url: str) -> SourceEntry | None:
        # 单条新闻解析。任何关键字段缺失就返回 None（跳过这条），
        # 不让一条坏数据毁掉整个订阅源。
        title = normalize_ws(_text(item, "title"))
        if not title:
            return None
        link = _text(item, "link")
        if not link:
            link_el = item.find("link")
            if link_el is not None:
                link = link_el.attrib.get("href", "")
        link = link.strip()
        if not link:
            link = _text(item, "guid")
        if not link:
            return None
        # A guid is not necessarily a URL.
        # 有些源的 guid 只是一串内部编号，不能当链接用；
        # 只有 http/https 才直接采用，其他走相对路径解析。
        if link.startswith("http://") or link.startswith("https://"):
            absolute = link
        else:
            absolute = urljoin(base_url, link)
        if urlsplit(absolute).scheme.lower() not in {"http", "https"}:
            return None
        summary = normalize_ws(strip_html(_text(item, "description") or _text(item, "summary")))
        # 正文优先取 content:encoded（很多 RSS 在这里放全文），
        # 其次是 Atom 的 content，最后退回摘要。全部去 HTML 标签。
        content = strip_html(
            _text(item, "encoded") or _text(item, "content") or summary
        )
        content = normalize_ws(content)
        if len(content) > MAX_TEXT:
            content = content[:MAX_TEXT]
        author = normalize_ws(strip_html(_text(item, "author") or _text(item, "creator")))
        published_raw = _text(item, "pubDate") or _text(item, "published") or _text(item, "updated") or _text(item, "date")
        # 时间先保留原始字符串，由 timeutil.parse_feed_datetime 统一解析，
        # 并记录“精度”（精确到时间/只到日期/未知）。
        return SourceEntry(
            title=title[:300],
            url=absolute,
            summary=truncate(summary, 2_000, ""),
            content=content,
            author=author[:120],
            published_raw=published_raw,
        )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    # API credentials must never cross a redirect.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class DeepSeekSearchSource(SourceAdapter):
    """Optional Search/Fetch source on the fixed official DeepSeek endpoint.

    Every physical search/fetch HTTP attempt reserves a fixed USD amount before
    network I/O.  A known empty response settles at zero; each ambiguous
    attempt keeps its own conservative reservation.  Result URLs are never
    fetched directly, so the provider bearer token is only sent to
    api.deepseek.com.
    """

    kind = "deepseek_search"

    def __init__(
        self,
        entry: dict,
        config: Config,
        http: HttpClient,
        clock: Clock,
        *,
        budget: BudgetGuard | None = None,
        sleep=time.sleep,
        fetch_quota: PhysicalFetchQuota | None = None,
        allow_all_domains: bool = False,
    ):
        super().__init__(entry, config, http, clock)
        self.query = str(entry.get("query") or "").strip()
        if not self.query:
            raise NotConfigured("deepseek_search source requires a query")
        self.budget = budget
        if self.budget is None:
            raise NotConfigured("deepseek_search source requires the budget guard")
        self.api_key = config.secrets.deepseek_api_key
        if not self.api_key:
            raise NotConfigured("DEEPSEEK_API_KEY is missing; search source is disabled")
        self.allowed_domains = domain_allowlist(entry.get("allowed_domains"))
        self.allow_all_domains = bool(allow_all_domains or entry.get("allow_all_domains"))
        if not self.allowed_domains and not self.allow_all_domains:
            raise NotConfigured("deepseek_search source requires allowed_domains")
        max_items = int(config.get("limits.max_items_per_source", 30))
        self.max_results = max(1, min(10, max_items, int(entry.get("max_results", 3))))
        self.max_fetches = max(0, min(self.max_results, int(entry.get("max_fetches", 1))))
        # S1: a research task may make at most six physical fetch attempts,
        # including retries.  The hard cap is six; config may lower it.  The
        # task-level quota object is shared across the direct link and every
        # search round.
        self.max_fetch_attempts = max(
            1, min(PhysicalFetchQuota.HARD_LIMIT, int(entry.get("max_fetch_attempts", 6)))
        )
        self.fetch_quota = fetch_quota or PhysicalFetchQuota(self.max_fetch_attempts)
        try:
            lookback_days = int(entry.get("lookback_days", 3))
        except (TypeError, ValueError):
            lookback_days = 3
        # Bound the lookback so a typo cannot turn the query into "everything"
        # or into a single instant.
        self.lookback_days = max(1, min(30, lookback_days))
        self.discovery_mode = (
            "any" if str(entry.get("discovery_mode") or "recent").lower() == "any" else "recent"
        )
        self.should_fetch_content = bool(entry.get("fetch_content", True))
        self.timeout = min(120.0, max(1.0, float(config.get("network.timeout_seconds", 20))))
        self.max_bytes = min(4_194_304, max(1024, int(config.get("network.max_fetch_bytes", 2_097_152))))
        self.max_retries = max(0, min(2, int(config.get("network.max_retries", 2))))
        self._sleep = sleep
        self.guard = http.guard if http is not None else SSRFGuard()

    def fetch(self, *, etag: str | None = None, last_modified: str | None = None) -> SourceFetch:
        results = self._search()
        entries: list[SourceEntry] = []
        fetches = 0
        for result in results:
            if not isinstance(result, dict):
                continue
            url = str(result.get("url") or "").strip()
            if not self._allowed_url(url):
                continue
            title = normalize_ws(strip_html(str(result.get("title") or "")))[:300]
            if not title:
                continue
            content = ""
            if self.should_fetch_content and fetches < self.max_fetches:
                fetches += 1
                try:
                    content = self._fetch_content(url)
                except SourceError:
                    # Fetch is optional evidence enrichment; fall back to the
                    # search result's explicit content field, never its summary.
                    content = ""
            if not content:
                content = normalize_ws(strip_html(str(result.get("content") or "")))
            if not content:
                # Empty content is not evidence.  Do not synthesize a summary.
                continue
            entries.append(
                SourceEntry(
                    title=title,
                    url=url,
                    # The provider's "summary" is generated text, not quoted
                    # source content.  Keep it out of the stored entry so
                    # _pick_quotes can never promote it into evidence.
                    summary="",
                    content=content[:MAX_TEXT],
                    # Retain the provider's original publication timestamp.
                    # A missing date stays unknown; it is never stamped "now".
                    published_raw=str(result.get("date") or "").strip(),
                )
            )
            if len(entries) >= self.max_results:
                break
        return SourceFetch(
            entries=entries,
            status=200,
            note="deepseek-search",
            diagnostics={
                "provider": "deepseek-search",
                "query": self.query,
                "raw_count": len(results),
                "fetched_count": fetches,
                "entries_count": len(entries),
            },
        )

    def safe_url(self, url: str) -> tuple[bool, str]:
        """SSRF/scheme check without requiring the configured domain allowlist.

        A supplied public URL may be fetched directly (S1), but private hosts,
        embedded credentials and non-HTTP schemes are still rejected.
        """
        raw = str(url or "").strip()
        if not raw:
            return False, "url_unsafe"
        try:
            parts = urlsplit(raw)
            if parts.scheme.lower() not in {"http", "https"}:
                return False, "url_unsafe"
            if parts.username or parts.password:
                return False, "url_unsafe"
            self.guard.validate(raw, allow_private=False, resolve=False)
            if not parts.hostname:
                return False, "url_unsafe"
        except (SSRFBlocked, ValueError):
            return False, "url_unsafe"
        return True, "ok"

    def search_candidates(self) -> tuple[list[dict], dict]:
        """Return all raw search candidates plus sanitized diagnostics.

        This method intentionally does not fetch article bodies and never treats
        the provider's generated summary as content.
        """
        results = self._search()
        candidates: list[dict] = []
        for ordinal, result in enumerate(results):
            if not isinstance(result, dict):
                continue
            raw_url = str(result.get("url") or "").strip()
            safe, safe_code = self.safe_url(raw_url)
            host = ""
            try:
                host = (urlsplit(raw_url).hostname or "").lower().rstrip(".")
            except ValueError:
                host = ""
            domain_verified = bool(host) and host_matches_allowed_domains(
                host, self.allowed_domains
            )
            title = normalize_ws(strip_html(str(result.get("title") or "")))[:300]
            explicit_content = normalize_ws(
                strip_html(str(result.get("content") or ""))
            )[:MAX_TEXT]
            published_raw = str(result.get("date") or "").strip()
            candidates.append(
                {
                    "ordinal": ordinal,
                    "title": title,
                    "url": raw_url,
                    "canonical_url": canonicalize_url(raw_url) if raw_url else "",
                    "domain": host,
                    "domain_verified": domain_verified,
                    "url_safe": safe,
                    "url_code": safe_code,
                    "published_raw": published_raw,
                    "raw_content": explicit_content,
                    "summary_present": bool(str(result.get("summary") or "").strip()),
                    "provider": "deepseek-search",
                }
            )
        diagnostics = {
            "provider": "deepseek-search",
            "query": self.query,
            "raw_count": len(results),
            "candidates": [
                {
                    "ordinal": item["ordinal"],
                    "url": item["canonical_url"],
                    "domain": item["domain"],
                    "domain_verified": item["domain_verified"],
                    "url_safe": item["url_safe"],
                    "summary_present": item["summary_present"],
                }
                for item in candidates
            ],
            "counts": {
                "raw_results": len(results),
                "candidates": len(candidates),
                "url_safe": sum(1 for item in candidates if item["url_safe"]),
                "domain_verified": sum(
                    1 for item in candidates if item["domain_verified"]
                ),
            },
        }
        return candidates, diagnostics

    def fetch_content(self, url: str) -> dict:
        """Fetch one article body with the same bounded, SSRF-safe transport.

        The caller owns domain trust decisions.  This method only guarantees
        that the request is HTTP(S), public, and bounded by the physical-attempt
        cap.  It does not consult the configured source allowlist.
        """
        safe, code = self.safe_url(url)
        if not safe:
            raise SourceError("unsafe URL for content fetch", retryable=False, code=code)
        attempts_before = int(self.fetch_quota.attempts)
        retries_before = int(self.fetch_quota.retries)
        started = time.monotonic()
        direct_fallback = False
        try:
            content = self._fetch_content(url)
        except SourceError as exc:
            # DeepSeek's fetch proxy occasionally returns 404 for a live public
            # page (observed on blog.google).  Fall back once to the same
            # SSRF-safe direct HTTP client; this is still one bounded physical
            # attempt from the shared quota and never bypasses URL safety.
            if int(exc.status or 0) != 404:
                raise
            self.fetch_quota.record_send(retry=True)
            try:
                response = self.http.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; OpsAgent/1.0)"},
                    timeout=self.timeout,
                    max_bytes=self.max_bytes,
                    retries=0,
                )
            except Exception as direct_exc:  # noqa: BLE001 - report as source failure
                raise SourceError(
                    f"deepseek fetch HTTP 404; direct fallback failed: {direct_exc}",
                    retryable=False,
                    status=int(exc.status or 0),
                    code=exc.code or "http_error",
                ) from direct_exc
            if response.status != 200:
                raise SourceError(
                    f"deepseek fetch HTTP 404; direct fallback HTTP {response.status}",
                    retryable=False,
                    status=response.status,
                    code="http_error",
                ) from exc
            content = normalize_ws(strip_html(response.text()))
            if not content:
                raise SourceError(
                    "deepseek fetch HTTP 404; direct fallback extracted no text",
                    retryable=False,
                    status=404,
                    code=exc.code or "http_error",
                ) from exc
            content = content[:MAX_TEXT]
            direct_fallback = True
        return {
            "content": content,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "status": 200,
            "direct_fallback": direct_fallback,
            "physical_attempts": int(self.fetch_quota.attempts) - attempts_before,
            "retries": int(self.fetch_quota.retries) - retries_before,
            "quota_attempts": int(self.fetch_quota.attempts),
            "quota_limit": int(self.fetch_quota.limit),
        }

    def _search(self) -> list:
        started = time.monotonic()
        after_date = (
            (self.clock.now() - timedelta(days=self.lookback_days)).date().isoformat()
            if self.discovery_mode == "recent"
            else ""
        )
        # Replace any configured after: token with one derived from the injected
        # clock on every fetch.  The example query therefore never freezes a
        # date or keeps returning the same high-ranking old pages.
        base_query = re.sub(r"(?i)\bafter:\S+\s*", "", self.query).strip()
        if not base_query:
            raise SourceError("DeepSeek search query is empty after removing the date filter", retryable=False)
        # Bound before _post(): an oversized query is rejected locally by the
        # provider and must never consume a paid/ambiguous reservation.
        query = build_search_query(base_query, after_date)
        payload, reservation = self._post(
            DEEPSEEK_SEARCH_PATH,
            {"query": query, "count": self.max_results, "need_content": True},
        )
        if payload.get("success") is False:
            exc = SourceError("DeepSeek search reported failure", retryable=False)
            self._retain(reservation, exc, started)
            raise exc
        results = payload.get("results")
        if not isinstance(results, list):
            exc = SourceError("DeepSeek search response has no results array", retryable=False)
            self._retain(reservation, exc, started)
            raise exc
        if not results:
            # Official docs: an empty search is not billed.
            self.budget.settle_fixed(reservation, cost_usd=0.0)
            return []
        self.budget.settle_fixed(reservation, cost_usd=DEEPSEEK_CALL_USD)
        return results

    def _fetch_content(self, url: str) -> str:
        started = time.monotonic()
        payload, reservation = self._post(DEEPSEEK_FETCH_PATH, {"url": url})
        if payload.get("success") is False:
            exc = SourceError("DeepSeek fetch reported failure", retryable=False)
            self._retain(reservation, exc, started)
            raise exc
        content = normalize_ws(strip_html(str(payload.get("content") or "")))
        if not content:
            self.budget.settle_fixed(reservation, cost_usd=0.0)
            return ""
        self.budget.settle_fixed(reservation, cost_usd=DEEPSEEK_CALL_USD)
        return content[:MAX_TEXT]

    def _reserve(self, purpose: str, ref: str) -> int:
        return self.budget.reserve(
            DEEPSEEK_CALL_USD,
            provider="deepseek",
            model="deepseek-search",
            purpose=purpose,
            ref=ref[:200],
        )

    def _retain(self, reservation: int, exc: Exception, started: float) -> None:
        self.budget.retain_unknown(
            reservation,
            error=f"{type(exc).__name__}: {exc}"[:300],
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    def _allowed_url(self, url: str) -> bool:
        if not url:
            return False
        try:
            self.guard.validate(url, allow_private=False, resolve=False)
            host = (urlsplit(url).hostname or "").lower().rstrip(".")
        except (SSRFBlocked, ValueError):
            return False
        if not host:
            return False
        if self.allow_all_domains:
            return True
        return host_matches_allowed_domains(host, self.allowed_domains)

    def _post(self, path: str, payload: dict) -> tuple[dict, int]:
        """Send one logical request, reserving before every physical attempt.

        A timed-out-but-billed attempt keeps its own conservative reservation;
        the retry reserves again before touching the network.  The returned
        reservation belongs to the successful HTTP response and is settled by
        the caller according to the documented empty/nonempty outcome.
        """
        if path not in {DEEPSEEK_SEARCH_PATH, DEEPSEEK_FETCH_PATH}:
            raise SourceError("unsupported DeepSeek tool path", retryable=False)
        purpose = "search" if path == DEEPSEEK_SEARCH_PATH else "fetch"
        ref = str(payload.get("query") or payload.get("url") or "")
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            is_fetch = path == DEEPSEEK_FETCH_PATH
            if is_fetch:
                # The shared task quota is checked before reserving budget, so
                # a denied request is never counted as a physical attempt.
                self.fetch_quota.ensure_available()
            started = time.monotonic()
            reservation = self._reserve(purpose, ref)
            if is_fetch:
                # Only now is the request actually handed to the transport.
                self.fetch_quota.record_send(retry=attempt > 0)
            try:
                return self._post_once(path, payload), reservation
            except SourceError as exc:
                # The provider may have billed this attempt even though we did
                # not receive a usable response.
                self._retain(reservation, exc, started)
                last_error = exc
                if exc.retryable and attempt < self.max_retries:
                    self._sleep(min(8.0, 2.0**attempt))
                    continue
                raise
            except Exception as exc:  # noqa: BLE001 - ambiguous attempts are never free
                self._retain(reservation, exc, started)
                raise
        raise SourceError(f"DeepSeek API failed after retries: {last_error}", retryable=True)

    def _post_once(self, path: str, payload: dict) -> dict:
        """One physical HTTP request.  Retries/reservations live in _post."""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            DEEPSEEK_BASE + path,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "BotOperator/0.1",
            },
            method="POST",
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
        )
        try:
            with opener.open(request, timeout=self.timeout) as response:
                raw = response.read(self.max_bytes + 1)
                if len(raw) > self.max_bytes:
                    raise SourceError("DeepSeek tool response exceeds size limit", retryable=False)
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            detail = safe_http_error_text(exc, secret=self.api_key)
            if 300 <= exc.code < 400:
                raise SourceError(
                    "DeepSeek API redirect refused", retryable=False, code="http_redirect"
                ) from exc
            raise SourceError(
                f"DeepSeek API HTTP {exc.code}: {detail}",
                retryable=exc.code in {429, 500, 502, 503, 504},
                status=exc.code,
                code="http_error",
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            message = f"DeepSeek API network error: {type(exc).__name__}"
            timed_out = isinstance(exc, TimeoutError) or "timed out" in str(exc).lower()
            raise SourceError(
                message,
                retryable=True,
                code="timeout" if timed_out else "network_error",
            ) from exc
        if status != 200:
            raise SourceError(
                f"DeepSeek API HTTP {status}", retryable=True, status=status, code="http_error"
            )
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise SourceError("DeepSeek API returned invalid JSON", retryable=False) from exc
        if not isinstance(parsed, dict):
            raise SourceError("DeepSeek API JSON must be an object", retryable=False)
        return parsed


class FixtureSource(SourceAdapter):
    """Reads a local JSON feed used by the offline demo/tests."""

    kind = "fixture"

    def fetch(self, *, etag: str | None = None, last_modified: str | None = None) -> SourceFetch:
        # 演示来源是本地文件。live 模式必须拒绝，否则真实运营会以为
        # 自己抓到了新闻，实际上读的是演示数据。
        if self.config.is_live:
            raise NotConfigured("fixture sources are disabled in live mode")
        path = Path(self.url)
        if not path.is_absolute():
            path = (self.config.root / path).resolve()
        if not path.exists():
            raise SourceError(f"fixture not found: {path}", retryable=False)
        try:
            payload = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise SourceError(f"fixture read error: {exc}", retryable=False) from exc
        items = payload.get("items") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise SourceError("fixture must be a JSON object with an items array", retryable=False)
        now = self.clock.now()
        entries: list[SourceEntry] = []
        for item in items[: int(self.config.get("limits.max_items_per_source", 30))]:
            if not isinstance(item, dict) or not item.get("title") or not item.get("url"):
                continue
            published = str(item.get("published") or "").strip()
            # 演示数据可以用“相对现在几小时”的方式写发布时间，
            # 这样无论哪天运行，新闻看起来都是新鲜的。
            if not published and item.get("published_offset_hours") is not None:
                offset = float(item["published_offset_hours"])
                published = to_iso(now + timedelta(hours=offset))
            entries.append(
                SourceEntry(
                    title=str(item["title"]).strip()[:300],
                    url=str(item["url"]).strip(),
                    summary=normalize_ws(str(item.get("summary") or "")),
                    content=normalize_ws(str(item.get("content") or item.get("summary") or ""))[:MAX_TEXT],
                    author=str(item.get("author") or "")[:120],
                    published_raw=published,
                )
            )
        return SourceFetch(entries=entries, status=200, note="fixture")


def build_source(
    entry: dict,
    config: Config,
    http: HttpClient,
    clock: Clock,
    *,
    budget: BudgetGuard | None = None,
) -> SourceAdapter:
    # 工厂函数：根据配置里的 kind 创建对应适配器。
    # 未知类型直接报 NotConfigured，让人看到明确的配置错误。
    kind = str(entry.get("kind", "rss")).lower()
    if kind == "rss":
        return RssSource(entry, config, http, clock)
    if kind == "fixture":
        return FixtureSource(entry, config, http, clock)
    if kind in {"deepseek_search", "deepseek-search"}:
        return DeepSeekSearchSource(entry, config, http, clock, budget=budget)
    raise NotConfigured(f"unsupported source kind: {kind}")


def domain_allowlist(value) -> tuple[str, ...]:
    """Normalise configured domains; exact hosts unless ``*.``/leading dot."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    domains = []
    for item in value:
        text = str(item or "").strip().lower()
        if "://" in text:
            try:
                text = (urlsplit(text).hostname or "").lower()
            except ValueError:
                continue
        text = text.rstrip(".")
        subdomains = False
        if text.startswith("*."):
            subdomains = True
            text = text[2:]
        elif text.startswith("."):
            # Leading-dot spelling is also an explicit subdomain opt-in.
            subdomains = True
            text = text.lstrip(".")
        if not text or "/" in text or ":" in text:
            continue
        if not re.fullmatch(r"[a-z0-9.-]+\.[a-z0-9-]+", text):
            continue
        domains.append(("*." if subdomains else "") + text)
    return tuple(dict.fromkeys(domains))


def host_matches_allowed_domains(host: str, domains: tuple[str, ...] | list[str]) -> bool:
    """Apply the same exact-host/subdomain policy everywhere."""
    host = str(host or "").strip().lower().rstrip(".")
    if not host:
        return False
    for domain in domains:
        domain = str(domain or "").strip().lower()
        if domain.startswith("*."):
            base = domain[2:]
            if host == base or host.endswith("." + base):
                return True
        elif host == domain:
            return True
    return False


# Backwards-compatible private alias used by older call sites/tests.
_domain_allowlist = domain_allowlist


def _text(parent: ET.Element, tag: str) -> str:
    # 取子标签的文本。RSS 里同名标签可能有多个，find 只取第一个。
    # 如果标签没有直接文本，就把所有后代文本拼起来（Atom 常见）。
    found = parent.find(tag)
    if found is None:
        return ""
    if found.text:
        return found.text
    # Atom <content type="html"> sometimes nests text.
    return "".join(found.itertext())

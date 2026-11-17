"""AIHot discovery feed.

AIHot is treated as a discovery layer, never as an official source.  Its RSS
items carry a short editorial summary plus an outbound ``阅读原文`` link.  The
researcher follows the original link and labels the item as an aggregator only
when no original body can be fetched.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlsplit

from ..adapters.types import SourceEntry, SourceError
from ..security import canonicalize_url, normalize_ws, strip_html
from ..timeutil import Clock, parse_feed_datetime

DEFAULT_FEED_URL = "https://aihot.news/feed.xml"
AIHOT_HOSTS = {"aihot.news", "www.aihot.news"}
AIHOT_SUMMARY_PREFIX = "据 AIHot 汇总（原文未取到）："


@dataclass(frozen=True)
class AIHotItem:
    title: str
    item_url: str
    original_url: str = ""
    summary: str = ""
    published_raw: str = ""
    author: str = ""
    categories: tuple[str, ...] = ()

    @property
    def published_at(self):
        return parse_feed_datetime(self.published_raw)


class _DescriptionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href", "")
        if href:
            self.links.append(href.strip())

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.text.append(data.strip())


def is_aihot_url(url: str | None) -> bool:
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return host in AIHOT_HOSTS and "/items/" in str(url or "")


def _clean_summary(description: str, item_url: str) -> str:
    text = strip_html(description)
    lines = []
    for line in text.splitlines():
        value = normalize_ws(line)
        if not value:
            continue
        if "阅读原文" in value or "via AIHOT" in value or "AIHOT" in value:
            continue
        if item_url and item_url in value:
            continue
        lines.append(value)
    return "\n".join(lines).strip()


def _original_link(description: str) -> str:
    parser = _DescriptionParser()
    try:
        parser.feed(description or "")
    except Exception:
        return ""
    for href in parser.links:
        value = html.unescape(href.strip())
        if not value.lower().startswith(("http://", "https://")):
            continue
        try:
            host = (urlsplit(value).hostname or "").lower().rstrip(".")
        except ValueError:
            continue
        if host and host not in AIHOT_HOSTS:
            return value
    return ""


def _text(item: ET.Element, name: str) -> str:
    child = item.find(name)
    return "" if child is None or child.text is None else child.text.strip()


def parse_items(body: bytes, *, base_url: str = DEFAULT_FEED_URL) -> list[AIHotItem]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise SourceError(f"AIHot feed XML parse error: {exc}", retryable=True) from exc
    for element in root.iter():
        if "}" in element.tag:
            element.tag = element.tag.split("}", 1)[1]
    if root.tag == "feed":
        nodes = list(root.findall("entry"))
    elif root.tag == "rss":
        channel = root.find("channel")
        nodes = list(channel.findall("item")) if channel is not None else []
    else:
        nodes = list(root.findall(".//item")) or list(root.findall(".//entry"))

    items: list[AIHotItem] = []
    for node in nodes:
        title = normalize_ws(_text(node, "title"))
        link = _text(node, "link")
        if not link:
            link_el = node.find("link")
            if link_el is not None:
                link = link_el.attrib.get("href", "")
        link = link.strip()
        if not title or not link.lower().startswith(("http://", "https://")):
            continue
        description = _text(node, "description") or _text(node, "summary")
        if not description:
            encoded = node.find("encoded")
            if encoded is not None and encoded.text:
                description = encoded.text
        categories = tuple(normalize_ws(c.text or "") for c in node.findall("category") if normalize_ws(c.text or ""))
        items.append(
            AIHotItem(
                title=title,
                item_url=link,
                original_url=_original_link(description),
                summary=_clean_summary(description, link),
                published_raw=_text(node, "pubDate") or _text(node, "published") or _text(node, "updated"),
                author=normalize_ws(_text(node, "author") or _text(node, "creator")),
                categories=categories,
            )
        )
    return items


def fetch_items(http, config, *, limit: int = 50, feed_url: str | None = None) -> list[AIHotItem]:
    url = str(feed_url or config.get("topics.aihot_feed_url", DEFAULT_FEED_URL)).strip()
    if not url:
        raise SourceError("AIHot feed URL is empty", retryable=False)
    try:
        response = http.get(
            url,
            headers={"Accept": "application/rss+xml, application/xml, text/xml"},
            timeout=float(config.get("network.timeout_seconds", 20)),
            max_bytes=int(config.get("network.max_fetch_bytes", 2_097_152)),
            allow_private=True,
        )
    except Exception as exc:
        raise SourceError(f"AIHot feed fetch failed: {type(exc).__name__}: {exc}", retryable=True) from exc
    if response.status >= 400:
        raise SourceError(f"AIHot feed HTTP {response.status}", retryable=response.status >= 500, status=response.status)
    return parse_items(response.body, base_url=response.url)[: max(1, min(100, int(limit)))]


def resolve_item(http, config, item_url: str, *, limit: int = 100) -> AIHotItem | None:
    """Resolve an ``aihot.news/items/...`` URL against the feed."""
    if not is_aihot_url(item_url):
        return None
    target = canonicalize_url(item_url)
    for item in fetch_items(http, config, limit=limit):
        if canonicalize_url(item.item_url) == target:
            return item
    return None


def best_match(items: list[AIHotItem], keywords: tuple[str, ...]) -> AIHotItem | None:
    """Conservative lexical match for automatic discovery.

    Requires at least two distinct keyword hits (or one long CJK phrase) in the
    title/summary.  A weak match falls back to normal search instead of
    hijacking the topic.
    """
    wanted = [normalize_ws(k).lower() for k in keywords if normalize_ws(k)]
    if not wanted:
        return None
    best: tuple[int, AIHotItem] | None = None
    for item in items:
        haystack = f"{item.title}\n{item.summary}".lower()
        hits = {k for k in wanted if k in haystack}
        if not hits:
            continue
        long_hit = any(len(k) >= 4 and not k.isascii() and k in haystack for k in hits)
        if len(hits) < 2 and not long_hit:
            continue
        score = len(hits) * 10 + sum(len(k) for k in hits)
        if best is None or score > best[0]:
            best = (score, item)
    return best[1] if best else None


def summary_entry(item: AIHotItem) -> SourceEntry:
    """Build a clearly labelled aggregator entry when no original exists."""
    body = f"{AIHOT_SUMMARY_PREFIX}{item.summary}".strip()
    return SourceEntry(
        title=item.title,
        url=item.item_url,
        summary=item.summary,
        content=body,
        author=item.author,
        published_raw=item.published_raw,
    )

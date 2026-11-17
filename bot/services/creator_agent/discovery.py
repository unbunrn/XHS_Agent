"""V2 daily discovery source.

AIHot is a discovery layer: it provides fresh story leads and an outbound
original link.  The scheduled brief carries the original URL as a seed; the
agent still decides whether to read it, search further, or ask the owner.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from bot.services.aihot import fetch_items
from bot.security import normalize_ws, strip_html


@dataclass(frozen=True)
class DiscoveryItem:
    topic: str
    source_url: str
    summary: str
    published_at: str | None = None


def discover_aihot(
    http,
    config,
    clock,
    *,
    limit: int = 3,
    max_age_hours: int = 48,
    feed_limit: int = 50,
) -> list[DiscoveryItem]:
    """Return at most ``limit`` fresh, de-duplicated AIHot leads."""
    items = fetch_items(http, config, limit=feed_limit)
    now = clock.now()
    cutoff = now - timedelta(hours=max(1, int(max_age_hours)))
    out: list[DiscoveryItem] = []
    seen: set[str] = set()
    for item in items:
        title = normalize_ws(strip_html(item.title))[:300]
        if not title:
            continue
        published = item.published_at
        published_at = None
        if isinstance(published, tuple):
            published_at = published[0]
        elif published is not None:
            published_at = published
        if published_at is not None:
            if getattr(published_at, "tzinfo", None) is None:
                continue
            if published_at < cutoff:
                continue
        source_url = str(item.original_url or item.item_url or "").strip()
        key = title.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(
            DiscoveryItem(
                topic=title,
                source_url=source_url,
                summary=normalize_ws(strip_html(item.summary))[:1200],
                published_at=published_at.isoformat() if published_at else None,
            )
        )
        if len(out) >= max(1, int(limit)):
            break
    return out

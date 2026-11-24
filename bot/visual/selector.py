"""Theme & palette selection with cooldown rotation and recent-cover ring buffer."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from bot.db import get_setting, set_setting
from bot.timeutil import to_iso

from .themes import THEMES, Palette, Theme, available_themes

RECENT_SETTING_KEY = "visual.recent"
DEFAULT_RECENT_MAX = 12


def load_recent(db: Any) -> list[dict[str, Any]]:
    if db is None:
        return []
    try:
        if hasattr(db, "read"):
            with db.read() as conn:
                raw = get_setting(conn, RECENT_SETTING_KEY)
        else:
            raw = get_setting(db, RECENT_SETTING_KEY)
    except Exception:
        return []
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def record_recent(
    db: Any,
    entry: dict[str, Any],
    *,
    clock: Any = None,
    max_items: int = DEFAULT_RECENT_MAX,
) -> list[dict[str, Any]]:
    """Append one rendered/submitted visual entry into the ring buffer in settings."""
    if db is None:
        return []
    recent = load_recent(db)
    item = dict(entry)
    now_str = to_iso(clock.now()) if clock is not None else to_iso(datetime.now(timezone.utc))
    item.setdefault("at", now_str)

    # Deduplicate by run_key so repeated submit retries do not eat cooldown slots
    run_key = str(item.get("run_key") or "")
    if run_key:
        recent = [r for r in recent if str(r.get("run_key") or "") != run_key]
    recent.append(item)
    trimmed = recent[-max(1, int(max_items)) :]
    with db.tx() as conn:
        set_setting(conn, RECENT_SETTING_KEY, json.dumps(trimmed, ensure_ascii=False), now_str)
    return trimmed


def _hash_index(seed: str, count: int) -> int:
    if count <= 1:
        return 0
    digest = hashlib.sha256(str(seed).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % count


def select_theme_and_palette(
    *,
    run_key: str,
    layouts: list[str] | set[str] | None = None,
    suggested_theme: str = "",
    explicit_theme: str = "",
    recent: list[dict[str, Any]] | None = None,
    config: Any = None,
    registry: dict[str, Theme] | None = None,
) -> tuple[Theme, Palette]:
    """Choose an available theme and palette honoring cooldown and rotation."""
    avail = available_themes(config, registry=registry)
    if not avail:
        raise RuntimeError("no visual theme has available local fonts")
    by_id = {t.id: t for t in avail}

    recent_list = list(recent or [])
    cooldown = 2
    if config is not None and hasattr(config, "get"):
        cooldown = max(0, int(config.get("render.theme_cooldown", 2)))

    recent_themes = [str(r.get("theme") or "") for r in recent_list[-cooldown:]] if cooldown > 0 else []
    candidates = [t for t in avail if t.id not in recent_themes]
    if not candidates:
        candidates = list(avail)
    cand_ids = {t.id for t in candidates}

    chosen: Theme | None = None
    explicit = str(explicit_theme or "").strip().lower()
    if explicit and explicit in by_id:
        chosen = by_id[explicit]
    else:
        suggested = str(suggested_theme or "").strip().lower()
        if suggested and suggested in cand_ids:
            chosen = by_id[suggested]
        else:
            layout_set = {str(x).lower() for x in (layouts or []) if str(x).strip()}
            scored = sorted(
                candidates,
                key=lambda t: (
                    -len(layout_set & set(t.tags)),
                    _hash_index(f"{run_key}:{t.id}", 10_000),
                ),
            )
            # Pick deterministically among top-scoring or rotate across candidates
            top_score = len(layout_set & set(scored[0].tags)) if layout_set else 0
            top_group = [t for t in candidates if (len(layout_set & set(t.tags)) if layout_set else 0) == top_score]
            chosen = top_group[_hash_index(run_key, len(top_group))]

    # Palette rotation: exclude last palette used for this theme
    last_palette = ""
    for item in reversed(recent_list):
        if str(item.get("theme") or "") == chosen.id:
            last_palette = str(item.get("palette") or "")
            break
    pal_candidates = [p for p in chosen.palettes if p.name != last_palette]
    if not pal_candidates:
        pal_candidates = list(chosen.palettes)
    palette = pal_candidates[_hash_index(f"{run_key}:palette:{chosen.id}", len(pal_candidates))]
    return chosen, palette

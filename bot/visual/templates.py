"""HTML card templates for all 12 storyboard layouts and 5 cover variants.

All dynamic strings pass through ``render_inline_html`` / ``html.escape`` so
untrusted web/model text never executes scripts or loads external URLs.
"""

from __future__ import annotations

import hashlib
import html
import re
from pathlib import Path
from typing import Any

from bot.content import ALLOWED_ICONS, DraftContent, ImageCard
from bot.typography import render_inline_html

from .themes import ASSETS_DIR, Palette, Theme

_BASE_CSS: str | None = None
_FIT_JS: str | None = None
_THEME_CSS_CACHE: dict[str, str] = {}
_ICON_CACHE: dict[str, str] = {}


def _read_asset(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def base_css() -> str:
    global _BASE_CSS
    if _BASE_CSS is None:
        _BASE_CSS = _read_asset(ASSETS_DIR / "base.css")
    return _BASE_CSS


def fit_js() -> str:
    global _FIT_JS
    if _FIT_JS is None:
        _FIT_JS = _read_asset(ASSETS_DIR / "fit.js")
    return _FIT_JS


def theme_css(css_file: str) -> str:
    if css_file not in _THEME_CSS_CACHE:
        _THEME_CSS_CACHE[css_file] = _read_asset(ASSETS_DIR / "themes" / css_file)
    return _THEME_CSS_CACHE[css_file]


def icon_svg(name: str) -> str:
    clean = str(name or "").strip().lower()
    if clean not in ALLOWED_ICONS:
        return ""
    if clean not in _ICON_CACHE:
        svg_path = ASSETS_DIR / "icons" / f"{clean}.svg"
        _ICON_CACHE[clean] = _read_asset(svg_path) if svg_path.is_file() else ""
    svg = _ICON_CACHE[clean]
    return f'<span class="icon-svg" aria-hidden="true">{svg}</span>' if svg else ""


def _esc(text: str) -> str:
    return html.escape(str(text or ""), quote=True)


def _inline(text: str, emphasis: list[str] | None = None) -> str:
    return render_inline_html(str(text or ""), emphasis=emphasis)


def _headline_html(text: str, emphasis: list[str] | None = None) -> str:
    lines = str(text or "").split("\n")
    return "<br>".join(_inline(line, emphasis=emphasis) for line in lines if line or len(lines) == 1)


def _font_faces(theme: Theme) -> str:
    rules: list[str] = []
    slot_map = {"display": "BotDisplay", "body": "BotBody", "mono": "BotMono"}
    for slot, family in slot_map.items():
        path = theme.resolved_fonts.get(slot)
        if path is None:
            continue
        uri = path.resolve().as_uri()
        rules.append(
            f'@font-face{{font-family:"{family}";src:url("{uri}");font-display:block;}}'
        )
    return "\n".join(rules)


def _deterministic_vars(run_key: str, palette: Palette) -> str:
    digest = hashlib.sha256(str(run_key or "default").encode("utf-8")).digest()
    glow_x = -340 + (digest[0] % 160)
    glow_y = -280 + (digest[1] % 140)
    tilt = -3.5 + (digest[2] % 70) / 10.0
    pos_x = 12 + (digest[3] % 76)
    pos_y = 65 + (digest[4] % 25)
    hue_deg = (digest[5] % 17) - 8  # ±8° subtle deterministic variation
    items = [f"{k}:{v}" for k, v in palette.css_vars().items()]
    items.extend([
        f"--glow-x:{glow_x}px",
        f"--glow-y:{glow_y}px",
        f"--tilt:{tilt:.1f}deg",
        f"--glow-pos:{pos_x}% {pos_y}%",
        f"--hue-shift:{hue_deg}deg",
    ])
    return ";".join(items)


def _render_cover_body(card: ImageCard, theme: Theme) -> str:
    variant = (card.variant or "headline").lower()
    emph = list(card.emphasis or [])
    kicker_icon = icon_svg(card.icon) if card.icon else ""
    kicker_html = (
        f'<div class="kicker">{kicker_icon}<span>{_inline(card.kicker)}</span></div>'
        if card.kicker
        else ""
    )

    if variant == "stat" and card.stats:
        stat_blocks = []
        for s in card.stats[:3]:
            val = _esc(s.get("value", ""))
            unit = _esc(s.get("unit", ""))
            lbl = _inline(s.get("label", ""))
            unit_html = f"<small>{unit}</small>" if unit else ""
            stat_blocks.append(
                f'<div class="cover-stat-item">'
                f'<div class="stat-num" data-fit="32,120" data-slot="cover.stat">{val}{unit_html}</div>'
                f'<div class="stat-label">{lbl}</div>'
                f"</div>"
            )
        sub_html = (
            f'<div class="cover-subcard"><p class="cover-subhead">{_inline(card.subhead)}</p></div>'
            if card.subhead
            else ""
        )
        return (
            f"{kicker_html}"
            f'<div class="cover-hero">'
            f'<div class="cover-stat-row">{"".join(stat_blocks)}</div>'
            f'<h1 class="cover-headline" data-fit="28,88" data-fill="0.65" data-slot="cover.headline">'
            f"{_headline_html(card.headline, emphasis=emph)}</h1>"
            f"</div>{sub_html}"
        )

    if variant == "quote" and card.subhead:
        attr_html = (
            f'<div class="cover-attribution">—— {_inline(card.attribution)}</div>'
            if card.attribution
            else ""
        )
        return (
            f"{kicker_html}"
            f'<div class="cover-hero">'
            f'<div class="cover-quote-mark">“</div>'
            f'<p class="cover-quote-text" data-fit="28,72" data-fill="0.7" data-slot="cover.quote">'
            f"{_inline(card.subhead, emphasis=emph)}</p>"
            f"{attr_html}"
            f"</div>"
            f'<div class="cover-subcard"><h1 class="cover-subhead">{_headline_html(card.headline, emphasis=emph)}</h1></div>'
        )

    if variant == "split":
        parts = [p.strip() for p in re.split(r"[｜|/\n]", card.headline) if p.strip()]
        left_txt = parts[0] if parts else card.headline
        right_txt = parts[1] if len(parts) > 1 else (card.subhead or "另一面")
        sub_html = (
            f'<div class="cover-subcard"><p class="cover-subhead">{_inline(card.subhead)}</p></div>'
            if card.subhead
            else ""
        )
        return (
            f"{kicker_html}"
            f'<div class="cover-hero">'
            f'<div class="cover-split-grid">'
            f'<div class="cover-split-box"><h1 class="cover-headline" data-fit="26,88" data-slot="cover.left">{_inline(left_txt, emphasis=emph)}</h1></div>'
            f'<div class="cover-split-box accent-box"><h1 class="cover-headline" data-fit="26,88" data-slot="cover.right">{_inline(right_txt, emphasis=emph)}</h1></div>'
            f"</div></div>{sub_html}"
        )

    # Default "headline" or "question"
    quote_glyph = '<div class="cover-quote-badge">“</div>' if theme.id == "editorial" else ""
    sub_html = ""
    if card.subhead:
        sub_html = (
            f'<div class="cover-subcard">{quote_glyph}'
            f'<p class="cover-subhead">{_inline(card.subhead)}</p></div>'
        )
    return (
        f"{kicker_html}"
        f'<div class="cover-hero">'
        f'<h1 class="cover-headline" data-fit="28,156" data-fill="0.72" data-slot="cover.headline">'
        f"{_headline_html(card.headline, emphasis=emph)}</h1>"
        f"</div>{sub_html}"
    )


def _render_content_body(card: ImageCard) -> str:
    emph = list(card.emphasis or [])
    kicker_icon = icon_svg(card.icon) if card.icon else ""
    kicker_html = (
        f'<div class="kicker">{kicker_icon}<span>{_inline(card.kicker)}</span></div>'
        if card.kicker
        else ""
    )
    sub_html = f'<p class="card-subhead">{_inline(card.subhead)}</p>' if card.subhead and card.layout not in {"annotated", "quote"} else ""
    header = (
        f'<div class="card-header">{kicker_html}'
        f'<h2 class="card-headline" data-fit="24,68" data-slot="card.headline">{_inline(card.headline, emphasis=emph)}</h2>'
        f"{sub_html}</div>"
    )

    layout = card.layout
    body_html = ""

    if layout in {"comparison", "checklist"}:
        cols = card.columns[:2]
        panels = []
        for idx, col in enumerate(cols):
            cls = "col-panel col-panel-accent" if idx == 0 else "col-panel col-panel-alt"
            prefix = ("✓ " if idx == 0 else "✗ ") if layout == "checklist" else ""
            panels.append(
                f'<div class="{cls}" data-check-overflow data-slot="col.{idx}">'
                f'<div class="col-label">{_esc(prefix)}{_inline(col.label)}</div>'
                f'<p class="col-text" data-fit="22,60" data-slot="col.text.{idx}">{_inline(col.text)}</p>'
                f"</div>"
            )
        body_html = f'<div class="two-col-grid">{"".join(panels)}</div>'

    elif layout == "stat":
        items = []
        for idx, s in enumerate(card.stats[:3]):
            val = _esc(s.get("value", ""))
            unit = _esc(s.get("unit", ""))
            lbl = _inline(s.get("label", ""))
            unit_html = f"<small>{unit}</small>" if unit else ""
            items.append(
                f'<div class="stat-card" data-check-overflow data-slot="stat.{idx}">'
                f'<div class="stat-card-num" data-fit="28,112" data-slot="stat.val.{idx}">{val}{unit_html}</div>'
                f'<div class="stat-card-label" data-fit="24,40" data-slot="stat.lbl.{idx}">{lbl}</div>'
                f"</div>"
            )
        body_html = f'<div class="stat-grid">{"".join(items)}</div>'

    elif layout == "quote":
        attr = f'<div class="quote-attr">—— {_inline(card.attribution)}</div>' if card.attribution else ""
        body_html = (
            f'<div class="quote-stage">'
            f'<div class="cover-quote-mark">“</div>'
            f'<p class="quote-text" data-fit="26,56" data-slot="quote.subhead">{_inline(card.subhead, emphasis=emph)}</p>'
            f"{attr}</div>"
        )

    elif layout == "timeline":
        items = []
        for idx, pt in enumerate(card.points):
            parts = pt.split(None, 1)
            date_part = parts[0]
            desc_part = parts[1] if len(parts) > 1 else ""
            if not desc_part:
                m = re.match(r"^([^\s，,：:]+[年月日号\d./-]+)[，,：:\s]*(.+)$", pt)
                if m:
                    date_part, desc_part = m.group(1), m.group(2)
            items.append(
                f'<div class="timeline-item">'
                f'<div class="timeline-date">{_inline(date_part)}</div>'
                f'<p class="timeline-desc" data-fit="20,34" data-slot="tl.{idx}">{_inline(desc_part or pt, emphasis=emph)}</p>'
                f"</div>"
            )
        body_html = f'<div class="timeline-list">{"".join(items)}</div>'

    elif layout == "qa":
        items = []
        for idx, pt in enumerate(card.points[:4]):
            sep = "｜" if "｜" in pt else "|"
            q_part, a_part = pt.split(sep, 1) if sep in pt else (pt, "")
            items.append(
                f'<div class="qa-item">'
                f'<div class="qa-q" data-fit="20,34" data-slot="qa.q.{idx}"><span>Q</span><span>{_inline(q_part.strip(), emphasis=emph)}</span></div>'
                f'<div class="qa-a" data-fit="20,34" data-slot="qa.a.{idx}"><span>A</span><span>{_inline(a_part.strip(), emphasis=emph)}</span></div>'
                f"</div>"
            )
        body_html = f'<div class="item-list">{"".join(items)}</div>'

    elif layout == "annotated":
        notes = []
        for idx, pt in enumerate(card.points):
            notes.append(
                f'<div class="list-card">'
                f'<div class="list-badge">注{idx + 1}</div>'
                f'<p class="list-text" data-fit="20,34" data-slot="ann.{idx}">{_inline(pt, emphasis=emph)}</p>'
                f"</div>"
            )
        body_html = (
            f'<div class="excerpt-box">'
            f'<div class="excerpt-tag">来源摘录</div>'
            f'<p class="excerpt-text" data-fit="20,36" data-slot="ann.excerpt">{_inline(card.subhead)}</p>'
            f"</div>"
            f'<div class="item-list">{"".join(notes)}</div>'
        )

    else:
        # process / keypoints / diagram / argument
        items = []
        for idx, pt in enumerate(card.points):
            if layout == "diagram":
                items.append(
                    f'<div class="list-card diagram-card">'
                    f'<p class="list-text" data-fit="20,36" data-slot="diag.{idx}">{_inline(pt, emphasis=emph)}</p>'
                    f"</div>"
                )
            elif layout == "argument":
                items.append(
                    f'<div class="list-card arg-card">'
                    f'<div class="arg-tag">理由 {idx + 1}</div>'
                    f'<p class="list-text" data-fit="20,36" data-slot="arg.{idx}">{_inline(pt, emphasis=emph)}</p>'
                    f"</div>"
                )
            else:
                badge = str(idx + 1) if layout == "process" else f"{idx + 1:02d}"
                items.append(
                    f'<div class="list-card">'
                    f'<div class="list-badge">{badge}</div>'
                    f'<p class="list-text" data-fit="20,36" data-slot="pt.{idx}">{_inline(pt, emphasis=emph)}</p>'
                    f"</div>"
                )
        body_html = f'<div class="item-list">{"".join(items)}</div>'

    return f'{header}<div class="card-body" data-check-overflow data-slot="card.body">{body_html}</div>'


def build_card_html(
    card: ImageCard,
    *,
    theme: Theme,
    palette: Palette,
    page: int,
    total: int,
    account_label: str,
    run_key: str,
    date_label: str = "",
    footer_disclaimer: str = "last",
    disclaimer_text: str = "公开资料整理，未做个人实测",
) -> str:
    """Build a standalone self-contained HTML document for one card."""
    is_cover = card.layout == "cover"
    show_disclaimer = (
        footer_disclaimer == "every"
        or (footer_disclaimer == "last" and page == total)
    )
    note_html = f'<span class="card-note">{_inline(card.note)}</span>' if card.note else "<span></span>"
    disc_html = f'<span class="card-disclaimer">{_esc(disclaimer_text)}</span>' if show_disclaimer else ""
    footer_html = (
        f'<div class="card-footer">{note_html}{disc_html}</div>'
        if (card.note or show_disclaimer)
        else ""
    )

    page_label = f"{page:02d}/{total:02d}" if theme.folio_style == "swiss" else f"{page}/{total}"
    right_folio = date_label if (is_cover and date_label and theme.id in {"editorial", "newsprint"}) else page_label
    folio_html = (
        f'<div class="folio">'
        f'<div class="folio-left"><span>{_esc(account_label)}</span></div>'
        f'<span class="folio-page">{_esc(right_folio)}</span>'
        f"</div>"
    )

    body_content = _render_cover_body(card, theme) if is_cover else _render_content_body(card)
    body_classes = f"theme-{_esc(theme.id)}" + (" is-cover" if is_cover else "")
    css_vars = _deterministic_vars(run_key, palette)

    return (
        "<!doctype html>"
        "<html><head><meta charset=\"utf-8\">"
        f"<style>{_font_faces(theme)}\n{base_css()}\n{theme_css(theme.css_file)}</style>"
        f"</head><body class=\"{body_classes}\" style=\"{css_vars}\">"
        f'<div class="canvas" data-check-overflow data-slot="canvas">{folio_html}<div class="main-area" data-check-overflow data-slot="main">{body_content}</div>{footer_html}</div>'
        "</body></html>"
    )

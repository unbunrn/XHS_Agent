"""Playwright + local Microsoft Edge HTML-to-PNG card rendering engine."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from bot.content import DraftContent, ImageCard
from bot.render import RenderError, RenderedImage
from bot.timeutil import local_iso
from datetime import datetime, timezone

from .selector import load_recent, select_theme_and_palette
from .templates import build_card_html, fit_js
from .themes import THEMES, Palette, Theme

log = logging.getLogger(__name__)


class BrowserLaunchError(RenderError):
    """Raised when local Edge/Playwright cannot launch (eligible for fallback)."""


def render_storyboard_html(
    content: DraftContent,
    *,
    out_dir: str | Path,
    config: Any,
    draft_key: str,
    db: Any = None,
    theme_override: str | None = None,
    run_key: str | None = None,
    aborted_counter: list[str] | None = None,
) -> list[RenderedImage]:
    """Render storyboard cards via Playwright + local Edge into PNG files."""
    if content.storyboard is None:
        raise RenderError("render_storyboard_html requires a storyboard")

    cards: list[ImageCard] = content.storyboard.all_cards()
    max_cards = int(config.get("render.max_cards", 6))
    if len(cards) > max_cards:
        raise RenderError(
            f"storyboard has {len(cards)} cards but render.max_cards={max_cards}; shorten the storyboard"
        )

    width = int(config.get("render.width", 1080))
    height = int(config.get("render.height", 1440))
    card_timeout_ms = int(config.get("render.card_timeout_ms", 15000))
    footer_disclaimer = str(config.get("render.footer_disclaimer", "last") or "last").lower()
    channel = str(config.get("render.browser_channel", "msedge") or "msedge")
    executable = str(config.get("render.browser_executable", "") or "").strip()

    seed = run_key or draft_key
    suggested = content.editorial_plan.visual_theme if content.editorial_plan else ""
    recent = load_recent(db)
    try:
        chosen_theme, chosen_palette = select_theme_and_palette(
            run_key=seed,
            layouts={c.layout for c in cards},
            suggested_theme=suggested,
            explicit_theme=theme_override or "",
            recent=recent,
            config=config,
        )
    except Exception as exc:
        raise BrowserLaunchError(f"theme selection failed: {exc}") from exc

    account_label = "AI 工具观察"
    try:
        account_label = str(config.account().get("display_name") or account_label)[:20]
    except Exception:
        pass
    date_label = local_iso(datetime.now(timezone.utc), config.timezone_name)[:10].replace("-", ".")

    html_docs = [
        build_card_html(
            card,
            theme=chosen_theme,
            palette=chosen_palette,
            page=index,
            total=len(cards),
            account_label=account_label,
            run_key=seed,
            date_label=date_label,
            footer_disclaimer=footer_disclaimer,
        )
        for index, card in enumerate(cards, start=1)
    ]

    if executable and not Path(executable).is_file():
        raise BrowserLaunchError(f"configured browser_executable does not exist: {executable}")

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise BrowserLaunchError(f"playwright is not available: {exc}") from exc

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rendered: list[RenderedImage] = []

    pw = None
    browser = None
    context = None
    try:
        pw, browser, context, page = _open_page(
            width=width,
            height=height,
            channel=channel,
            executable=executable,
            aborted_counter=aborted_counter,
        )
        for index, html_doc in enumerate(html_docs, start=1):
            page.set_content(html_doc, wait_until="load", timeout=card_timeout_ms)
            page.evaluate("() => document.fonts.ready")
            page.evaluate(fit_js())
            overflow = page.evaluate("() => window.__overflow || []")
            if overflow:
                slots = ", ".join(str(item.get("slot") or "slot") for item in overflow)
                raise RenderError(f"card {index}: {slots} overflows; shorten it")

            path = out / f"{draft_key}-{index:02d}.png"
            page.screenshot(
                path=str(path),
                type="png",
                clip={"x": 0, "y": 0, "width": width, "height": height},
                timeout=card_timeout_ms,
            )
            with path.open("rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
            rendered.append(
                RenderedImage(
                    ordinal=index,
                    path=path,
                    sha256=digest,
                    width=width,
                    height=height,
                    theme=chosen_theme.id,
                    palette=chosen_palette.name,
                )
            )
    finally:
        for closer in (context, browser):
            if closer is not None:
                try:
                    closer.close()
                except Exception:
                    pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass

    return rendered


def _open_page(
    *,
    width: int,
    height: int,
    channel: str,
    executable: str,
    aborted_counter: list[str] | None = None,
):
    import os
    from playwright.sync_api import sync_playwright

    # Point TEMP/TMP to project-local var/tmp so Playwright's Node driver and
    # Edge never hit EPERM on restricted system Temp directories.
    project_root = Path(__file__).resolve().parent.parent.parent
    local_tmp = project_root / "var" / "tmp"
    try:
        local_tmp.mkdir(parents=True, exist_ok=True)
        os.environ["TEMP"] = str(local_tmp)
        os.environ["TMP"] = str(local_tmp)
        os.environ["TMPDIR"] = str(local_tmp)
    except OSError:
        pass
    node_exe = Path(r"D:\Dev\Tools\node-v22.23.2-win-x64\node.exe")
    if "PLAYWRIGHT_NODEJS_PATH" not in os.environ and node_exe.is_file():
        os.environ["PLAYWRIGHT_NODEJS_PATH"] = str(node_exe)

    pw = None
    browser = None
    context = None
    try:
        pw = sync_playwright().start()
        launch_kwargs: dict[str, Any] = {"headless": True}
        if executable:
            launch_kwargs["executable_path"] = executable
        elif channel:
            launch_kwargs["channel"] = channel
        browser = pw.chromium.launch(**launch_kwargs)
    except Exception as exc:
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass
        raise BrowserLaunchError(f"failed to launch browser ({channel}): {exc}") from exc

    context = browser.new_context(
        viewport={"width": width, "height": height},
        device_scale_factor=1,
    )

    def _route_guard(route) -> None:
        url = route.request.url or ""
        if url.startswith(("file://", "data:", "about:")):
            route.continue_()
        else:
            if aborted_counter is not None:
                aborted_counter.append(url)
            route.abort()

    context.route("**/*", _route_guard)
    page = context.new_page()
    return pw, browser, context, page

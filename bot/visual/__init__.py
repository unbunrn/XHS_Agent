"""HTML/CSS + local Edge visual rendering subsystem."""

from .engine import BrowserLaunchError, render_storyboard_html
from .selector import load_recent, record_recent, select_theme_and_palette
from .similarity import dhash, hamming, largest_blank_band
from .themes import THEMES, available_themes, theme_catalog

__all__ = [
    "BrowserLaunchError",
    "THEMES",
    "available_themes",
    "dhash",
    "hamming",
    "largest_blank_band",
    "load_recent",
    "record_recent",
    "render_storyboard_html",
    "select_theme_and_palette",
    "theme_catalog",
]

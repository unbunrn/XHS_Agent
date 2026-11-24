"""Deterministic image similarity (64-bit dHash) and blank-band detector."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None  # type: ignore[assignment]


def dhash(path: str | Path) -> int:
    """Compute a 64-bit difference hash (9x8 grayscale horizontal gradients)."""
    if Image is None:
        raise RuntimeError("Pillow is required for dhash")
    with Image.open(path) as im:
        gray = im.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        if hasattr(gray, "get_flattened_data"):
            pixels = list(gray.get_flattened_data())
        else:
            pixels = list(gray.getdata())
    bits = 0
    bit_index = 0
    for y in range(8):
        row_offset = y * 9
        for x in range(8):
            if pixels[row_offset + x + 1] > pixels[row_offset + x]:
                bits |= 1 << bit_index
            bit_index += 1
    return bits


def hamming(a: int, b: int) -> int:
    """Hamming distance between two integer hashes."""
    return (int(a) ^ int(b)).bit_count()


def _parse_bg_rgb(bg: str | tuple[int, int, int] | None, pixels, width: int, height: int) -> tuple[int, int, int]:
    if isinstance(bg, tuple) and len(bg) == 3:
        return (int(bg[0]), int(bg[1]), int(bg[2]))
    if isinstance(bg, str):
        raw = bg.strip().lstrip("#")
        if len(raw) == 6:
            try:
                return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))
            except ValueError:
                pass
    # Fallback: sample center-top margin interior
    sample_x = max(0, min(width - 1, width // 2))
    sample_y = max(0, min(height - 1, 20))
    px = pixels[sample_x, sample_y]
    return (int(px[0]), int(px[1]), int(px[2]))


def largest_blank_band(
    path: str | Path,
    bg: str | tuple[int, int, int] | None = None,
    *,
    margin_x: int = 96,
    diff_threshold: int = 48,
    min_active_ratio: float = 0.015,
) -> tuple[int, float]:
    """Return ``(start_y, ratio_of_height)`` of the longest contiguous blank horizontal band.

    Evaluates the interior content band ``[margin_x, width - margin_x)`` so outer
    theme frames/side-bars do not mask empty interior voids.  Uses each row's
    median pixel vs foreground contrast to stay resilient to gentle gradients
    while catching real typography, cards, and borders.
    """
    if Image is None:
        raise RuntimeError("Pillow is required for largest_blank_band")
    with Image.open(path) as im:
        rgb = im.convert("RGB")
        width, height = rgb.size
        if width <= 0 or height <= 0:
            return (0, 1.0)
        x0 = min(max(0, margin_x), max(0, width // 4))
        x1 = max(x0 + 1, width - x0)
        step_x = 4
        # Read raw bytes once for fast row slicing
        raw = rgb.tobytes()

    stride = width * 3
    cols = list(range(x0, x1, step_x))
    n_cols = max(1, len(cols))

    best_start = 0
    best_len = 0
    cur_start = 0
    cur_len = 0

    for y in range(height):
        row_base = y * stride
        # Sample row reference color from median luminance-like middle samples
        # plus check against global edge/center color so solid surfaces/borders count
        rs = [raw[row_base + x * 3] for x in cols]
        gs = [raw[row_base + x * 3 + 1] for x in cols]
        bs = [raw[row_base + x * 3 + 2] for x in cols]
        # Row median handles soft radial glows without false positives
        mid = len(rs) // 2
        med_r = sorted(rs)[mid]
        med_g = sorted(gs)[mid]
        med_b = sorted(bs)[mid]

        active = 0
        for r, g, b in zip(rs, gs, bs):
            if (abs(r - med_r) + abs(g - med_g) + abs(b - med_b)) > diff_threshold:
                active += 1
        is_blank = (active / n_cols) < min_active_ratio
        if is_blank:
            if cur_len == 0:
                cur_start = y
            cur_len += 1
            if cur_len > best_len:
                best_len = cur_len
                best_start = cur_start
        else:
            cur_len = 0

    return (best_start, round(best_len / float(height), 4))

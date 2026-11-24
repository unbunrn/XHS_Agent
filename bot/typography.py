"""Chinese typography normalization and HTML inline tokenization helpers.

This module cleans punctuation in Chinese prose without mutating numeric
expressions, Latin segments, or URLs, and marks non-breaking spans / CJK-Latin
spacing boundaries for the HTML card renderer.
"""

from __future__ import annotations

import html
import re
import unicodedata

# CJK Unified Ideographs + Extension A + Compatibility Ideographs
_CJK_CLASS = r"\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff"
_CJK_CHAR_RE = re.compile(f"[{_CJK_CLASS}]")
_CJK_OR_PUNCT_CLASS = f"{_CJK_CLASS}，。：；？！（）【】《》「」『』、……——“”‘’～"

_URL_SPLIT_RE = re.compile(
    r"((?:https?|ftp|file)://[^\s<>\"'\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]*[^\s<>\"',:;?!.\)\]\u4e00-\u9fff\u3000-\u303f\uff00-\uffef])"
)
_DIGIT_RANGE_TILDE_RE = re.compile(r"(?<=\d)(%?)\s*~\s*(?=\d)")
_ELLIPSIS_RE = re.compile(r"\.{3,}|…+")
_DASH_RE = re.compile(r"(?<!-)--(?!-)")
_PAIRED_PARENS_RE = re.compile(r"\(([^()\n]*)\)")
_PAIRED_QUOTES_RE = re.compile(r'"([^"\n]+)"')

_PUNCT_MAP = {
    ",": "，",
    ":": "：",
    ";": "；",
    "?": "？",
    "!": "！",
}

# Adjacency checks for ASCII punctuation in Chinese context:
# - Never touch `,` or `:` flanked by digits (`1,000`, `12:30`).
# - Convert when left is CJK/CJK-punct OR right (after optional horizontal space) is CJK/CJK-punct.
_PUNCT_LEFT_RE = re.compile(
    rf"(?<=[{_CJK_OR_PUNCT_CLASS}])([,:;?!])"
)
_PUNCT_RIGHT_RE = re.compile(
    rf"([,:;?!])(?=[ \t]*[{_CJK_OR_PUNCT_CLASS}])"
)
_SPACE_AFTER_ZH_PUNCT_RE = re.compile(r"([，：；？！])[ \t]+(?=[^\s])")


def _has_cjk(text: str) -> bool:
    return bool(_CJK_CHAR_RE.search(text))


def _normalize_chunk(chunk: str, *, fullwidth_tilde: bool) -> str:
    if not chunk or not _has_cjk(chunk):
        return chunk

    # 1. Ellipsis and em-dash in Chinese context
    chunk = _ELLIPSIS_RE.sub(
        lambda m: "……" if (m.group(0).startswith("…") or _has_cjk(chunk)) else m.group(0),
        chunk,
    )
    chunk = _DASH_RE.sub("——", chunk)

    # 2. Optional numeric range tilde (e.g. 98%~99% -> 98%～99%)
    if fullwidth_tilde:
        chunk = _DIGIT_RANGE_TILDE_RE.sub(lambda m: f"{m.group(1)}～", chunk)

    # 3. Paired parentheses adjacent to CJK or containing CJK
    def _replace_parens(match: re.Match[str]) -> str:
        inner = match.group(1)
        start, end = match.span()
        left = chunk[start - 1] if start > 0 else ""
        right = chunk[end] if end < len(chunk) else ""
        if (
            _has_cjk(inner)
            or (left and left in _CJK_OR_PUNCT_CLASS or _has_cjk(left))
            or (right and right in _CJK_OR_PUNCT_CLASS or _has_cjk(right))
        ):
            return f"（{inner}）"
        return match.group(0)

    # Run iteratively for nested or sequential pairs
    for _ in range(3):
        updated = _PAIRED_PARENS_RE.sub(_replace_parens, chunk)
        if updated == chunk:
            break
        chunk = updated

    # 4. Paired double quotes in Chinese context
    chunk = _PAIRED_QUOTES_RE.sub(r"“\1”", chunk)

    # 5. Halfwidth `, : ; ? !` adjacent to CJK or CJK punctuation,
    #    preserving numeric internals like `1,000` and `12:30`.
    def _conv_left(match: re.Match[str]) -> str:
        ch = match.group(1)
        idx = match.start(1)
        left = chunk[idx - 1] if idx > 0 else ""
        right = chunk[idx + 1] if idx + 1 < len(chunk) else ""
        if ch in {",", ":"} and left.isdigit() and right.isdigit():
            return ch
        return _PUNCT_MAP[ch]

    def _conv_right(match: re.Match[str]) -> str:
        ch = match.group(1)
        idx = match.start(1)
        left = chunk[idx - 1] if idx > 0 else ""
        right = chunk[idx + 1] if idx + 1 < len(chunk) else ""
        if ch in {",", ":"} and left.isdigit() and right.isdigit():
            return ch
        return _PUNCT_MAP[ch]

    for _ in range(3):
        prev = chunk
        chunk = _PUNCT_LEFT_RE.sub(_conv_left, chunk)
        chunk = _PUNCT_RIGHT_RE.sub(_conv_right, chunk)
        if chunk == prev:
            break

    # Remove redundant spaces right after newly converted fullwidth punctuation
    # when followed by CJK/digits/Latin (e.g. "3.5, 价格" -> "3.5，价格")
    chunk = _SPACE_AFTER_ZH_PUNCT_RE.sub(r"\1", chunk)
    return chunk


def normalize_zh(text: str, *, fullwidth_tilde: bool = True) -> str:
    """Normalize halfwidth punctuation in Chinese prose while preserving numbers and URLs."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text))
    parts = _URL_SPLIT_RE.split(text)
    out: list[str] = []
    for index, part in enumerate(parts):
        if index % 2 == 1:
            # Exact URL segment: keep untouched
            out.append(part)
        else:
            out.append(_normalize_chunk(part, fullwidth_tilde=fullwidth_tilde))
    return "".join(out)


# Non-breaking span patterns (ordered longest/most-specific first):
# - Numeric ranges or numbers + units / Chinese quantifiers (`1700 万行`, `98%～99%`, `98%`, `25 万页`, `100ms`)
# - Short Latin brand/model + optional version (`Claude 4.5`, `GPT-4o`, `Lean`)
_NB_PATTERN = re.compile(
    r"("
    r"\d+(?:[.,]\d+)?%?(?:\s*[～~\-–]\s*\d+(?:[.,]\d+)?%?)?"
    r"(?:\s*(?:万[个行页次家条篇款倍美元%]?|亿[个行页次家条篇款倍美元%]?|千[个行页次条]?|%|％|ms|s|秒|分钟|小时|天|个月|年|倍|元|美元|k|K|M|B|GB|TB|tokens?))?"
    r"|"
    r"[A-Za-z][A-Za-z0-9._+\-]{0,20}(?:\s+\d+(?:\.\d+)*[A-Za-z]?)?"
    r")"
)
_CJK_PLAIN_RE = re.compile(f"[{_CJK_CLASS}]")


def cjk_latin_tokens(text: str) -> list[tuple[str, str]]:
    """Split text into typed spans for HTML non-breaking and CJK-Latin spacing.

    Returns a list of ``(segment, kind)`` pairs where ``kind`` is one of:
    - ``"text"``: regular text
    - ``"nb"``: non-breaking numeric/unit or short Latin token
    - ``"nb_gap_l"`` / ``"nb_gap_r"`` / ``"nb_gap_lr"``: non-breaking token with
      tight CJK-Latin boundary spacing on left/right/both sides.
    """
    if not text:
        return []
    tokens: list[tuple[str, str]] = []
    pos = 0
    for match in _NB_PATTERN.finditer(text):
        start, end = match.span()
        if start > pos:
            tokens.append((text[pos:start], "text"))
        seg = match.group(0)
        # Only mark short segments as non-breaking to avoid runaway overflow
        if len(seg) <= 24 and re.search(r"[A-Za-z0-9]", seg):
            left_ch = text[start - 1] if start > 0 else ""
            right_ch = text[end] if end < len(text) else ""
            gap_l = bool(left_ch and _CJK_PLAIN_RE.match(left_ch))
            gap_r = bool(right_ch and _CJK_PLAIN_RE.match(right_ch))
            if gap_l and gap_r:
                kind = "nb_gap_lr"
            elif gap_l:
                kind = "nb_gap_l"
            elif gap_r:
                kind = "nb_gap_r"
            else:
                kind = "nb"
            tokens.append((seg, kind))
        else:
            tokens.append((seg, "text"))
        pos = end
    if pos < len(text):
        tokens.append((text[pos:], "text"))
    return tokens


def render_inline_html(text: str, *, emphasis: list[str] | None = None) -> str:
    """Escape text for HTML, apply `<mark>` for valid emphasis substrings, and wrap `.nb` spans."""
    raw = str(text or "")
    if not raw:
        return ""
    valid_emph = [item for item in (emphasis or []) if item and item in raw][:2]
    if not valid_emph:
        return _tokens_to_html(cjk_latin_tokens(raw))

    # Find non-overlapping emphasis intervals
    intervals: list[tuple[int, int]] = []
    for item in valid_emph:
        idx = raw.find(item)
        if idx >= 0:
            span = (idx, idx + len(item))
            if not any(not (span[1] <= a or span[0] >= b) for a, b in intervals):
                intervals.append(span)
    intervals.sort()
    if not intervals:
        return _tokens_to_html(cjk_latin_tokens(raw))

    parts: list[str] = []
    cursor = 0
    for a, b in intervals:
        if a > cursor:
            parts.append(_tokens_to_html(cjk_latin_tokens(raw[cursor:a])))
        parts.append(f"<mark>{_tokens_to_html(cjk_latin_tokens(raw[a:b]))}</mark>")
        cursor = b
    if cursor < len(raw):
        parts.append(_tokens_to_html(cjk_latin_tokens(raw[cursor:])))
    return "".join(parts)


def _tokens_to_html(tokens: list[tuple[str, str]]) -> str:
    out: list[str] = []
    for seg, kind in tokens:
        escaped = html.escape(seg, quote=True)
        if kind == "text":
            out.append(escaped)
        elif kind == "nb":
            out.append(f'<span class="nb">{escaped}</span>')
        elif kind == "nb_gap_l":
            out.append(f'<span class="nb gap-l">{escaped}</span>')
        elif kind == "nb_gap_r":
            out.append(f'<span class="nb gap-r">{escaped}</span>')
        elif kind == "nb_gap_lr":
            out.append(f'<span class="nb gap-l gap-r">{escaped}</span>')
    return "".join(out)

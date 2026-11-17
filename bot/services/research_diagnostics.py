"""Persist sanitized S1 research diagnostics.

The research pipeline needs to explain *why* a topic produced no evidence.  It
must not turn ``topic_requests`` into a dump of raw search responses or article
bodies, so diagnostics live in two small append-only tables:

* ``research_attempts`` - one row per search/fetch round, with counts and a
  sanitized error/explanation;
* ``research_candidates`` - one short row per candidate and stage decision.

Article bodies are never stored here.  Evidence continues to live in the
existing ``raw_items`` / ``evidence`` tables after a candidate is accepted.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit, unquote

from ..db import Database, json_dumps
from ..security import canonicalize_url, normalize_ws
from ..timeutil import Clock, to_iso

# Codes used by the S1 acceptance criteria.  Keep them stable and English;
# user-facing Chinese is produced by ``decision_label`` / ``build_receipt``.
DECISION_LABELS = {
    "direct_url": "直接链接",
    "search_result": "搜索候选",
    "url_unsafe": "URL 不安全",
    "domain_unverified": "域名未验证",
    "missing_title": "缺少标题",
    "fetch_timeout": "抓取超时",
    "fetch_http_error": "抓取 HTTP 错误",
    "fetch_empty": "抓取结果为空",
    "parse_failed": "正文解析失败",
    "body_low_quality": "正文过短",
    "topic_mismatch": "主题不匹配",
    "stale": "超出时效",
    "date_unknown": "发布时间未知",
    "budget_exhausted": "预算不足",
    "provider_unavailable": "检索服务不可用",
    "fetch_limit": "抓取次数上限",
    "search_empty": "搜索为空",
    "cache_hit": "使用缓存正文",
    "accepted": "正文可用",
    "not_fetched": "未抓取",
}

_FAILURE_ORDER = (
    "fetch_timeout",
    "fetch_http_error",
    "fetch_empty",
    "body_low_quality",
    "topic_mismatch",
    "stale",
    "url_unsafe",
    "domain_unverified",
    "missing_title",
    "parse_failed",
    "provider_unavailable",
    "budget_exhausted",
    "fetch_limit",
    "search_empty",
)


def decision_label(code: str | None) -> str:
    text = str(code or "").strip()
    return DECISION_LABELS.get(text, text or "未知原因")


_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|cookie|authorization|secret)\b\s*[:=]\s*[^\s,;]+"
)
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_-]{24,}\b")


_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"']+")
# S1 review repair: default-deny.  Query values are diagnostics only; keep a
# deliberately tiny allowlist of non-identifying pagination/display keys and
# mask everything else.  This catches signed URL families (X-Amz-*,
# X-Goog-*, token, etc.) without having to enumerate every provider.
_SAFE_QUERY_KEYS = frozenset(
    {"id", "page", "p", "lang", "locale", "v", "version"}
)
_URL_QUERY_KEYS = frozenset(
    {"url", "u", "next", "redirect", "target", "continue", "return", "return_to"}
)


def _sanitize_plain(value: Any, *, limit: int) -> str:
    text = normalize_ws(str(value or ""))
    text = _BEARER_RE.sub("Bearer [redacted]", text)
    text = _SECRET_RE.sub(lambda m: f"{m.group(1)}=[redacted]", text)
    text = _LONG_TOKEN_RE.sub("[redacted]", text)
    return text[:limit]


def _redacted_url(value: Any, *, depth: int = 0) -> str:
    """Return a diagnostic-only URL with credential-like query values removed.

    The URL used for the actual request is untouched.  An irreversible
    fingerprint is stored separately for exact correlation.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if depth > 2:
        return _sanitize_plain(text, limit=1000)
    try:
        split = urlsplit(text)
        if split.scheme.lower() not in {"http", "https"} or not split.netloc:
            return _sanitize_plain(text, limit=1000)
        host = split.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = split.port
        netloc = f"{host}:{port}" if port is not None else host
        pairs: list[tuple[str, str]] = []
        for raw_key, raw_value in parse_qsl(
            split.query, keep_blank_values=True, strict_parsing=False
        ):
            key = str(raw_key)
            lowered = key.strip().lower()
            decoded = unquote(str(raw_value))
            if lowered in _URL_QUERY_KEYS and decoded.lower().startswith(
                ("http://", "https://")
            ):
                pairs.append((key, _redacted_url(decoded, depth=depth + 1)))
            elif lowered in _SAFE_QUERY_KEYS and len(decoded) <= 120:
                pairs.append((key, decoded))
            else:
                pairs.append((key, "[redacted]"))
        query = urlencode(pairs, doseq=True, quote_via=quote)
        return urlunsplit(
            (split.scheme.lower(), netloc.lower(), split.path or "/", query, "")
        )[:1000]
    except (TypeError, ValueError, UnicodeError):
        return _sanitize_plain(text, limit=1000)


def _redact_urls_in_text(value: Any, *, limit: int) -> str:
    text = normalize_ws(str(value or ""))
    text = _URL_IN_TEXT_RE.sub(lambda match: _redacted_url(match.group(0)), text)
    return _sanitize_plain(text, limit=limit)


def sanitize_text(value: Any, *, limit: int = 300) -> str:
    """Redact credential-like fragments and bound a diagnostic string."""
    return _redact_urls_in_text(value, limit=limit)


def canonical_diagnostic_url(url: str) -> str:
    try:
        return canonicalize_url(_redacted_url(url))[:1000]
    except Exception:
        return ""


def url_fingerprint(url: str) -> str:
    """One-way identifier for the exact request URL (including query values)."""
    try:
        exact = canonicalize_url(str(url or "")) or str(url or "")
    except Exception:
        exact = str(url or "")
    return hashlib.sha256(exact.encode("utf-8")).hexdigest()


def body_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _safe_body_hash(value: Any) -> str:
    """Persist a real content hash, never a redacted 24+ char token.

    The generic diagnostic sanitizer intentionally redacts long opaque
    strings.  A SHA-256 is not a credential, so accept exactly the 64-char
    lowercase hex form produced by :func:`body_sha256`.
    """
    text = str(value or "").strip().lower()
    return text if re.fullmatch(r"[0-9a-f]{64}", text) else ""


def body_length(content: str) -> int:
    return len(normalize_ws(content))


def new_attempt(
    db: Database,
    clock: Clock,
    *,
    topic_request_id: int | None,
    query: str,
    provider: str,
) -> int:
    now = to_iso(clock.now())
    with db.tx() as conn:
        cur = conn.execute(
            """
            INSERT INTO research_attempts
                (topic_request_id, query, provider, status, started_at, created_at)
            VALUES (?, ?, ?, 'running', ?, ?)
            """,
            (topic_request_id, sanitize_text(query, limit=300), provider, now, now),
        )
        return int(cur.lastrowid)


def add_candidate(
    db: Database,
    clock: Clock,
    attempt_id: int,
    *,
    ordinal: int,
    title: str = "",
    url: str = "",
    domain: str = "",
    domain_verified: bool = False,
    stage: str = "search_result",
    decision_code: str = "",
    fetch_status: int | None = None,
    body_len: int | None = None,
    body_hash: str = "",
    latency_ms: int | None = None,
    detail: dict | None = None,
    date_status: str = "",
) -> int:
    canonical = canonical_diagnostic_url(url)
    fingerprint = url_fingerprint(url) if url else ""
    with db.tx() as conn:
        cur = conn.execute(
            """
            INSERT INTO research_candidates
                (attempt_id, ordinal, title, url, canonical_url, domain,
                 domain_verified, stage, decision_code, fetch_status,
                 body_length, body_sha256, latency_ms, detail_json, created_at,
                 url_fingerprint, citable_as_official, date_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(attempt_id),
                int(ordinal),
                sanitize_text(title, limit=300),
                canonical,
                canonical,
                sanitize_text(domain, limit=255),
                1 if domain_verified else 0,
                sanitize_text(stage, limit=80) or "search_result",
                sanitize_text(decision_code, limit=80),
                fetch_status,
                body_len,
                _safe_body_hash(body_hash),
                latency_ms,
                json_dumps(_clean_detail(detail)),
                to_iso(clock.now()),
                fingerprint,
                1 if domain_verified else 0,
                sanitize_text(date_status, limit=40),
            ),
        )
        return int(cur.lastrowid)


def update_candidate(db: Database, candidate_id: int, **fields: Any) -> None:
    allowed = {
        "stage",
        "decision_code",
        "fetch_status",
        "body_length",
        "body_sha256",
        "latency_ms",
        "detail_json",
        "domain_verified",
        "url_fingerprint",
        "citable_as_official",
        "date_status",
    }
    values: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "detail_json":
            values[key] = json_dumps(_clean_detail(value))
        elif key in {"domain_verified", "citable_as_official"}:
            values[key] = 1 if value else 0
        elif key in {"stage", "decision_code", "date_status"}:
            values[key] = sanitize_text(value, limit=80)
        elif key == "body_sha256":
            values[key] = _safe_body_hash(value)
        elif key == "url_fingerprint":
            text = str(value or "").strip().lower()
            values[key] = text if re.fullmatch(r"[0-9a-f]{64}", text) else ""
        else:
            values[key] = value
    if not values:
        return
    columns = ", ".join(f"{key}=?" for key in values)
    with db.tx() as conn:
        conn.execute(
            f"UPDATE research_candidates SET {columns} WHERE id=?",
            (*values.values(), int(candidate_id)),
        )


def finish_attempt(
    db: Database,
    clock: Clock,
    attempt_id: int,
    *,
    status: str,
    counts: dict,
    diagnostics: dict | None = None,
    error: str = "",
) -> None:
    with db.tx() as conn:
        conn.execute(
            """
            UPDATE research_attempts
               SET status=?, counts_json=?, diagnostics_json=?,
                   error=?, finished_at=?
             WHERE id=? AND status='running'
            """,
            (
                sanitize_text(status, limit=40) or "finished",
                json_dumps(counts),
                json_dumps(_clean_detail(diagnostics)),
                sanitize_text(error, limit=500),
                to_iso(clock.now()),
                int(attempt_id),
            ),
        )


def _clean_detail(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    clean: dict[str, Any] = {}
    for key, item in value.items():
        name = str(key)[:80]
        if isinstance(item, (int, float, bool)) or item is None:
            clean[name] = item
        elif isinstance(item, (list, tuple)):
            clean[name] = [sanitize_text(entry, limit=160) for entry in item[:20]]
        elif isinstance(item, dict):
            clean[name] = {
                str(k)[:60]: (
                    sanitize_text(v, limit=160)
                    if isinstance(v, str)
                    else v
                )
                for k, v in list(item.items())[:20]
                if isinstance(v, (str, int, float, bool)) or v is None
            }
        else:
            clean[name] = sanitize_text(item, limit=300)
    return clean


def failure_counts(counts: dict) -> dict[str, int]:
    raw = counts.get("failures")
    if not isinstance(raw, dict):
        return {}
    result: dict[str, int] = {}
    for key, value in raw.items():
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            result[sanitize_text(key, limit=60)] = number
    return result


def build_receipt(counts: dict, diagnostics: dict | None = None) -> str:
    """Build a Chinese receipt using only recorded counters.

    No model is involved.  Every number here comes from the attempt/candidate
    records, so a second-round failure cites the second-round stats.
    """
    counts = counts if isinstance(counts, dict) else {}
    raw = int(counts.get("raw_results") or 0)
    url_safe = int(counts.get("url_safe") or 0)
    verified = int(counts.get("domain_verified") or 0)
    fetches = int(counts.get("physical_fetches") or 0)
    usable = int(counts.get("usable_bodies") or 0)
    topic = int(counts.get("topic_matches") or 0)
    fresh = int(counts.get("freshness_passed") or 0)
    cache_hits = int(counts.get("cache_hits") or 0)
    direct = bool((diagnostics or {}).get("direct_url_present"))
    parts = []
    if direct:
        direct_code = str((diagnostics or {}).get("direct_url_code") or "")
        parts.append(f"直接链接：{decision_label(direct_code) if direct_code else '已尝试'}")
    parts.append(f"搜索发现 {raw} 条")
    parts.append(f"{url_safe} 条通过 URL 安全检查")
    parts.append(f"{verified} 条命中配置来源（仅参考）")
    parts.append(f"实际抓取 {fetches} 条")
    failures = failure_counts(counts)
    if failures:
        bits = [
            f"{failures[code]} 条{decision_label(code)}"
            for code in _FAILURE_ORDER
            if failures.get(code)
        ]
        for code in sorted(failures):
            if code not in _FAILURE_ORDER:
                bits.append(f"{failures[code]} 条{decision_label(code)}")
        parts.append("失败分布：" + "、".join(bits))
    if cache_hits:
        parts.append(f"复用缓存 {cache_hits} 条")
    parts.append(
        f"可用正文 {usable} 条，主题匹配 {topic} 条，时效通过 {fresh} 条"
    )
    if usable <= 0:
        parts.append("本次尚无可引用正文")
    return "；".join(parts) + "。"


def get_attempt(db: Database, attempt_id: int) -> sqlite3.Row | None:
    return db.fetchone("SELECT * FROM research_attempts WHERE id=?", (int(attempt_id),))


def attempt_candidates(db: Database, attempt_id: int) -> list[sqlite3.Row]:
    return db.fetchall(
        "SELECT * FROM research_candidates WHERE attempt_id=? ORDER BY ordinal, id",
        (int(attempt_id),),
    )


def parse_json_field(row: sqlite3.Row | None, field: str) -> dict:
    if row is None:
        return {}
    from ..db import json_loads

    try:
        value = json_loads(row[field]) if row[field] else {}
    except (KeyError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def research_summary(db: Database, attempt_id: int) -> dict:
    """Small query used by tests/CLI without exposing stored bodies."""
    row = get_attempt(db, attempt_id)
    if row is None:
        return {}
    return {
        "id": int(row["id"]),
        "status": row["status"],
        "counts": parse_json_field(row, "counts_json"),
        "diagnostics": parse_json_field(row, "diagnostics_json"),
        "error": row["error"] or "",
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "candidates": [dict(item) for item in attempt_candidates(db, attempt_id)],
    }


# Keep datetime imported for callers that type-check monkeypatched clocks.
_ = datetime

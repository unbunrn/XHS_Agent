"""S1 bounded topic research: direct link first, then ranked search candidates.

This module owns *one* research round.  It deliberately knows nothing about
Feishu, drafts or publishing; ``topics.py`` remains the lifecycle orchestrator.
The adapter is responsible only for transport/SSRF checks and raw candidates.

The important safety properties are:

* a direct public URL is fetched without requiring search-index inclusion;
* only content matching the requested topic and freshness window can become
  evidence;
* provider summaries are never promoted to bodies;
* at most six physical fetch attempts are made (including retries);
* every candidate decision is persisted in sanitized diagnostic tables.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from ..adapters.sources import FetchLimit, PhysicalFetchQuota, host_matches_allowed_domains
from ..adapters.types import (
    BudgetExceeded,
    SourceEntry,
    SourceError,
)
from ..db import Database, json_dumps, json_loads
from ..security import canonicalize_url, normalize_ws
from ..timeutil import Clock, to_iso
from . import research_diagnostics as diagnostics_store


@dataclass
class ResearchOutcome:
    entries: list[SourceEntry]
    attempt_id: int
    counts: dict[str, Any]
    diagnostics: dict[str, Any]
    receipt: str
    round_label: str = "first"
    status: str = "no_evidence"
    candidates: list[SourceEntry] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


def _host(url: str) -> str:
    try:
        return (urlsplit(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _has_http_url(text: str) -> bool:
    return bool(re.search(r"https?://[^\s<>\"')\]]+", str(text or ""), re.I))


def _keywords(value: Iterable[str] | None) -> list[str]:
    result = []
    for item in value or ():
        text = normalize_ws(str(item or "")).lower()
        if text and text not in result:
            result.append(text)
    return result


def _lexically_relevant(title: str, url: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    haystack = f"{title} {url}".lower()
    return any(keyword in haystack for keyword in keywords)


def _priority(candidate: dict, keywords: list[str], direct_url: str | None) -> tuple:
    url = str(candidate.get("url") or "")
    title = str(candidate.get("title") or "").lower()
    path = ""
    try:
        path = (urlsplit(url).path or "").lower()
    except ValueError:
        path = ""
    exact_direct = bool(direct_url) and candidate.get("canonical_url") == canonicalize_url(direct_url)
    exact_entity = sum(1 for keyword in keywords if keyword and keyword in title)
    release_path = 1 if any(
        token in path for token in ("/release", "/news", "/blog", "/announce", "/article")
    ) else 0
    bad_path = 1 if any(
        token in path for token in ("/login", "/signin", "/search", "/list", "/category")
    ) else 0
    home_like = 1 if path in {"", "/", "/index.html"} else 0
    return (
        0 if exact_direct else 1,
        bad_path,
        home_like,
        -release_path,
        -exact_entity,
        int(candidate.get("ordinal") or 0),
    )


def _cache_lookup(
    db: Database,
    *,
    canonical_url: str,
    cutoff: str,
) -> dict | None:
    if not canonical_url:
        return None
    row = db.fetchone(
        """
        SELECT r.title, r.content, r.published_at, r.fetched_at,
               COALESCE(s.name, '') AS source_name
          FROM raw_items r
          JOIN sources s ON s.id = r.source_id
         WHERE r.canonical_url=? AND r.content IS NOT NULL AND r.content!=''
           AND r.fetched_at>=? AND s.enabled=1
         ORDER BY r.fetched_at DESC
         LIMIT 1
        """,
        (canonical_url, cutoff),
    )
    if row is None:
        return None
    return {
        "title": row["title"] or "",
        "content": row["content"] or "",
        "published_at": row["published_at"] or "",
        "fetched_at": row["fetched_at"] or "",
        "source_name": row["source_name"] or "",
    }




def _research_mode(request) -> str:
    try:
        return str(request["research_mode"] or "news")
    except (KeyError, IndexError, TypeError):
        return "news"


def _cache_key(
    *,
    request,
    query: str,
    trusted: Iterable[str],
    verified: Iterable[str],
    max_age_hours: float,
    direct_url: str | None,
    discovery_mode: str = "recent",
    config,
) -> str:
    """Stable, non-secret cache key for one research round.

    The key deliberately includes the trust/policy configuration and the
    normalized topic, so a config change or a different source scope cannot
    silently reuse a cached verdict.  Query values inside URLs are not stored
    here; the query is already the provider query, never a credential.
    """
    material = {
        "schema": "research-cache/v1",
        "mode": _research_mode(request),
        "topic": normalize_ws(str((request or {})["topic"] if request is not None else "")),
        "query": normalize_ws(query),
        "trusted": sorted({str(x).lower() for x in trusted if x}),
        "verified": sorted({str(x).lower() for x in verified if x}),
        "max_age_hours": round(float(max_age_hours), 4),
        "direct": canonicalize_url(direct_url) if direct_url else "",
        "discovery_mode": "any" if str(discovery_mode or "recent") == "any" else "recent",
        "max_results": int(config.get("topics.search_max_results", 5)),
        "min_usable": int(config.get("topics.research_min_usable", 1)),
        "min_body_chars": int(config.get("topics.research_min_body_chars", 0)),
        "max_fetch": int(config.get("topics.research_max_fetch_attempts", 6)),
    }
    return hashlib.sha256(json_dumps(material).encode("utf-8")).hexdigest()


def _cache_get(db: Database, *, cache_key: str, now_iso: str) -> dict | None:
    row = db.fetchone(
        "SELECT entries_json, counts_json, diagnostics_json FROM research_cache "
        "WHERE cache_key=? AND expires_at>?",
        (cache_key, now_iso),
    )
    return dict(row) if row is not None else None


def _cache_put(
    db: Database,
    *,
    cache_key: str,
    request,
    query: str,
    mode: str,
    policy_key: str,
    entries: list[SourceEntry],
    counts: dict,
    diagnostics: dict,
    now_iso: str,
    expires_at: str,
) -> None:
    payload = [
        {
            "title": entry.title,
            "url": entry.url,
            "content": entry.content,
            "published_raw": entry.published_raw,
        }
        for entry in entries
    ]
    if not payload:
        return
    with db.tx() as conn:
        conn.execute(
            """
            INSERT INTO research_cache
                (cache_key, topic, query, research_mode, policy_key,
                 entries_json, counts_json, diagnostics_json, created_at, expires_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(cache_key) DO UPDATE SET
                entries_json=excluded.entries_json,
                counts_json=excluded.counts_json,
                diagnostics_json=excluded.diagnostics_json,
                created_at=excluded.created_at,
                expires_at=excluded.expires_at
            """,
            (
                cache_key,
                normalize_ws(str((request or {})["topic"] if request is not None else ""))[:500],
                normalize_ws(query)[:500],
                mode,
                policy_key,
                json_dumps(payload),
                json_dumps(counts),
                json_dumps(diagnostics),
                now_iso,
                expires_at,
            ),
        )


def _failure(counts: dict, code: str) -> None:
    failures = counts.setdefault("failures", {})
    failures[code] = int(failures.get(code) or 0) + 1


def _fetch_error_code(exc: SourceError) -> str:
    if isinstance(exc, FetchLimit):
        return "fetch_limit"
    if str(getattr(exc, "code", "")) == "timeout":
        return "fetch_timeout"
    if getattr(exc, "status", None) == 429:
        return "fetch_http_error"
    text = str(exc).lower()
    if "timed out" in text or "timeout" in text:
        return "fetch_timeout"
    return "fetch_http_error"


def _new_entry(
    *,
    title: str,
    url: str,
    content: str,
    published_raw: str,
) -> SourceEntry:
    return SourceEntry(
        title=normalize_ws(title)[:300],
        url=url,
        summary="",
        content=normalize_ws(content)[:20_000],
        published_raw=str(published_raw or "").strip(),
    )


def run_topic_research(
    db: Database,
    config,
    clock: Clock,
    adapter,
    request,
    *,
    direct_url: str | None,
    trusted_domains: Iterable[str],
    keywords: Iterable[str],
    max_age_hours: float,
    topic_check: Callable[[SourceEntry], bool],
    freshness_check: Callable[[SourceEntry], tuple[bool, str]],
    allow_unverified_search: bool = True,
    round_label: str = "first",
    verification_domains: Iterable[str] | None = None,
    fetch_quota: PhysicalFetchQuota | None = None,
    refresh: bool = False,
    discovery_mode: str | None = None,
) -> ResearchOutcome:
    """Run one bounded research round and persist its sanitized diagnostics."""
    trusted = tuple(str(item or "").strip().lower() for item in trusted_domains if item)
    verified = tuple(
        str(item or "").strip().lower()
        for item in (verification_domains if verification_domains is not None else trusted_domains)
        if item
    )
    keyword_list = _keywords(keywords)
    max_fetch_attempts = max(
        1, min(6, int(config.get("topics.research_max_fetch_attempts", 6)))
    )
    cache_hours = max(
        0.0, float(config.get("topics.research_cache_hours", 6))
    )
    min_usable = max(1, int(config.get("topics.research_min_usable", 1)))
    min_body_chars = max(0, int(config.get("topics.research_min_body_chars", 0)))

    # A single quota object is shared by the direct link, the first search round
    # and any model-expanded second round.  The hard cap is six physical sends
    # including retries; config can only lower it.
    quota = fetch_quota or PhysicalFetchQuota(max_fetch_attempts)
    quota.limit = max(0, min(quota.limit, max_fetch_attempts, PhysicalFetchQuota.HARD_LIMIT))
    adapter.fetch_quota = quota
    adapter.max_fetch_attempts = quota.limit
    round_attempts_start = int(quota.attempts)
    round_retries_start = int(quota.retries)

    query = str(getattr(adapter, "query", "") or "")
    attempt_id = diagnostics_store.new_attempt(
        db,
        clock,
        topic_request_id=int(request["id"]) if request is not None else None,
        query=query,
        provider="deepseek-search",
    )

    counts: dict[str, Any] = {
        "raw_results": 0,
        "url_safe": 0,
        "domain_verified": 0,
        "physical_fetches": 0,
        "retries": 0,
        "task_physical_fetches": int(quota.attempts),
        "task_retries": int(quota.retries),
        "identity_unverified": 0,
        "logical_candidates": 0,
        "usable_bodies": 0,
        "topic_matches": 0,
        "freshness_passed": 0,
        "cache_hits": 0,
        "candidate_bodies": 0,
        "failures": {},
    }
    diagnostics: dict[str, Any] = {
        "schema": "research-diagnostics/s1",
        "provider": "deepseek-search",
        "round": round_label,
        "direct_url_present": bool(direct_url),
        "direct_url_code": "",
        "trusted_domains": list(trusted),
        "max_fetch_attempts": max_fetch_attempts,
        "cache_hours": cache_hours,
        "min_body_chars": min_body_chars,
        "search_query": query,
    }
    entries: list[SourceEntry] = []
    candidate_bodies: list[SourceEntry] = []
    candidate_canonical: set[str] = set()
    seen_canonical: set[str] = set()
    cutoff = to_iso(clock.now() - timedelta(hours=cache_hours))
    research_mode = _research_mode(request)
    discovery = "any" if str(discovery_mode or "recent") == "any" else "recent"
    diagnostics["discovery_mode"] = discovery
    cache_key = _cache_key(
        request=request,
        query=query,
        trusted=trusted,
        verified=verified,
        max_age_hours=max_age_hours,
        direct_url=direct_url,
        discovery_mode=discovery,
        config=config,
    )
    cache_now = clock.now()
    cache_now_iso = to_iso(cache_now)
    cache_expires_at = to_iso(cache_now + timedelta(hours=cache_hours))

    def finish(status: str, error: str = "") -> ResearchOutcome:
        counts["physical_fetches"] = max(
            0, int(quota.attempts) - round_attempts_start
        )
        counts["retries"] = max(0, int(quota.retries) - round_retries_start)
        counts["task_physical_fetches"] = int(quota.attempts)
        counts["task_retries"] = int(quota.retries)
        if status == "no_evidence" and (
            candidate_bodies or int(counts.get("identity_unverified") or 0) > 0
        ):
            status = "identity_unverified"
        diagnostics_store.finish_attempt(
            db,
            clock,
            attempt_id,
            status=status,
            counts=counts,
            diagnostics=diagnostics,
            error=error,
        )
        receipt = diagnostics_store.build_receipt(counts, diagnostics)
        if status == "identity_unverified":
            receipt += " 已取得候选正文，来源身份待核验，未写入官方证据。"
        if status == "completed" and entries and not candidate_bodies and cache_hours > 0:
            _cache_put(
                db,
                cache_key=cache_key,
                request=request,
                query=query,
                mode=research_mode,
                policy_key=cache_key,
                entries=entries,
                counts=counts,
                diagnostics=diagnostics,
                now_iso=cache_now_iso,
                expires_at=cache_expires_at,
            )
        return ResearchOutcome(
            entries=list(entries),
            attempt_id=attempt_id,
            counts=dict(counts),
            diagnostics=dict(diagnostics),
            receipt=receipt,
            round_label=round_label,
            status=status,
            candidates=list(candidate_bodies),
        )

    if not refresh and cache_hours > 0:
        cached = _cache_get(db, cache_key=cache_key, now_iso=cache_now_iso)
        if cached is not None:
            try:
                cached_entries = json_loads(cached.get("entries_json") or "[]")
            except Exception:
                cached_entries = []
            restored: list[SourceEntry] = []
            for item in cached_entries if isinstance(cached_entries, list) else []:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "")
                domain = _host(url)
                content = str(item.get("content") or "")
                if not content or not domain:
                    continue
                if not host_matches_allowed_domains(domain, verified):
                    continue
                entry = _new_entry(
                    title=str(item.get("title") or ""),
                    url=url,
                    content=content,
                    published_raw=str(item.get("published_raw") or ""),
                )
                fresh, fresh_code = freshness_check(entry)
                if not fresh:
                    continue
                entry.detail = {"date_status": fresh_code or "ok"}
                restored.append(entry)
            if restored:
                for ordinal, entry in enumerate(restored):
                    diagnostics_store.add_candidate(
                        db,
                        clock,
                        attempt_id,
                        ordinal=ordinal + 1,
                        title=entry.title,
                        url=entry.url,
                        domain=_host(entry.url),
                        domain_verified=True,
                        stage="cache_reuse",
                        decision_code="cache_hit",
                        body_len=len(entry.content),
                        body_hash=diagnostics_store.body_sha256(entry.content),
                        detail={"source": "research_cache", "ttl_hours": cache_hours},
                        date_status=getattr(entry, "detail", {}).get("date_status", "ok"),
                    )
                entries.extend(restored)
                counts["cache_hits"] = len(restored)
                counts["freshness_passed"] = len(restored)
                diagnostics["cache_reuse"] = True
                return finish("completed")
            # Expired/stale/trust-changed cache rows must not hide updates.
            with db.tx() as conn:
                conn.execute("DELETE FROM research_cache WHERE cache_key=?", (cache_key,))

    def try_candidate(
        candidate: dict,
        candidate_id: int,
        *,
        ordinal: int,
        stage: str,
        allow_fetch_unverified: bool = False,
        require_trusted: bool = False,
        defer_entry: bool = False,
        duplicate_entry: SourceEntry | None = None,
        allow_unverified_entry: bool = False,
        refresh: bool = False,
    ) -> bool | SourceEntry:
        """Evaluate one candidate.

        Normally returns True when an entry was accepted.  ``defer_entry`` lets
        the direct-URL path keep the fetched entry aside until the search round
        has had a chance to provide the real article title; it returns the
        SourceEntry instead of appending it.
        """
        nonlocal direct_entry
        url = str(candidate.get("url") or "").strip()
        title = normalize_ws(str(candidate.get("title") or ""))
        domain = _host(url)
        domain_verified = bool(domain) and host_matches_allowed_domains(domain, verified)
        canonical = canonicalize_url(url) if url else ""
        if canonical and canonical in seen_canonical:
            # The direct URL was fetched before search.  When the same URL
            # arrives from search, adopt the search title/published date but
            # keep the already-fetched body instead of fetching (or failing)
            # twice.
            if (
                duplicate_entry is not None
                and canonical == canonicalize_url(str(duplicate_entry.url or ""))
            ):
                duplicate_entry.title = title or duplicate_entry.title
                duplicate_entry.published_raw = str(
                    candidate.get("published_raw") or duplicate_entry.published_raw
                )
                fresh, fresh_code = freshness_check(duplicate_entry)
                if not fresh:
                    _failure(counts, fresh_code or "stale")
                    diagnostics_store.update_candidate(
                        db,
                        candidate_id,
                        stage="freshness",
                        decision_code=fresh_code or "stale",
                        date_status=fresh_code or "stale",
                    )
                    # A later search result explicitly marks the direct URL as
                    # stale (for example a reposted old page).  Never fall back
                    # to that deferred direct body after the ranked loop.
                    if (
                        direct_entry is not None
                        and canonical == canonicalize_url(str(direct_entry.url or ""))
                    ):
                        direct_entry = None
                    candidate_bodies[:] = [
                        item
                        for item in candidate_bodies
                        if canonicalize_url(str(item.url or "")) != canonical
                    ]
                    candidate_canonical.discard(canonical)
                    return False
                diagnostics_store.update_candidate(
                    db,
                    candidate_id,
                    stage="accepted",
                    decision_code="direct_duplicate_reused",
                    date_status=fresh_code or "ok",
                )
                if not domain_verified:
                    counts["identity_unverified"] = int(
                        counts.get("identity_unverified") or 0
                    ) + 1
                    diagnostics_store.update_candidate(
                        db,
                        candidate_id,
                        stage="accepted",
                        decision_code="domain_unverified",
                        domain_verified=False,
                        detail_json={"citable_as_official": False},
                    )
                    if not allow_unverified_entry:
                        return False
                    if canonical not in candidate_canonical:
                        candidate_canonical.add(canonical)
                        candidate_bodies.append(duplicate_entry)
                        counts["candidate_bodies"] = (
                            int(counts.get("candidate_bodies") or 0) + 1
                        )
                    return duplicate_entry
                entries.append(duplicate_entry)
                return True
            diagnostics_store.update_candidate(
                db,
                candidate_id,
                stage=stage,
                decision_code="duplicate_candidate",
            )
            return False
        if canonical:
            seen_canonical.add(canonical)

        if not title:
            _failure(counts, "missing_title")
            diagnostics_store.update_candidate(
                db, candidate_id, stage=stage, decision_code="missing_title"
            )
            return False
        if not bool(candidate.get("url_safe")):
            _failure(counts, "url_unsafe")
            diagnostics_store.update_candidate(
                db, candidate_id, stage=stage, decision_code="url_unsafe"
            )
            return False
        counts["url_safe"] = int(counts.get("url_safe") or 0) + 1
        if domain_verified:
            counts["domain_verified"] = int(counts.get("domain_verified") or 0) + 1
        if not domain_verified and not allow_unverified_search and not allow_fetch_unverified:
            _failure(counts, "domain_unverified")
            diagnostics_store.update_candidate(
                db,
                candidate_id,
                stage=stage,
                decision_code="domain_unverified",
                domain_verified=False,
            )
            return False
        # A directly supplied URL is a deliberate lead.  Its visible title may
        # be the owner's topic phrase rather than the page headline, so defer
        # the real topic check until after the bounded fetch.  This does not
        # bypass identity or evidence checks: the fetched body still has to
        # match the topic and pass source verification.
        direct_lead = stage == "direct_url"
        if not direct_lead and not _lexically_relevant(title, url, keyword_list) and not (
            domain_verified
            and allow_unverified_entry
            and _has_http_url(f"{title} {str(candidate.get('raw_content') or '')}")
        ):
            _failure(counts, "topic_mismatch")
            diagnostics_store.update_candidate(
                db,
                candidate_id,
                stage=stage,
                decision_code="topic_mismatch",
            )
            return False

        inline_content = str(candidate.get("raw_content") or "")
        # A provider may return an inline body.  When the source is configured
        # to fetch, prefer a physical fetch so HTTP 429/timeout/empty decisions
        # are real; keep the inline body only as a fallback when the fetch
        # returns no body.  Summaries are never copied into raw_content.
        wants_fetch = bool(getattr(adapter, "should_fetch_content", True))
        content = "" if wants_fetch else inline_content
        cache_used = False
        detail = {
            "domain_verified": domain_verified,
            "inline_content": bool(content),
            "cache_hit": False,
        }
        if not content and canonical and not refresh:
            cached = _cache_lookup(db, canonical_url=canonical, cutoff=cutoff)
            if cached:
                content = str(cached.get("content") or "")
                cache_used = True
                counts["cache_hits"] = int(counts.get("cache_hits") or 0) + 1
                detail["cache_hit"] = True
                detail["cache_fetched_at"] = cached.get("fetched_at") or ""
                detail["cache_source_name"] = cached.get("source_name") or ""
                diagnostics_store.update_candidate(
                    db,
                    candidate_id,
                    stage="cache",
                    decision_code="cache_hit",
                    body_length=len(normalize_ws(content)),
                    body_sha256=diagnostics_store.body_sha256(content),
                    detail_json=detail,
                )
        if not content:
            counts["logical_candidates"] = int(
                counts.get("logical_candidates") or 0
            ) + 1
            if quota.remaining <= 0:
                _failure(counts, "fetch_limit")
                diagnostics_store.update_candidate(
                    db,
                    candidate_id,
                    stage="fetch",
                    decision_code="fetch_limit",
                    detail_json=detail,
                )
                return False
            started = time.monotonic()
            try:
                fetched = adapter.fetch_content(url)
                content = str((fetched or {}).get("content") or "")
                latency = int((fetched or {}).get("latency_ms") or 0)
                http_status = int((fetched or {}).get("status") or 200)
                detail["http_status"] = http_status
                if not content and inline_content:
                    content = inline_content
                    detail["inline_fallback"] = True
            except FetchLimit as exc:
                # The transport refused before reserving/sending.  Never
                # decrement sent-attempt counters: they are transport truth.
                _failure(counts, "fetch_limit")
                diagnostics_store.update_candidate(
                    db,
                    candidate_id,
                    stage="fetch",
                    decision_code="fetch_limit",
                    detail_json={**detail, "error": diagnostics_store.sanitize_text(exc)},
                )
                return False
            except BudgetExceeded:
                _failure(counts, "budget_exhausted")
                diagnostics_store.update_candidate(
                    db,
                    candidate_id,
                    stage="fetch",
                    decision_code="budget_exhausted",
                    detail_json=detail,
                )
                return False
            except SourceError as exc:
                latency = int((time.monotonic() - started) * 1000)
                code = _fetch_error_code(exc)
                _failure(counts, code)
                diagnostics_store.update_candidate(
                    db,
                    candidate_id,
                    stage="fetch",
                    decision_code=code,
                    fetch_status=getattr(exc, "status", None),
                    latency_ms=latency,
                    detail_json={
                        **detail,
                        "error": diagnostics_store.sanitize_text(exc),
                    },
                )
                return False
            finally:
                counts["physical_fetches"] = max(
                    0, int(quota.attempts) - round_attempts_start
                )
                counts["retries"] = max(
                    0, int(quota.retries) - round_retries_start
                )
            diagnostics_store.update_candidate(
                db,
                candidate_id,
                stage="fetch",
                decision_code="",
                latency_ms=latency,
                fetch_status=http_status,
                body_length=len(normalize_ws(content)),
                body_sha256=diagnostics_store.body_sha256(content),
                detail_json=detail,
            )
        else:
            latency = 0
            diagnostics_store.update_candidate(
                db,
                candidate_id,
                stage="cache" if cache_used else stage,
                decision_code="cache_hit" if cache_used else "",
                body_length=len(normalize_ws(content)),
                body_sha256=diagnostics_store.body_sha256(content),
                detail_json=detail,
            )

        if not content:
            _failure(counts, "fetch_empty")
            diagnostics_store.update_candidate(
                db, candidate_id, stage="body", decision_code="fetch_empty"
            )
            return False
        counts["usable_bodies"] = int(counts.get("usable_bodies") or 0) + 1
        body_len = len(normalize_ws(content))
        if min_body_chars and body_len < min_body_chars:
            _failure(counts, "body_low_quality")
            diagnostics_store.update_candidate(
                db,
                candidate_id,
                stage="body",
                decision_code="body_low_quality",
                body_length=body_len,
            )
            return False

        entry = _new_entry(
            title=title,
            url=url,
            content=content,
            published_raw=str(candidate.get("published_raw") or ""),
        )
        if not topic_check(entry):
            _failure(counts, "topic_mismatch")
            diagnostics_store.update_candidate(
                db, candidate_id, stage="match", decision_code="topic_mismatch"
            )
            # A trusted page can be relationship material even when its own
            # text does not match the topic (for example a release index that
            # only links to the product page).  Keep the fetched body for the
            # single identity batch, but never treat it as evidence by itself.
            if domain_verified and allow_unverified_entry:
                canonical = canonicalize_url(url)
                if canonical and canonical not in candidate_canonical:
                    candidate_canonical.add(canonical)
                    candidate_bodies.append(entry)
                    counts["candidate_bodies"] = (
                        int(counts.get("candidate_bodies") or 0) + 1
                    )
            return False
        counts["topic_matches"] = int(counts.get("topic_matches") or 0) + 1
        fresh, fresh_code = freshness_check(entry)
        if not fresh:
            _failure(counts, fresh_code or "stale")
            diagnostics_store.update_candidate(
                db,
                candidate_id,
                stage="freshness",
                decision_code=fresh_code or "stale",
                date_status=fresh_code or "stale",
            )
            return False
        counts["freshness_passed"] = int(counts.get("freshness_passed") or 0) + 1
        if not domain_verified:
            counts["identity_unverified"] = int(
                counts.get("identity_unverified") or 0
            ) + 1
            diagnostics_store.update_candidate(
                db,
                candidate_id,
                stage="accepted",
                decision_code="domain_unverified",
                domain_verified=False,
                date_status=fresh_code or "ok",
                detail_json={**detail, "citable_as_official": False},
            )
            if allow_unverified_entry:
                canonical = canonicalize_url(url)
                if canonical and canonical not in candidate_canonical:
                    candidate_canonical.add(canonical)
                    candidate_bodies.append(entry)
                    counts["candidate_bodies"] = (
                        int(counts.get("candidate_bodies") or 0) + 1
                    )
                return entry
            return False
        diagnostics_store.update_candidate(
            db,
            candidate_id,
            stage="accepted",
            decision_code="accepted",
            date_status=fresh_code or "ok",
        )
        if defer_entry:
            return entry
        entries.append(entry)
        return True

    # 1) Supplied URL first.  A safe public URL is validated and fetched
    # directly even if the search index does not know it.  Only a base trusted
    # domain can become official evidence; an unverified domain is fetched as
    # candidate material and reported as identity-unverified, never cited.
    direct_entry: SourceEntry | None = None
    unverified_direct_entry: SourceEntry | None = None
    if direct_url:
        request_topic = ""
        try:
            request_topic = str(request["topic"] or "")
        except (KeyError, TypeError, IndexError):
            request_topic = ""
        safe, safe_code = adapter.safe_url(direct_url)
        diagnostics["direct_url_code"] = safe_code
        domain = _host(direct_url)
        domain_verified = bool(domain) and host_matches_allowed_domains(domain, verified)
        candidate = {
            "ordinal": 0,
            "title": normalize_ws(request_topic),
            "url": direct_url,
            "canonical_url": canonicalize_url(direct_url),
            "domain": domain,
            "domain_verified": domain_verified,
            "url_safe": safe,
            "url_code": safe_code,
            "published_raw": "",
            "raw_content": "",
            "summary_present": False,
        }
        candidate_id = diagnostics_store.add_candidate(
            db,
            clock,
            attempt_id,
            ordinal=0,
            title=candidate["title"],
            url=direct_url,
            domain=domain,
            domain_verified=domain_verified,
            stage="direct_url",
            decision_code=(
                "domain_unverified"
                if safe and not domain_verified
                else (safe_code if not safe else "direct_url")
            ),
            detail={"source": "supplied_url", "safe": safe},
        )
        if not safe:
            _failure(counts, "url_unsafe")
        elif not domain_verified:
            accepted_direct = try_candidate(
                candidate,
                candidate_id,
                ordinal=0,
                stage="direct_url",
                allow_fetch_unverified=True,
                require_trusted=False,
                defer_entry=True,
                allow_unverified_entry=True,
                refresh=refresh,
            )
            if isinstance(accepted_direct, SourceEntry):
                unverified_direct_entry = accepted_direct
            else:
                seen_canonical.discard(canonicalize_url(direct_url))
        else:
            accepted_direct = try_candidate(
                candidate,
                candidate_id,
                ordinal=0,
                stage="direct_url",
                allow_fetch_unverified=False,
                require_trusted=True,
                defer_entry=True,
                refresh=refresh,
            )
            if isinstance(accepted_direct, SourceEntry):
                direct_entry = accepted_direct
            else:
                # The direct fetch produced no body (for example the site
                # served a shell page).  Do not reserve the canonical URL:
                # search may still supply a usable body for the same article.
                seen_canonical.discard(canonicalize_url(direct_url))

    # 2) Search candidates.  Search performs no article fetches; candidates are
    # ranked and only then handed to the bounded fetch loop.
    try:
        search_results, search_diagnostics = adapter.search_candidates()
    except BudgetExceeded:
        diagnostics["search_error"] = "budget_exhausted"
        _failure(counts, "budget_exhausted")
        if direct_entry is not None:
            entries.append(direct_entry)
            return finish("completed")
        if unverified_direct_entry is not None:
            return finish("identity_unverified", "budget exhausted after candidate fetch")
        return finish("deferred", "budget exhausted before search")
    except SourceError as exc:
        diagnostics["search_error"] = diagnostics_store.sanitize_text(exc)
        _failure(counts, "provider_unavailable")
        if direct_entry is not None:
            entries.append(direct_entry)
            return finish("completed")
        if unverified_direct_entry is not None:
            return finish("identity_unverified", str(exc))
        return finish("failed", str(exc))
    diagnostics["search"] = search_diagnostics
    counts["raw_results"] = int(
        (search_diagnostics.get("counts") or {}).get("raw_results")
        or search_diagnostics.get("raw_count")
        or 0
    )
    if not search_results:
        _failure(counts, "search_empty")
        if direct_entry is not None:
            entries.append(direct_entry)
            return finish("completed")
        if unverified_direct_entry is not None:
            return finish("identity_unverified")
        return finish("no_evidence")

    rows: list[tuple[dict, int]] = []
    for candidate in search_results:
        candidate = dict(candidate)
        title = normalize_ws(str(candidate.get("title") or ""))
        domain = str(candidate.get("domain") or _host(str(candidate.get("url") or "")))
        # Search may use model-expanded domains for recall, but source
        # identity is derived only from the base trusted domains.
        domain_verified = bool(domain) and host_matches_allowed_domains(domain, verified)
        candidate["domain_verified"] = domain_verified
        code = ""
        if not candidate.get("url_safe"):
            code = "url_unsafe"
        elif not title:
            code = "missing_title"
        candidate_id = diagnostics_store.add_candidate(
            db,
            clock,
            attempt_id,
            ordinal=int(candidate.get("ordinal") or 0) + 1,
            title=title,
            url=str(candidate.get("url") or ""),
            domain=domain,
            domain_verified=domain_verified,
            stage="search_result",
            decision_code=code,
            detail={
                "source": "search",
                "summary_present": bool(candidate.get("summary_present")),
            },
        )
        candidate["_candidate_id"] = candidate_id
        rows.append((candidate, candidate_id))

    direct_canonical = canonicalize_url(direct_url) if direct_url else ""
    if direct_entry is not None and direct_canonical:
        # The search result carries the real article title; reuse the direct
        # fetch body instead of paying for the same URL twice.
        for candidate, _candidate_id in rows:
            if str(candidate.get("canonical_url") or "") == direct_canonical:
                candidate["raw_content"] = str(direct_entry.content or "")
    ranked = sorted(
        rows,
        key=lambda pair: _priority(pair[0], keyword_list, direct_url),
    )
    processed: set[int] = set()
    for candidate, candidate_id in ranked:
        # Do not stop at the first trusted entry: a new vendor/product page may
        # still be an unverified candidate that needs the one identity batch.
        # The shared physical-fetch quota remains the hard stop.
        processed.add(candidate_id)
        if not candidate.get("url_safe"):
            _failure(counts, "url_unsafe")
            diagnostics_store.update_candidate(
                db, candidate_id, stage="search_result", decision_code="url_unsafe"
            )
            continue
        if not str(candidate.get("title") or "").strip():
            _failure(counts, "missing_title")
            diagnostics_store.update_candidate(
                db, candidate_id, stage="search_result", decision_code="missing_title"
            )
            continue
        try_candidate(
            candidate,
            candidate_id,
            ordinal=int(candidate.get("ordinal") or 0) + 1,
            stage="search_result",
            duplicate_entry=direct_entry or unverified_direct_entry,
            allow_unverified_entry=True,
            refresh=refresh,
        )
        if int(counts.get("failures", {}).get("budget_exhausted", 0) or 0) > 0:
            break
        if quota.remaining <= 0:
            _failure(counts, "fetch_limit")
            break

    for candidate, candidate_id in rows:
        if candidate_id in processed:
            continue
        diagnostics_store.update_candidate(
            db,
            candidate_id,
            stage="search_result",
            decision_code="not_fetched",
            detail_json={
                "reason": "research stopped at the physical fetch limit",
            },
        )

    if direct_entry is not None and direct_canonical:
        if not any(
            canonicalize_url(str(entry.url or "")) == direct_canonical
            for entry in entries
        ):
            entries.insert(0, direct_entry)
    if (
        not entries
        and int(counts.get("failures", {}).get("budget_exhausted", 0) or 0) > 0
    ):
        return finish("deferred", "budget exhausted during candidate research")
    status = "completed" if entries else ("identity_unverified" if candidate_bodies else "no_evidence")
    return finish(status)


# Keep the import used by tests that inspect canonical URL helpers.
_ = (json_dumps,)

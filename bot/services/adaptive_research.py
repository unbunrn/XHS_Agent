"""Bounded adaptive research coverage checks.

The first plan is intentionally small (three queries).  This module looks at
what that plan actually found, names the remaining coverage gaps, and proposes
at most two follow-up queries.  It never turns a search result into a fact:
the same body/topic/date/source-identity gates still apply to every follow-up.

The checks are deterministic so they are cheap, testable, and cannot invent
evidence.  A future version may ask a model for queries, but this P0 loop is a
safe, explainable agent step: observe -> detect gaps -> act -> re-check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urlsplit

from ..security import normalize_ws
from .research_orchestrator import PlanRun

_DIGIT_RE = __import__("re").compile(r"\d")
_NUMERIC_TOPIC_WORDS = ("价格", "成本", "参数", "性能", "token", "版本", "速度", "对比", "选择", "评测")
_COMPARE_TOPIC_WORDS = ("对比", "选择", "vs", "优缺点", "风险", "限制", "替代")


@dataclass
class AdaptiveDecision:
    gaps: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"gaps": list(self.gaps), "queries": list(self.queries), "reason": self.reason}


def _host(value: Any) -> str:
    try:
        return (urlsplit(str(value or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _text_of(entry: Any) -> str:
    return " ".join(
        str(getattr(entry, key, "") or "")
        for key in ("title", "summary", "content")
    )


def _unique_queries(values: Iterable[str], *, existing: set[str], limit: int) -> list[str]:
    result: list[str] = []
    for value in values:
        text = normalize_ws(str(value or ""))[:240]
        key = text.casefold()
        if not text or key in existing:
            continue
        existing.add(key)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def coverage_decision(
    run: PlanRun,
    plan,
    *,
    trusted_domains: Iterable[str] = (),
    topic: str = "",
    max_queries: int = 2,
    max_fetch: int = 6,
) -> AdaptiveDecision:
    """Return at most two safe follow-up queries based on observed coverage."""

    limit = max(0, min(2, int(max_queries)))
    if limit == 0:
        return AdaptiveDecision(reason="disabled")
    if str(getattr(run, "status", "")) == "deferred":
        return AdaptiveDecision(reason="deferred")
    physical = int((getattr(run, "counts", {}) or {}).get("task_physical_fetches", 0))
    if physical >= int(max_fetch):
        return AdaptiveDecision(reason="fetch_limit")

    material = list(getattr(run, "entries", []) or []) + list(
        getattr(run, "candidates", []) or []
    )
    hosts = [_host(getattr(entry, "url", "")) for entry in material]
    hosts = [host for host in hosts if host]
    distinct_hosts = set(hosts)
    trusted = [str(value or "").lower().rstrip(".") for value in trusted_domains]
    trusted = [value for value in trusted if value]
    has_trusted = bool(trusted) and any(
        any(host == value or host.endswith("." + value) for value in trusted)
        for host in hosts
    )
    has_independent = len(distinct_hosts) >= 2
    text = " ".join(_text_of(entry) for entry in material)
    has_numbers = bool(_DIGIT_RE.search(text))

    topic_text = normalize_ws(str(topic or getattr(plan, "topic", "") or ""))
    needs_numbers = any(word in topic_text.casefold() for word in _NUMERIC_TOPIC_WORDS)
    needs_counter = any(word in topic_text.casefold() for word in _COMPARE_TOPIC_WORDS)

    gaps: list[str] = []
    if trusted and not has_trusted:
        gaps.append("official_source")
    if not has_independent:
        gaps.append("independent_verification")
    if needs_numbers and not has_numbers:
        gaps.append("key_numbers")
    if needs_counter and len(distinct_hosts) < 2:
        gaps.append("counter_evidence")

    if not gaps:
        return AdaptiveDecision(reason="coverage_sufficient")

    base = normalize_ws(str(getattr(plan, "topic", "") or topic_text))[:180]
    candidates: list[str] = []
    if "official_source" in gaps:
        domain = trusted[0] if trusted else ""
        candidates.append(f"{base} site:{domain}" if domain else f"{base} 官方 公告")
    if "independent_verification" in gaps:
        candidates.append(f"{base} 独立 报道 评测")
    if "key_numbers" in gaps:
        candidates.append(f"{base} 参数 价格 版本")
    if "counter_evidence" in gaps:
        candidates.append(f"{base} 限制 问题 争议")

    existing = {
        normalize_ws(str(query or "")).casefold()
        for query in ((getattr(run, "diagnostics", {}) or {}).get("queries") or [])
    }
    queries = _unique_queries(candidates, existing=existing, limit=limit)
    reason = "follow_up" if queries else "no_new_query"
    return AdaptiveDecision(gaps=gaps, queries=queries, reason=reason)

"""S2 bounded research planning.

One compact plan per commissioned topic.  The plan may propose entities,
version strings and at most three search queries, but it is never trusted as a
source of facts: every claim still has to come from fetched material.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..adapters.types import BudgetExceeded, LLMError, NotConfigured
from ..db import Database, json_dumps
from ..security import normalize_ws
from ..timeutil import Clock, to_iso
from . import research_diagnostics as diagnostics_store

_VERSION_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9._-]{1,30}\s+\d+(?:\.\d+){0,3}\b")


@dataclass
class ResearchPlan:
    topic: str
    entities: list[str] = field(default_factory=list)
    versions: list[str] = field(default_factory=list)
    claims: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    research_mode: str = "news"
    note: str = ""
    generator: str = "fallback"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "research-plan/s2",
            "topic": self.topic,
            "entities": list(self.entities),
            "versions": list(self.versions),
            "claims": list(self.claims),
            "source_urls": list(self.source_urls),
            "queries": list(self.queries),
            "research_mode": self.research_mode,
            "note": self.note,
            "generator": self.generator,
        }


def _clean_items(value: Any, *, limit: int = 8) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in value if isinstance(value, list) else []:
        text = normalize_ws(str(item or ""))[:160]
        if not text or len(text) < 2:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def fallback_plan(
    *, topic: str, query_base: str, supplied_url: str | None, mode: str = "news"
) -> ResearchPlan:
    topic = normalize_ws(topic)[:300]
    queries = [normalize_ws(query_base)[:300]] if query_base else [topic]
    # A bounded second recall variant.  It is a query, not a claim about the
    # material; real results still have to pass body/topic checks.
    lower = (queries[0] if queries else topic).lower()
    if lower and not lower.endswith("official"):
        queries.append(f"{queries[0]} official")
    versions = sorted(set(_VERSION_RE.findall(f"{topic} {query_base}")))
    return ResearchPlan(
        topic=topic,
        entities=[],
        versions=versions[:6],
        claims=[],
        source_urls=[supplied_url] if supplied_url else [],
        queries=queries[:3],
        research_mode=mode,
        note="deterministic fallback; no model planning call",
        generator="fallback",
    )


def build_research_plan(
    db: Database,
    config,
    clock: Clock,
    llm,
    *,
    topic: str,
    query_base: str,
    supplied_url: str | None,
    mode: str = "news",
) -> ResearchPlan:
    """One bounded planning call; any failure returns the deterministic plan."""
    if not bool(config.get("topics.model_plan", True)) or llm is None:
        return fallback_plan(
            topic=topic, query_base=query_base, supplied_url=supplied_url, mode=mode
        )
    payload = {
        "topic": topic,
        "query_base": query_base,
        "supplied_url": supplied_url or "",
        "research_mode": mode,
        "max_queries": 3,
        "constraints": {
            "preserve_versions": True,
            "no_global_word_dedupe": True,
            "queries_must_be_search_queries_not_claims": True,
            "never_trust_model_as_source": True,
        },
    }
    try:
        response = llm.complete_json(
            purpose="research_plan",
            payload=payload,
            max_output_tokens=int(
                config.get("topics.model_plan_max_output_tokens", 700)
            ),
        )
        data = response.data if isinstance(response.data, dict) else {}
        fallback = fallback_plan(
            topic=topic, query_base=query_base, supplied_url=supplied_url, mode=mode
        )
        queries = _clean_items(data.get("queries"), limit=3) or fallback.queries
        return ResearchPlan(
            topic=topic,
            entities=_clean_items(data.get("entities"), limit=8),
            versions=_clean_items(data.get("versions"), limit=8),
            claims=_clean_items(data.get("claims"), limit=10),
            source_urls=_clean_items(data.get("source_urls"), limit=6),
            queries=queries[:3],
            research_mode=mode,
            note=normalize_ws(str(data.get("note") or ""))[:300],
            generator=str(getattr(response, "provider", "model") or "model")[:80],
        )
    except BudgetExceeded:
        # A budget stop is a terminal/deferred condition; do not hide it behind
        # a deterministic plan that would keep spending on searches.
        raise
    except (LLMError, NotConfigured, Exception) as exc:  # noqa: BLE001
        plan = fallback_plan(
            topic=topic, query_base=query_base, supplied_url=supplied_url, mode=mode
        )
        plan.note = (
            f"planning fallback after {type(exc).__name__}: "
            f"{diagnostics_store.sanitize_text(exc, limit=160)}"
        )
        return plan


def store_plan(db: Database, clock: Clock, request_id: int, plan: ResearchPlan) -> None:
    with db.tx() as conn:
        conn.execute(
            "UPDATE topic_requests SET research_plan_json=?, updated_at=? WHERE id=?",
            (json_dumps(plan.as_dict()), to_iso(clock.now()), int(request_id)),
        )

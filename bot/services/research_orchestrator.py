"""Execute one bounded S2 research plan.

Planning, transport and source identity each have their own module.  This file
only orchestrates the already-bounded query list through the existing
``run_topic_research`` engine and aggregates its diagnostics.  It never
introduces extra search/model rounds: the plan is capped at three queries and
the shared physical-fetch quota is owned by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..adapters.types import BudgetExceeded, SourceError
from .research_diagnostics import build_receipt
from .topic_research import ResearchOutcome


@dataclass
class PlanRun:
    entries: list[Any] = field(default_factory=list)
    candidates: list[Any] = field(default_factory=list)
    outcomes: list[ResearchOutcome] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    receipt: str = ""
    status: str = "no_evidence"
    attempt_id: int | None = None
    errors: list[str] = field(default_factory=list)


def _empty_counts() -> dict[str, int]:
    return {
        "raw_results": 0,
        "url_safe": 0,
        "domain_verified": 0,
        "physical_fetches": 0,
        "logical_candidates": 0,
        "usable_bodies": 0,
        "topic_matches": 0,
        "freshness_passed": 0,
        "cache_hits": 0,
        "retries": 0,
        "task_physical_fetches": 0,
        "task_retries": 0,
        "identity_unverified": 0,
        "candidate_bodies": 0,
    }


def run_plan(
    plan,
    research: Callable[[str, str | None, str], ResearchOutcome],
    *,
    direct_url: str | None = None,
    max_queries: int = 3,
) -> PlanRun:
    """Run at most ``max_queries`` planned queries, collecting all material.

    Trusted entries and unverified candidate bodies are kept separate; the
    caller performs one identity batch over the combined material.  The shared
    physical-fetch quota, not a trusted entry alone, is the hard stop.
    """
    queries = [str(q).strip() for q in list(getattr(plan, "queries", []) or []) if str(q).strip()]
    queries = queries[: max(1, min(3, int(max_queries or 3)))]
    run = PlanRun()
    failures: dict[str, int] = {}
    statuses: list[str] = []
    budget_deferred = False
    seen_entries: set[str] = set()
    seen_candidates: set[str] = set()
    for index, query in enumerate(queries):
        label = f"plan-{index + 1}"
        try:
            outcome = research(
                query,
                str(direct_url or "") if index == 0 else None,
                label,
            )
        except BudgetExceeded as exc:
            run.errors.append(f"budget_exhausted: {exc}")
            budget_deferred = True
            break
        except SourceError as exc:
            run.errors.append(f"{type(exc).__name__}: {exc}")
            if not run.entries and not run.candidates:
                break
            continue
        run.outcomes.append(outcome)
        statuses.append(str(outcome.status))
        run.attempt_id = outcome.attempt_id or run.attempt_id
        for key, value in (outcome.counts or {}).items():
            if key == "failures":
                continue
            if isinstance(value, int):
                run.counts[key] = int(run.counts.get(key, 0)) + value
        for key, value in ((outcome.counts or {}).get("failures") or {}).items():
            if isinstance(value, int):
                failures[str(key)] = failures.get(str(key), 0) + value
        for entry in outcome.entries:
            canonical = str(getattr(entry, "url", "") or "")
            if canonical and canonical not in seen_entries:
                seen_entries.add(canonical)
                run.entries.append(entry)
        for entry in outcome.candidates:
            canonical = str(getattr(entry, "url", "") or "")
            if canonical and canonical not in seen_candidates:
                seen_candidates.add(canonical)
                run.candidates.append(entry)
        if outcome.status == "deferred":
            budget_deferred = True
            break
        if outcome.status == "failed" and not (run.entries or run.candidates):
            break

    run.counts = {**_empty_counts(), **run.counts, "failures": failures}
    if run.outcomes:
        last = run.outcomes[-1]
        run.counts["task_physical_fetches"] = int(last.counts.get("task_physical_fetches", 0))
        run.counts["task_retries"] = int(last.counts.get("task_retries", 0))
        run.counts["identity_unverified"] = max(
            int(o.counts.get("identity_unverified", 0)) for o in run.outcomes
        )
    if budget_deferred or any(s == "deferred" for s in statuses):
        run.status = "deferred"
    elif run.entries or run.candidates:
        run.status = "completed"
    elif any(s == "failed" for s in statuses) or run.errors:
        run.status = "failed"
    elif any(s == "identity_unverified" for s in statuses):
        run.status = "identity_unverified"
    else:
        run.status = "no_evidence"

    run.diagnostics = {
        "schema": "research-plan-run/s2",
        "queries": queries,
        "outcomes": statuses,
        "errors": list(run.errors),
        "plan_generator": str(getattr(plan, "generator", "fallback")),
        "mode": str(getattr(plan, "research_mode", "news")),
        "candidates": len(run.candidates),
    }
    run.receipt = build_receipt(run.counts, run.diagnostics)
    return run


def run_followups(
    run: PlanRun,
    queries: list[str],
    research: Callable[[str, str | None, str], ResearchOutcome],
    *,
    max_queries: int = 2,
    label_prefix: str = "adaptive",
) -> PlanRun:
    """Run bounded gap-filling queries and merge them into an existing run.

    The same research callable, physical quota, topic check, date check and
    identity batch are used later by the caller.  This function only adds
    observations; it does not promote a candidate to evidence.
    """

    bounded = [str(q).strip() for q in list(queries or []) if str(q).strip()]
    bounded = bounded[: max(0, min(2, int(max_queries)))]
    adaptive = dict(run.diagnostics.get("adaptive") or {})
    adaptive.update(
        {
            "queries": bounded,
            "executed": 0,
            "stopped": "no_query" if not bounded else "",
            "errors": [],
        }
    )
    run.diagnostics["adaptive"] = adaptive

    failures = dict((run.counts or {}).get("failures") or {})
    seen_entries = {str(getattr(entry, "url", "") or "") for entry in run.entries}
    seen_candidates = {str(getattr(entry, "url", "") or "") for entry in run.candidates}
    budget_deferred = False
    statuses: list[str] = []

    for index, query in enumerate(bounded):
        label = f"{label_prefix}-{index + 1}"
        try:
            outcome = research(query, None, label)
        except BudgetExceeded as exc:
            run.errors.append(f"budget_exhausted: {exc}")
            adaptive["errors"].append("budget_exhausted")
            budget_deferred = True
            adaptive["stopped"] = "budget"
            break
        except SourceError as exc:
            run.errors.append(f"{type(exc).__name__}: {exc}")
            adaptive["errors"].append(f"{type(exc).__name__}: {str(exc)[:160]}")
            if not run.entries and not run.candidates:
                adaptive["stopped"] = "source_error"
                break
            continue

        adaptive["executed"] = int(adaptive.get("executed", 0)) + 1
        run.outcomes.append(outcome)
        statuses.append(str(outcome.status))
        run.attempt_id = outcome.attempt_id or run.attempt_id
        for key, value in (outcome.counts or {}).items():
            if key == "failures":
                continue
            if isinstance(value, int):
                run.counts[key] = int(run.counts.get(key, 0)) + value
        for key, value in ((outcome.counts or {}).get("failures") or {}).items():
            if isinstance(value, int):
                failures[str(key)] = failures.get(str(key), 0) + value
        for entry in outcome.entries:
            canonical = str(getattr(entry, "url", "") or "")
            if canonical and canonical not in seen_entries:
                seen_entries.add(canonical)
                run.entries.append(entry)
        for entry in outcome.candidates:
            canonical = str(getattr(entry, "url", "") or "")
            if canonical and canonical not in seen_candidates:
                seen_candidates.add(canonical)
                run.candidates.append(entry)
        if outcome.status == "deferred":
            budget_deferred = True
            adaptive["stopped"] = "deferred"
            break
        if outcome.status == "failed" and not (run.entries or run.candidates):
            adaptive["stopped"] = "failed"
            break

    run.counts = {**_empty_counts(), **run.counts, "failures": failures}
    if run.outcomes:
        last = run.outcomes[-1]
        run.counts["task_physical_fetches"] = int(last.counts.get("task_physical_fetches", 0))
        run.counts["task_retries"] = int(last.counts.get("task_retries", 0))
        run.counts["identity_unverified"] = max(
            int(o.counts.get("identity_unverified", 0)) for o in run.outcomes
        )
    if budget_deferred or any(s == "deferred" for s in statuses):
        run.status = "deferred"
    elif run.entries or run.candidates:
        run.status = "completed"
    elif any(s == "identity_unverified" for s in statuses):
        run.status = "identity_unverified"
    elif any(s == "failed" for s in statuses):
        run.status = "failed"
    else:
        run.status = "no_evidence"
    if not adaptive["stopped"]:
        adaptive["stopped"] = "max_queries"
    run.diagnostics["queries"] = list(run.diagnostics.get("queries") or []) + bounded
    run.diagnostics["outcomes"] = list(run.diagnostics.get("outcomes") or []) + statuses
    run.diagnostics["adaptive"] = adaptive
    run.receipt = build_receipt(run.counts, run.diagnostics)
    return run


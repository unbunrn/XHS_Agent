"""Offline replay/evaluation metrics for the content agent.

This module does not call a model or a platform.  It aggregates recorded run
outcomes so the workflow, adaptive-research and critic arms can be compared
with the same definitions.  A live evaluator can write its traces into the
same JSONL record shape; the metric layer stays deterministic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

ARMS = ("workflow", "adaptive", "critic")
ARM_LABELS = {
    "workflow": "固定工作流",
    "adaptive": "+自适应检索",
    "critic": "+自适应检索+审稿",
}


@dataclass(frozen=True)
class EvalRecord:
    case_id: str
    arm: str
    passed: bool
    official_found: bool
    claims: int
    unsupported_claims: int
    queries: int
    cost_usd: float
    latency_s: float
    human_edited: bool


def parse_record(raw: dict[str, Any]) -> EvalRecord:
    arm = str(raw.get("arm") or "").strip().lower()
    if arm not in ARMS:
        raise ValueError(f"unknown eval arm: {arm!r}")
    case_id = str(raw.get("case_id") or "").strip()
    if not case_id:
        raise ValueError("eval record needs case_id")
    claims = max(0, int(raw.get("claims", 0)))
    unsupported = max(0, int(raw.get("unsupported_claims", 0)))
    if unsupported > claims:
        raise ValueError("unsupported_claims cannot exceed claims")
    return EvalRecord(
        case_id=case_id,
        arm=arm,
        passed=bool(raw.get("passed", False)),
        official_found=bool(raw.get("official_found", False)),
        claims=claims,
        unsupported_claims=unsupported,
        queries=max(0, int(raw.get("queries", 0))),
        cost_usd=max(0.0, float(raw.get("cost_usd", 0.0))),
        latency_s=max(0.0, float(raw.get("latency_s", 0.0))),
        human_edited=bool(raw.get("human_edited", False)),
    )


def load_records(path: str | Path) -> list[EvalRecord]:
    records: list[EvalRecord] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_number}: {exc}") from exc
            if not isinstance(raw, dict):
                raise ValueError(f"record at line {line_number} must be an object")
            records.append(parse_record(raw))
    return records


def _empty_arm() -> dict[str, Any]:
    return {
        "cases": 0,
        "passed": 0,
        "official_found": 0,
        "claims": 0,
        "unsupported_claims": 0,
        "queries": 0,
        "cost_usd": 0.0,
        "latency_s": 0.0,
        "human_edited": 0,
    }


def aggregate(records: Iterable[EvalRecord]) -> dict[str, Any]:
    """Return per-arm rates plus an explicit comparison table."""

    totals: dict[str, dict[str, Any]] = {arm: _empty_arm() for arm in ARMS}
    case_ids: dict[str, set[str]] = {arm: set() for arm in ARMS}
    for record in records:
        bucket = totals[record.arm]
        case_ids[record.arm].add(record.case_id)
        bucket["cases"] += 1
        bucket["passed"] += int(record.passed)
        bucket["official_found"] += int(record.official_found)
        bucket["claims"] += record.claims
        bucket["unsupported_claims"] += record.unsupported_claims
        bucket["queries"] += record.queries
        bucket["cost_usd"] += record.cost_usd
        bucket["latency_s"] += record.latency_s
        bucket["human_edited"] += int(record.human_edited)

    arms: dict[str, Any] = {}
    for arm, bucket in totals.items():
        cases = int(bucket["cases"])
        claims = int(bucket["claims"])
        arms[arm] = {
            "cases": cases,
            "unique_topics": len(case_ids[arm]),
            "first_pass_acceptance": (bucket["passed"] / cases) if cases else 0.0,
            "official_source_recall": (bucket["official_found"] / cases) if cases else 0.0,
            "unsupported_claim_rate": (bucket["unsupported_claims"] / claims) if claims else 0.0,
            "human_edit_rate": (bucket["human_edited"] / cases) if cases else 0.0,
            "avg_queries": (bucket["queries"] / cases) if cases else 0.0,
            "avg_cost_usd": (bucket["cost_usd"] / cases) if cases else 0.0,
            "avg_latency_s": (bucket["latency_s"] / cases) if cases else 0.0,
        }
    comparable = [arm for arm in ARMS if arms[arm]["cases"]]
    return {
        "schema": "agent-eval/summary-v1",
        "arms": arms,
        "comparable_arms": comparable,
        "note": "offline replay; no live model/platform calls",
    }


def render_markdown(summary: dict[str, Any]) -> str:
    headers = [
        "arm",
        "cases",
        "first-pass",
        "official recall",
        "unsupported claims",
        "human edit",
        "avg queries",
        "avg cost",
        "avg latency",
    ]
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(["---"] * len(headers)) + "|",
    ]
    for arm in ARMS:
        row = summary["arms"].get(arm) or {}
        if not row.get("cases"):
            continue
        lines.append(
            "| {arm} | {cases} | {first:.1%} | {official:.1%} | {unsupported:.1%} | "
            "{edited:.1%} | {queries:.1f} | ${cost:.3f} | {latency:.1f}s |".format(
                arm=ARM_LABELS.get(arm, arm),
                cases=row["cases"],
                first=row["first_pass_acceptance"],
                official=row["official_source_recall"],
                unsupported=row["unsupported_claim_rate"],
                edited=row["human_edit_rate"],
                queries=row["avg_queries"],
                cost=row["avg_cost_usd"],
                latency=row["avg_latency_s"],
            )
        )
    return "\n".join(lines)

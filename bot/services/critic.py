"""Bounded pre-approval editorial critic.

This is deliberately an *advisory* quality gate, not a security boundary.  The
existing deterministic content validation, approval binding, source checks and
publish gate remain authoritative.  The critic exists to create a real
generate -> review -> revise loop before a human sees the draft.
"""

from __future__ import annotations

import re
from typing import Any

from ..config import Config

CRITIC_VERDICTS = {"pass", "revise", "reject"}
_WS = re.compile(r"\s+")


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, (str, int, float)):
        return ""
    return _WS.sub(" ", str(value)).strip()[:limit]


def build_critic_payload(
    *,
    event: Any,
    evidence: list[dict[str, Any]],
    content: Any,
    config: Config,
) -> dict[str, Any]:
    """Build a minimal, bounded review payload.

    Only evidence that the writer could actually cite is included.  The draft
    is the already validated structured content, not raw model text.
    """

    draft = content.as_dict() if hasattr(content, "as_dict") else content
    evidence_payload = []
    for item in evidence[:6]:
        row = dict(item) if not isinstance(item, dict) else item
        evidence_payload.append(
            {
                "evidence_id": str(row.get("id", "")),
                "quote": _text(row.get("quote"), 1200),
                "source_name": _text(row.get("source_name"), 120),
                "source_url": _text(row.get("url"), 500),
                "source_role": _text(row.get("source_role") or "unknown", 30),
                "source_verified": bool(row.get("source_verified")),
                "vendor_claim": bool(row.get("vendor_claim")),
                "published_at": _text(row.get("published_at"), 60),
            }
        )
    event_payload = {
        "title": _text(dict(event).get("title") if not isinstance(event, dict) else event.get("title"), 300),
        "summary": _text(dict(event).get("summary") if not isinstance(event, dict) else event.get("summary"), 800),
    }
    return {
        "task": "review_draft_before_human_approval",
        "event": event_payload,
        "evidence": evidence_payload,
        "draft": draft,
        "constraints": {
            "title_max_chars": int(config.get("writer.title_max_chars", 20)),
            "body_max_chars": int(config.get("writer.body_max_chars", 900)),
            "max_problems": int(config.get("agent.max_critic_problems", 5)),
        },
    }


def parse_critic_review(data: Any, *, max_problems: int = 5) -> dict[str, Any]:
    """Fail closed to ``revise`` on malformed reviewer output.

    A malformed review must not silently become an approval.  The caller may
    skip the critic entirely when it is disabled/unavailable; once enabled, an
    unreadable verdict gets one bounded chance to be repaired by a revision.
    """

    if not isinstance(data, dict):
        return {
            "verdict": "revise",
            "summary": "critic did not return a JSON object",
            "problems": [
                {
                    "kind": "other",
                    "evidence_ids": [],
                    "instruction": "return a valid pass/revise/reject JSON review",
                }
            ],
            "confidence": 0.0,
        }
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in CRITIC_VERDICTS:
        verdict = "revise"
    raw_problems = data.get("problems")
    if not isinstance(raw_problems, list):
        raw_problems = []
    problems = []
    for item in raw_problems[: max(0, int(max_problems))]:
        if isinstance(item, dict):
            kind = _text(item.get("kind"), 40) or "other"
            instruction = _text(item.get("instruction"), 500)
            if not instruction:
                instruction = _text(item.get("message"), 500)
            evidence_ids = item.get("evidence_ids")
            if not isinstance(evidence_ids, list):
                evidence_ids = []
            evidence_ids = [_text(value, 40) for value in evidence_ids if _text(value, 40)]
        else:
            kind = "other"
            instruction = _text(item, 500)
            evidence_ids = []
        if instruction:
            problems.append(
                {"kind": kind, "evidence_ids": evidence_ids, "instruction": instruction}
            )
    if verdict == "pass" and problems:
        verdict = "revise"
    if verdict == "reject" and not problems:
        problems.append(
            {
                "kind": "other",
                "evidence_ids": [],
                "instruction": "critic rejected the draft without a concrete reason",
            }
        )
    if verdict == "revise" and not problems:
        problems.append(
            {
                "kind": "other",
                "evidence_ids": [],
                "instruction": "revise the draft to address the review findings",
            }
        )
    return {
        "verdict": verdict,
        "summary": _text(data.get("summary"), 500),
        "problems": problems[: max(1, int(max_problems))],
        "confidence": _safe_confidence(data.get("confidence")),
    }


def critic_instruction(review: dict[str, Any]) -> str:
    """Turn reviewer findings into a bounded writer instruction."""

    lines = ["发布前审稿发现以下必须修正的问题："]
    for index, problem in enumerate(review.get("problems") or [], 1):
        ids = ",".join(problem.get("evidence_ids") or [])
        suffix = f"（证据 {ids}）" if ids else ""
        lines.append(f"{index}. [{problem.get('kind') or 'other'}] {problem.get('instruction')}{suffix}")
    lines.append("请只依据 evidence 修正，不要新增没有证据的事实、数字或来源口吻。")
    return "\n".join(lines)[:3000]


def _safe_confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))

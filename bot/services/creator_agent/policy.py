"""Deterministic source/claim policy for Creator Agent V2.

The model may propose claims; the program decides whether they are eligible for
submission.  This module never reads the network and never upgrades a source
because its own page says it is official.
"""

from __future__ import annotations

import re
from typing import Iterable

from .contracts import ArtifactVersion, ClaimAssessment, Material

_NUMBER = re.compile(
    r"\d+(?:[.,]\d+)*\s*(?:%|％|万|亿|k|K|m|M|ms|s|秒|分钟|小时|token|tokens)?"
)
_DETAIL_STATUSES = {"supported", "attributed"}


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _numbers(value: str) -> list[str]:
    return [m.group(0).replace(" ", "").replace(",", "") for m in _NUMBER.finditer(value)]


def _same_quote(quote: str, material: Material) -> bool:
    q = _normalize_text(quote)
    if not q:
        return False
    haystack = _normalize_text(material.quote)
    if q in haystack:
        return True
    # Live material quotes are bounded excerpts; a longer claim quote still has
    # to match the fetched excerpt exactly rather than merely share numbers.
    return len(haystack) >= 50 and haystack in q


def claim_issues(claims: Iterable[ClaimAssessment], materials: dict[str, Material]) -> list[str]:
    issues: list[str] = []
    for claim in claims:
        material = materials.get(claim.material_id or "")
        if material is None:
            # An explicit evidence reference must resolve; a pure inference
            # claim (no evidence id) is allowed and is reviewed as analysis.
            if claim.material_id or claim.status in _DETAIL_STATUSES:
                issues.append(f"{claim.claim_id}: unknown material {claim.material_id!r}")
            continue
        if claim.status in _DETAIL_STATUSES:
            if material.content_type == "summary" or material.role == "aggregator":
                issues.append(
                    f"{claim.claim_id}: aggregator/summary cannot support a detail claim"
                )
            if not claim.quote.strip():
                issues.append(f"{claim.claim_id}: supported/attributed claim has no quote")
            elif not _same_quote(claim.quote, material):
                issues.append(f"{claim.claim_id}: quote is not present in the material")
            claim_numbers = _numbers(claim.text)
            quote_numbers = set(_numbers(claim.quote))
            missing = [number for number in claim_numbers if number not in quote_numbers]
            if missing:
                issues.append(
                    f"{claim.claim_id}: number(s) absent from the supporting quote: {', '.join(missing)}"
                )
        if claim.core and claim.status == "contradicted":
            issues.append(f"{claim.claim_id}: core premise is contradicted")
    return issues


def submission_issues(artifact: ArtifactVersion, materials: dict[str, Material]) -> list[str]:
    issues: list[str] = []
    if not artifact.title.strip():
        issues.append("artifact title is empty")
    if not artifact.body.strip():
        issues.append("artifact body is empty")
    if not artifact.images:
        issues.append("artifact has no rendered images")
    issues.extend(claim_issues(artifact.claims, materials))
    return issues

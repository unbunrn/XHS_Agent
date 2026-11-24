"""Typed contracts for the Creator Agent V2 loop.

These are deliberately small dataclasses: the existing project uses plain
objects + explicit validation, and the contracts must stay usable from tests
without a new framework.  They describe *semantics*; SQLite rows are a
separate persistence concern in :mod:`bot.services.creator_agent.store`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Literal

from bot.timeutil import to_iso


def _now() -> str:
    return to_iso(datetime.now(timezone.utc))


def _hash(value: Any) -> str:
    import json

    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


@dataclass
class CreativeBrief:
    run_key: str
    account_id: str
    origin: Literal["manual", "scheduled", "revision"]
    topic: str
    instruction: str = ""
    source_url: str = ""
    research_mode: Literal["news", "analysis"] = "news"
    must_keep: list[str] = field(default_factory=list)
    may_change: list[str] = field(default_factory=list)
    base_artifact_version: int | None = None
    parent_run_key: str | None = None
    generation: int = 1
    feedback_message_id: str = ""
    created_at: str = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_key": self.run_key,
            "account_id": self.account_id,
            "origin": self.origin,
            "topic": self.topic,
            "instruction": self.instruction,
            "source_url": self.source_url,
            "research_mode": self.research_mode,
            "must_keep": list(self.must_keep),
            "may_change": list(self.may_change),
            "base_artifact_version": self.base_artifact_version,
            "parent_run_key": self.parent_run_key,
            "generation": int(self.generation),
            "feedback_message_id": self.feedback_message_id,
            "created_at": self.created_at,
        }

    @property
    def brief_hash(self) -> str:
        return _hash(self.as_dict())


@dataclass(frozen=True)
class AgentDecision:
    action: str
    args: dict[str, Any] = field(default_factory=dict)
    intent_summary: str = ""
    expected_result: str = ""
    referenced_material_ids: list[str] = field(default_factory=list)
    base_version: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentDecision":
        if not isinstance(data, dict):
            raise ValueError("decision must be an object")
        action = str(data.get("action") or "").strip()
        if not action:
            raise ValueError("decision.action is required")
        args = data.get("args") or {}
        if not isinstance(args, dict):
            raise ValueError("decision.args must be an object")
        refs = data.get("referenced_material_ids") or []
        if not isinstance(refs, list) or not all(isinstance(x, str) for x in refs):
            raise ValueError("referenced_material_ids must be strings")
        base = data.get("base_version")
        if base is not None and not isinstance(base, int):
            raise ValueError("base_version must be an integer")
        return cls(
            action=action,
            args=args,
            intent_summary=str(data.get("intent_summary") or "")[:500],
            expected_result=str(data.get("expected_result") or "")[:500],
            referenced_material_ids=refs[:20],
            base_version=base,
        )


@dataclass
class Material:
    material_id: str
    url: str
    title: str = ""
    role: str = "unknown"  # official / primary / secondary / community / aggregator / unknown
    published_at: str | None = None
    fetched_at: str = field(default_factory=_now)
    content_type: str = "body"  # body / summary
    content_hash: str = ""
    quote: str = ""
    article_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class ClaimAssessment:
    claim_id: str
    text: str
    status: Literal["supported", "attributed", "inference", "uncertain", "contradicted"]
    material_id: str | None = None
    quote: str = ""
    rationale: str = ""
    expression: str = ""
    core: bool = False

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class ArtifactVersion:
    run_key: str
    version: int
    title: str
    body: str
    tags: list[str] = field(default_factory=list)
    images: list[dict[str, Any]] = field(default_factory=list)
    claims: list[ClaimAssessment] = field(default_factory=list)
    content: dict[str, Any] = field(default_factory=dict)
    status: str = "draft"
    brief_hash: str = ""
    parent_version: int | None = None
    draft_id: int | None = None
    created_at: str = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_key": self.run_key,
            "version": int(self.version),
            "title": self.title,
            "body": self.body,
            "tags": list(self.tags),
            "images": list(self.images),
            "claims": [c.as_dict() for c in self.claims],
            "content": dict(self.content),
            "status": self.status,
            "brief_hash": self.brief_hash,
            "parent_version": self.parent_version,
            "draft_id": self.draft_id,
            "created_at": self.created_at,
        }


@dataclass
class ReviewIssue:
    issue_id: str
    severity: Literal["blocking", "important", "suggestion"]
    location: str
    evidence: str
    action: str

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Review:
    verdict: Literal["pass", "revise", "reject"]
    issues: list[ReviewIssue] = field(default_factory=list)
    summary: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "issues": [i.as_dict() for i in self.issues], "summary": self.summary}


@dataclass
class RevisionItem:
    instruction: str
    disposition: Literal["addressed", "not_applicable", "unresolved"] = "unresolved"
    evidence: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class RevisionPlan:
    items: list[RevisionItem] = field(default_factory=list)
    preserve: list[str] = field(default_factory=list)
    change: list[str] = field(default_factory=list)
    research_needed: list[str] = field(default_factory=list)
    image_changes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": [i.as_dict() for i in self.items],
            "preserve": list(self.preserve),
            "change": list(self.change),
            "research_needed": list(self.research_needed),
            "image_changes": list(self.image_changes),
        }

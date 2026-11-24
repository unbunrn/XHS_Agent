"""Live adapter for the V2 agent loop.

The loop is intentionally independent of the legacy fixed pipeline.  This
module is the only place that touches the real search, writer, renderer and
approval tables.  It is still approval-gated: ``submit_for_approval`` creates a
normal pending approval; it never publishes.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

from PIL import Image

from bot.services.research import build_writer_payload
from bot.services.critic import build_critic_payload, parse_critic_review
from bot.adapters.sources import DeepSeekSearchSource, PhysicalFetchQuota, strip_html, normalize_ws
from bot.content import DraftContent, validate_draft_payload
from bot.services.drafts import DraftError, _finalize_with_approval, _insert_draft, _persist_artifact_images, _schedule
from bot.services.research import ingest_targeted_entries
from bot.timeutil import to_iso
from bot.security import images_digest, sha256_file, sha256_text
from bot.adapters.types import BudgetExceeded, LLMOutputError, SourceEntry
from bot.adapters.sources import host_matches_allowed_domains
from bot.services.source_policy import trusted_search_domains

from .contracts import ArtifactVersion, ClaimAssessment, CreativeBrief, Material, Review, ReviewIssue
from .policy import claim_issues, submission_issues
from .registry import ToolResult


class DraftValidationError(DraftError):
    """A validation failure that the model can act on.

    The old code reduced every validation error to one English string.  Keep the
    machine-readable issues and the rejected candidate so the revision loop can
    make one targeted repair instead of repeating the same call.
    """

    def __init__(
        self,
        message: str,
        *,
        issues: list[dict[str, Any]] | None = None,
        candidate: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.issues = list(issues or [])
        self.candidate = dict(candidate or {})


def _candidate_view(data: dict[str, Any]) -> dict[str, Any]:
    """Only return fields a repair step can safely reuse."""
    return {
        key: data.get(key)
        for key in ("title", "caption", "storyboard", "claims", "tags", "uncertainties")
        if key in data
    }


def _validation_issues(exc: Exception, data: dict[str, Any]) -> list[dict[str, Any]]:
    text = str(exc)
    issues: list[dict[str, Any]] = []
    match = re.search(
        r"numeric claim in '([^']+)' is not supported by cited evidence: ([^\s]+)",
        text,
    )
    if match:
        issues.append(
            {
                "field": match.group(1),
                "type": "numeric_claim",
                "snippet": match.group(2),
                "action": "cite the source that contains this number, or remove it from this field",
            }
        )
    unknown = re.search(r"claim cites unknown evidence: (\[[^\]]*\])", text)
    if unknown:
        issues.append(
            {
                "field": "claims",
                "type": "unknown_evidence",
                "snippet": unknown.group(1),
                "action": "use only material IDs present in the brief",
            }
        )
    if not issues:
        issues.append(
            {
                "field": "draft",
                "type": "validation",
                "snippet": text[:300],
                "action": "repair this field once; do not repeat the same unchanged draft",
            }
        )
    issues[0]["candidate"] = _candidate_view(data)
    return issues


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _quote_for_claim(body: str, claim_text: str) -> str:
    """Pick a source excerpt that actually contains the claim's numbers.

    The old implementation always used ``body[:300]``.  A source often starts
    with navigation or an intro, so the reviewer saw numbers that were absent
    from the quote and demanded another revision.  Prefer a bounded window
    around the first matching number; otherwise keep the old short quote.
    """
    body = str(body or "")
    if not body:
        return ""
    numbers = re.findall(r"\d+(?:\.\d+)?", str(claim_text or ""))
    if not numbers:
        return body[:300]
    positions = [body.find(number) for number in numbers]
    positions = [pos for pos in positions if pos >= 0]
    if not positions:
        return body[:300]
    start = max(0, min(positions) - 240)
    return body[start : start + 2000]


class LiveRuntime:
    """Real runtime used by a live ``creator_agent_run`` job.

    Dependencies are injected so the adapter can be tested without a network or
    model call.
    """

    def __init__(self, *, db, config, clock, http, budget, llm, store, run_key: str,
                 search_factory=None, renderer=None, image_inspector=None):
        self.db = db
        self.config = config
        self.clock = clock
        self.http = http
        self.budget = budget
        self.llm = llm
        self.store = store
        self.run_key = run_key
        self.search_factory = search_factory
        self.renderer = renderer
        self.image_inspector = image_inspector
        self._bodies: dict[str, str] = {}
        self._materials: dict[str, Material] = {}
        # One hard fetch budget for the whole run; a second search must not
        # silently reset the transport allowance.
        self.fetch_quota = PhysicalFetchQuota(
            int(config.get("creator_agent.max_physical_fetches", 6))
        )
        persisted = self.store.get_run(run_key) or {}
        counters = dict(persisted.get("counters") or {})
        # A restarted runtime continues the same run's budgets instead of
        # silently starting a fresh allowance.
        self.fetch_quota.attempts = min(
            self.fetch_quota.limit, max(0, int(counters.get("physical_fetches") or 0))
        )
        self.last_physical_fetches = 0
        self.writer_model_calls = 0
        self.model_calls = max(0, int(counters.get("_runtime_model_calls") or 0))
        self._model_call_baseline = self.model_calls
        self.model_limit = int(config.get("creator_agent.max_model_calls", 14))
        for material in self.store.materials(run_key):
            self._materials[material.material_id] = material
            self._bodies[material.material_id] = self.store.material_body(run_key, material.material_id)

    # ----- read-only tools -------------------------------------------------
    def _reserve_model_call(self) -> None:
        """Reserve one real provider attempt before it is sent.

        This is a hard per-run cap, not just a counter.  The reservation is
        persisted immediately so a process crash cannot hand back budget.
        Transport retries call this again for every HTTP attempt.
        """
        if self.model_calls >= self.model_limit:
            raise BudgetExceeded("creator agent model call limit reached")
        self.model_calls += 1
        self._persist_model_calls()

    def _persist_model_calls(self) -> None:
        run = self.store.get_run(self.run_key) or {}
        counters = dict(run.get("counters") or {})
        counters["_runtime_model_calls"] = int(self.model_calls)
        counters["model_calls"] = max(
            int(counters.get("model_calls") or 0), int(self.model_calls)
        )
        self.store.update_run(self.run_key, counters=counters)

    def load_parent_artifact(self, artifact: ArtifactVersion | None) -> list[Material]:
        """Rehydrate the evidence snippets belonging to a revision base.

        The original full body lives in the immutable artifact content.  The
        quoted evidence rows are sufficient for the writer to cite the same
        sources without pretending a summary is a new fetch.
        """
        if artifact is None:
            return []
        # Prefer the exact materials the parent Creator Agent run searched and
        # saved.  The old evidence table only keeps a short quote, so a number
        # supported by the full body looked unsupported during revision.
        if artifact.run_key:
            parent_materials = self.store.materials(artifact.run_key)
            if parent_materials:
                out: list[Material] = []
                for material in parent_materials:
                    body = self.store.material_body(artifact.run_key, material.material_id)
                    body = body or material.quote or ""
                    material = replace(material, quote=body)
                    self._materials[material.material_id] = material
                    self._bodies[material.material_id] = body
                    out.append(material)
                return out
        if not artifact.draft_id:
            return []
        draft = self.db.fetchone(
            "SELECT event_id FROM drafts WHERE id=?", (int(artifact.draft_id),)
        )
        if draft is None:
            return []
        rows = self.db.fetchall(
            "SELECT id, quote, source_url, source_name, published_at, source_role "
            "FROM evidence WHERE event_id=? ORDER BY id",
            (int(draft["event_id"]),),
        )
        out = []
        for row in rows:
            mid = f"P{int(row['id'])}"
            quote = str(row["quote"] or "")
            material = Material(
                material_id=mid,
                url=str(row["source_url"] or ""),
                title=str(row["source_name"] or _host(str(row["source_url"] or ""))),
                role=str(row["source_role"] or "secondary"),
                published_at=row["published_at"],
                content_type="quote",
                content_hash=sha256_text(quote),
                quote=quote,
            )
            self._materials[mid] = material
            self._bodies[mid] = quote
            out.append(material)
        return out

    def discover_topics(self, limit: int = 10):
        return []

    def search(self, query: str, limit: int = 8, mode: str = "news"):
        factory = self.search_factory or self._default_search_factory
        source = factory(str(query), int(limit), str(mode))
        before = self.fetch_quota.attempts
        raw, _diag = source.search_candidates()
        self.last_physical_fetches = self.fetch_quota.attempts - before
        out: list[Material] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            title = normalize_ws(strip_html(str(item.get("title") or "")))[:300]
            body = str(item.get("content") or item.get("raw_content") or "").strip()
            if not body:
                try:
                    fetched = source.fetch_content(url)
                    body = str(fetched.get("content") or "").strip()
                except Exception:
                    body = ""
            if not body:
                continue
            mid = f"M{len(self._materials) + 1}"
            role = "official" if trusted_search_domains(self.config) and host_matches_allowed_domains(
                _host(url), trusted_search_domains(self.config)
            ) else "secondary"
            material = Material(
                material_id=mid,
                url=url,
                title=title or url,
                role=role,
                content_hash=sha256_text(body),
                quote=body[:3000],
                content_type="body",
            )
            self._bodies[mid] = body
            self._materials[mid] = material
            self.store.save_material(self.run_key, material, content=body)
            out.append(material)
            if len(out) >= max(1, int(limit)):
                break
        return out

    def _default_search_factory(self, query: str, limit: int, mode: str):
        entry = {
            "name": "creator-v2-search",
            "kind": "deepseek_search",
            "query": query,
            "max_results": max(1, min(10, int(limit))),
            "max_fetches": max(1, min(6, int(limit))),
            "fetch_content": False,
            "discovery_mode": "any" if mode == "analysis" else "recent",
            "allow_all_domains": True,
        }
        return DeepSeekSearchSource(
            entry,
            self.config,
            self.http,
            self.clock,
            budget=self.budget,
            fetch_quota=self.fetch_quota,
            allow_all_domains=True,
        )

    def read_source(self, material_id=None, url=None):
        if material_id and material_id in self._materials:
            if not self._bodies.get(material_id):
                self._bodies[material_id] = self.store.material_body(self.run_key, material_id)
            return self._materials[material_id]
        if not url:
            raise KeyError("material_id or url is required")
        before = self.fetch_quota.attempts
        body = ""
        try:
            self.fetch_quota.record_send()
            response = self.http.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; OpsAgent/1.0)"},
                timeout=float(self.config.get("network.timeout_seconds", 20)),
                max_bytes=int(self.config.get("network.max_fetch_bytes", 2_097_152)),
                retries=0,
            )
            if response.status != 200:
                raise RuntimeError(f"fetch HTTP {response.status}")
            body = normalize_ws(strip_html(response.text()))[:20000]
        except Exception as direct_exc:
            # When local direct fetch fails (e.g. overseas host behind local TUN
            # Fake-IP or anti-bot block), fall back to the official DeepSeek
            # /v1/tools/fetch endpoint within the same fetch_quota and budget.
            if self.fetch_quota.remaining <= 0:
                self.last_physical_fetches = self.fetch_quota.attempts - before
                raise direct_exc
            try:
                source = self._default_search_factory("fetch", 1, "news")
                fetched = source.fetch_content(str(url))
                body = normalize_ws(strip_html(str(fetched.get("content") or "")))[:20000]
            except Exception:
                self.last_physical_fetches = self.fetch_quota.attempts - before
                raise direct_exc
            if not body:
                self.last_physical_fetches = self.fetch_quota.attempts - before
                raise direct_exc
        self.last_physical_fetches = self.fetch_quota.attempts - before
        mid = material_id or f"M{len(self._materials) + 1}"
        material = Material(material_id=mid, url=str(url), title=str(url), role="secondary",
                            content_hash=sha256_text(body), quote=body[:3000])
        self._bodies[mid] = body
        self._materials[mid] = material
        self.store.save_material(self.run_key, material, content=body)
        return material

    def inspect_claims(self, claims: list, material_ids: list[str]):
        out = []
        for i, text in enumerate(claims):
            mid = material_ids[min(i, len(material_ids) - 1)] if material_ids else next(iter(self._bodies), "M1")
            body = self._bodies.get(mid, "")
            ok = str(text) in body
            out.append(ClaimAssessment(
                claim_id=f"C{i+1}", text=str(text),
                status="supported" if ok else "uncertain",
                material_id=mid if ok else None,
                quote=body[:300] if ok else "",
                rationale="exact quote" if ok else "not found in fetched body",
                expression="据材料" if ok else "",
                core=i == 0,
            ))
        return out

    def recall_context(self, scope: str = ""):
        rows = self.store.preferences(account_id=self._account_id(), scope=scope, limit=10)
        return [{"text": r.get("text", ""), "scope": r.get("scope", "")} for r in rows]

    # ----- writer ----------------------------------------------------------
    def _evidence(self, material_ids: list[str]) -> tuple[list[dict], list[str]]:
        ids = [m for m in material_ids if m in self._materials] or list(self._materials)
        evidence = []
        for mid in ids:
            m = self._materials[mid]
            body = self._bodies.get(mid, "")
            evidence.append({
                # A 900-character window used to hide facts that lived later in
                # the same fetched page.  Keep a generous bounded body (the
                # writer payload caps the number of materials), not a summary.
                "id": mid, "quote": (body or m.quote)[:4000],
                "source_name": m.title or _host(m.url), "url": m.url,
                "published_at": m.published_at, "trust_tier": 2,
                "source_role": m.role, "source_verified": m.role == "official",
                "vendor_claim": False,
            })
        return evidence, ids

    @staticmethod
    def _revision_changes(instruction: str) -> list[dict[str, str]]:
        text = str(instruction or "")
        changes: list[dict[str, str]] = []
        if "标题" in text and any(word in text for word in ("短", "精简")):
            changes.append({"artifact": "title", "intent": "shorten"})
        if "去重" in text or "重复" in text:
            changes.append({"artifact": "images", "intent": "deduplicate"})
        if "排版" in text or "配图" in text:
            changes.append({"artifact": "images", "intent": "layout"})
        if "扩" in text and "正文" in text:
            changes.append({"artifact": "caption", "intent": "expand"})
        return changes

    @staticmethod
    def _visible(content: dict[str, Any]) -> dict[str, Any]:
        return {
            key: content.get(key)
            for key in ("title", "caption", "storyboard", "claims")
        }

    def _apply_local_scope(
        self,
        *,
        current: dict[str, Any] | None,
        content: DraftContent,
        brief: CreativeBrief,
        instruction: str,
        revision_plan: dict[str, Any] | None,
    ) -> None:
        """Keep unrequested fields when the owner asked for a local edit.

        This is deliberately a small merge, not a second writer.  It only
        protects the common cases (title-only, opening-only) from a full-body
        rewrite drifting into unrelated claims.
        """
        if not current:
            return
        text = str(instruction or "")
        scope = str((revision_plan or {}).get("scope") or "")
        rewrite_words = ("整体重写", "全部重写", "整篇重写", "重新写一篇", "全文重写")
        if scope == "rewrite" or any(word in text for word in rewrite_words):
            return
        if "all" in (brief.may_change or []):
            return
        base = DraftContent.from_dict(current)
        body_words = ("正文", "开头", "全文", "段落", "内容")
        title_only = ("标题" in text or scope == "title") and not any(
            word in text for word in body_words
        )
        opening_only = ("开头" in text or scope == "opening") and not any(
            word in text for word in ("全文", "整体", "整篇")
        )
        if title_only:
            content.caption = list(base.caption)
            content.storyboard = base.storyboard
            content.claims = list(base.claims)
            content.sections = list(base.sections)
        elif opening_only and content.caption and base.caption:
            content.caption = [content.caption[0], *base.caption[1:]]
            content.storyboard = base.storyboard
            content.claims = list(base.claims)
            content.sections = list(base.sections)

    def _write(self, brief: CreativeBrief, *, instruction: str = "", material_ids=None,
               base_version=None, current: dict | None = None,
               base_artifact: ArtifactVersion | None = None,
               revision_plan: dict[str, Any] | None = None):
        evidence, ids = self._evidence([str(x) for x in (material_ids or [])])
        if not evidence:
            raise DraftError("no materials available for drafting")
        event = {
            "id": 0, "title": brief.topic, "summary": brief.instruction or brief.topic,
            "latest_published_at": None, "source_name": "creator-agent",
        }
        writer_instruction = (instruction or brief.instruction or "").strip()
        revision_request = None
        if current is not None:
            revision_request = {
                "instruction": instruction or brief.instruction,
                "must_keep": list(brief.must_keep or []),
                "may_change": list(brief.may_change or []),
                "changes": self._revision_changes(instruction or brief.instruction),
                "scope": str((revision_plan or {}).get("scope") or ""),
            }
        payload = build_writer_payload(
            self.db,
            self.config,
            event,
            evidence,
            clock=self.clock,
            instruction=writer_instruction,
            current_draft=current,
            revision_request=revision_request,
            seed=self.run_key,
        )
        # The writer sees a bounded excerpt, but the validator must check the
        # saved body.  Otherwise a fact at character 4,001 is invisible to the
        # number check even though the source is stored locally.
        validation_evidence = {
            e["id"]: {**e, "quote": self._bodies.get(e["id"]) or e["quote"]}
            for e in evidence
        }
        self.writer_model_calls += 1
        max_output_tokens = int(self.config.get("budget.max_output_tokens", 1800))
        if current is not None:
            # A revision carries the full parent draft plus the requested
            # comparison/expansion.  Keep the draft budget unchanged but give
            # the revision writer enough room to finish its JSON object.
            max_output_tokens = max(
                max_output_tokens,
                int(self.config.get("budget.revision_max_output_tokens", 2600)),
            )
        response = self.llm.complete_json(
            purpose="revise" if current is not None else "draft", payload=payload,
            max_output_tokens=max_output_tokens,
            before_attempt=self._reserve_model_call,
        )
        try:
            content = validate_draft_payload(
                response.data,
                evidence_by_id=validation_evidence,
                config=self.config,
                date_unknown=False,
                generator=getattr(response, "provider", "unknown") or "unknown",
                model=getattr(response, "model", "") or "",
                require_storyboard=True,
                soft_body_limit=True,
                include_sources_in_body=False,
                public_disclaimers=False,
                repair_caption_claims=True,
                owner_context=f"{brief.topic}\n{brief.instruction}",
            )
        except (LLMOutputError, DraftError) as exc:
            candidate = response.data if isinstance(response.data, dict) else {}
            raise DraftValidationError(
                str(exc),
                issues=_validation_issues(exc, candidate),
                candidate=_candidate_view(candidate),
            ) from exc
        # The writer prompt still occasionally emits a report-style disclaimer.
        # Keep it in private uncertainty metadata, not in the XHS caption.
        content.caption = [
            paragraph for paragraph in content.caption
            if "公开资料整理" not in paragraph and "未做个人实测" not in paragraph
        ]
        cleaned_caption = []
        for paragraph in content.caption:
            without_urls = re.sub(r"https?://\S+", "", paragraph).strip()
            without_urls = re.sub(r"[ \t]{2,}", " ", without_urls)
            if without_urls:
                cleaned_caption.append(without_urls)
        content.caption = cleaned_caption
        if not content.caption:
            raise DraftError("writer produced no public caption after removing boilerplate")
        content = self._maybe_polish_draft(
            content=content,
            brief=brief,
            evidence=evidence,
            validation_evidence=validation_evidence,
        )
        if current is not None:
            self._apply_local_scope(
                current=current,
                content=content,
                brief=brief,
                instruction=instruction or brief.instruction,
                revision_plan=revision_plan,
            )
            if base_artifact is not None and self._visible(content.as_dict()) == self._visible(current):
                raise DraftValidationError(
                    "修改没有产生可见变化",
                    issues=[
                        {
                            "field": "draft",
                            "type": "no_change",
                            "snippet": "",
                            "action": (
                                "make a real change to the requested field, or report that "
                                "the instruction cannot be applied"
                            ),
                        }
                    ],
                    candidate=_candidate_view(content.as_dict()),
                )
        latest = self.store.latest_artifact(brief.run_key)
        version = 1 if latest is None else int(latest.version) + 1
        artifact = ArtifactVersion(
            run_key=brief.run_key, version=version, title=content.title,
            body=content.render_body(include_sources=False), tags=list(content.tags),
            claims=self._claims_from_content(content, ids),
            content=content.as_dict(), brief_hash=brief.brief_hash,
            parent_version=base_version, created_at=to_iso(self.clock.now()),
        )
        self.store.save_artifact(artifact)
        return artifact

    def _claims_from_content(self, content: DraftContent, ids: list[str]):
        out = []
        for i, claim in enumerate(content.claims):
            text = getattr(claim, "text", None) or getattr(claim, "claim", None) or ""
            if not text:
                continue
            evidence_ids = [str(eid) for eid in (getattr(claim, "evidence_ids", None) or [])]
            mid = next((eid for eid in evidence_ids if eid in self._materials), None)
            body = self._bodies.get(mid or "", "")
            material = self._materials.get(mid or "")
            # Only a real evidence-linked claim gets ``supported`` with a quote.
            # The reviewer only sees ``material.quote`` (a bounded excerpt), not
            # the whole fetched body; generate from the same excerpt so a quote
            # can never be valid for us but impossible for the reviewer.
            quote = ""
            status = "inference" if not evidence_ids else "uncertain"
            if mid and body and material is not None:
                candidate = _quote_for_claim(body, str(text))
                missing = [
                    n for n in re.findall(r"\d+(?:\.\d+)?", str(text))
                    if n not in candidate
                ]
                if not missing and candidate:
                    quote = candidate
                    status = "supported"
            out.append(ClaimAssessment(
                claim_id=f"C{i+1}", text=str(text), status=status,
                material_id=mid, quote=quote,
                rationale="writer claim linked to fetched body" if mid else "writer analysis",
                expression="",
                core=bool(getattr(claim, "core", False)),
            ))
        return out

    def draft_artifact(self, *, brief, instruction="", material_ids=None, base_version=None):
        return self._write(brief, instruction=instruction, material_ids=material_ids, base_version=base_version)

    def _preserve_ordinals(self, brief, revision_plan) -> set[int]:
        values: list[str] = []
        values.extend(str(x) for x in (getattr(brief, "must_keep", None) or []))
        plan = revision_plan or {}
        for key in ("preserve", "must_keep"):
            values.extend(str(x) for x in (plan.get(key) or []))
        ordinals: set[int] = set()
        for value in values:
            match = re.search(r"(?:image[:_ ]?|第)?(\d+)(?:张|图)?$", value.strip(), re.I)
            if match:
                ordinals.add(int(match.group(1)))
        return ordinals

    def revise_artifact(self, *, brief, base_artifact, instruction="", revision_plan=None, material_ids=None):
        base = base_artifact.content if base_artifact else None
        artifact = self._write(brief, instruction=instruction, material_ids=material_ids,
                               base_version=base_artifact.version if base_artifact else None,
                               current=base, base_artifact=base_artifact,
                               revision_plan=revision_plan)
        preserve = self._preserve_ordinals(brief, revision_plan)
        if base_artifact is not None and base_artifact.images:
            # Keep the previous manifest available for submit.  Only explicitly
            # requested images are hard-preserved; the rest may be replaced by a
            # later render_images call.  Without this, a text-only revision has
            # no images and needs an extra render step before it can submit.
            images = []
            for index, image in enumerate(base_artifact.images, 1):
                ordinal = int(image.get("ordinal") or index)
                item = dict(image)
                item["asset_id"] = str(item.get("asset_id") or f"image:{index}")
                item["preserved"] = ordinal in preserve
                images.append(item)
            artifact.images = images
            self.store.save_artifact(artifact)
        return artifact

    # ----- render/inspect/review -------------------------------------------
    def render_images(self, *, brief, artifact, layouts=None, theme: str | None = None):
        if artifact is None:
            raise RuntimeError("no artifact")
        content = DraftContent.from_dict(artifact.content)
        out_dir = Path(self.config.resolve_path(str(self.config.get("render.output_dir", "var/drafts")))) / "creator-v2" / self.run_key / f"v{artifact.version}"
        preserved = {
            int(image.get("ordinal") or index): dict(image)
            for index, image in enumerate(artifact.images or [], 1)
            if image.get("preserved")
        }
        for ordinal, keep in preserved.items():
            source = Path(str(keep.get("path") or ""))
            expected = str(keep.get("sha256") or "")
            if not source.is_file():
                raise RuntimeError(
                    f"preserved image {ordinal} is missing at {source}; refusing to re-render it"
                )
            if expected and sha256_file(source) != expected:
                raise RuntimeError(
                    f"preserved image {ordinal} hash changed; refusing to accept new bytes"
                )
        draft_key = f"{self.run_key}-v{artifact.version}"
        if self.renderer is not None:
            import inspect

            try:
                sig = inspect.signature(self.renderer)
                if "theme" in sig.parameters:
                    rendered = self.renderer(content, out_dir, draft_key, theme=theme)
                else:
                    rendered = self.renderer(content, out_dir, draft_key)
            except (TypeError, ValueError):
                rendered = self.renderer(content, out_dir, draft_key)
        else:
            rendered = self._render(content, out_dir, draft_key, theme=theme)
        final: list[dict] = []
        out_dir.mkdir(parents=True, exist_ok=True)
        seen_ordinals: set[int] = set()
        for index, image in enumerate(rendered, 1):
            item = image.as_dict()
            if getattr(image, "theme", ""):
                item["theme"] = image.theme
            if getattr(image, "palette", ""):
                item["palette"] = image.palette
            ordinal = int(item.get("ordinal") or index)
            if ordinal in seen_ordinals:
                raise RuntimeError(f"renderer returned duplicate image ordinal {ordinal}")
            seen_ordinals.add(ordinal)
            keep = preserved.get(ordinal)
            if keep is not None:
                source = Path(str(keep.get("path") or ""))
                asset_id = str(keep.get("asset_id") or f"image:{ordinal}").replace(":", "-")
                target = out_dir / f"{ordinal:02d}-{asset_id}-{source.name}"
                if source.resolve() != target.resolve():
                    shutil.copy2(source, target)
                item = dict(keep)
                item.update({
                    "ordinal": ordinal,
                    "path": str(target),
                    "sha256": sha256_file(target),
                    "width": int(item.get("width") or image.width),
                    "height": int(item.get("height") or image.height),
                    "asset_id": str(item.get("asset_id") or f"image:{ordinal}"),
                    "preserved": True,
                })
            item.setdefault("asset_id", f"image:{index}")
            final.append(item)
        missing = set(preserved) - {int(i.get("ordinal") or 0) for i in final}
        if missing:
            raise RuntimeError(
                "renderer dropped preserved image(s) "
                + ", ".join(str(n) for n in sorted(missing))
                + "; refusing to submit an incomplete manifest"
            )
        artifact.images = final
        self.store.save_artifact(artifact)
        return artifact

    def _render(
        self,
        content,
        out_dir,
        draft_key,
        *,
        theme: str | None = None,
    ):
        from bot.render import render_draft
        return render_draft(
            content,
            out_dir=out_dir,
            config=self.config,
            draft_key=draft_key,
            db=self.db,
            theme=theme,
            run_key=self.run_key,
        )

    def _maybe_polish_draft(
        self,
        *,
        content: DraftContent,
        brief,
        evidence: list[dict],
        validation_evidence: dict,
    ) -> DraftContent:
        """Run optional single-pass style polish when AI-style lint exceeds threshold."""
        from bot.style_lint import lint_draft, recent_posts
        from bot.voice import load_voice

        voice = load_voice(self.config)
        recent = recent_posts(self.db, limit=voice.recent_window)
        report = lint_draft(content, voice, recent=recent)
        if report.score <= voice.max_score:
            return content

        if not bool(self.config.get("writer.style_polish", True)):
            return content

        import copy
        import logging

        recent_openings = [p.opening_move for p in recent[:2] if p.opening_move]
        polish_payload = {
            "draft": content.as_dict(),
            "style_issues": report.issues,
            "voice": voice.as_prompt_dict(
                seed=f"{self.run_key}:polish",
                recent_openings=recent_openings,
            ),
            "evidence": [
                {
                    "id": e["id"],
                    "quote": e["quote"],
                    "source_name": e.get("source_name", ""),
                }
                for e in evidence
            ],
        }
        try:
            self.writer_model_calls += 1
            max_out = int(self.config.get("budget.max_output_tokens", 1800))
            response = self.llm.complete_json(
                purpose="polish",
                payload=polish_payload,
                max_output_tokens=max_out,
                before_attempt=self._reserve_model_call,
            )
            data = response.data if isinstance(response.data, dict) else {}
            merged = copy.deepcopy(content.as_dict())
            if isinstance(data.get("title"), str) and data["title"].strip():
                merged["title"] = data["title"]
            if isinstance(data.get("caption"), list) and data["caption"]:
                merged["caption"] = data["caption"]

            patch = data.get("storyboard_patch")
            sb = merged.get("storyboard")
            if isinstance(patch, dict) and isinstance(sb, dict):
                patch_cover = patch.get("cover")
                if isinstance(patch_cover, dict) and isinstance(sb.get("cover"), dict):
                    for key in ("kicker", "headline", "subhead", "attribution", "emphasis"):
                        if key in patch_cover:
                            sb["cover"][key] = patch_cover[key]
                patch_cards = patch.get("cards")
                sb_cards = sb.get("cards")
                if isinstance(patch_cards, list) and isinstance(sb_cards, list):
                    for idx, p_card in enumerate(patch_cards):
                        if not isinstance(p_card, dict):
                            continue
                        target_card = None
                        if isinstance(p_card.get("index"), int) and 0 <= p_card["index"] < len(sb_cards):
                            target_card = sb_cards[p_card["index"]]
                        elif isinstance(p_card.get("ordinal"), int):
                            card_idx = p_card["ordinal"] - 2
                            if 0 <= card_idx < len(sb_cards):
                                target_card = sb_cards[card_idx]
                        elif len(patch_cards) == len(sb_cards) and idx < len(sb_cards):
                            target_card = sb_cards[idx]
                        if isinstance(target_card, dict):
                            for key in ("kicker", "headline", "subhead", "points", "columns", "attribution", "emphasis"):
                                if key in p_card:
                                    target_card[key] = p_card[key]

            polished = validate_draft_payload(
                merged,
                evidence_by_id=validation_evidence,
                config=self.config,
                date_unknown=False,
                generator=content.generator,
                model=content.model,
                require_storyboard=True,
                soft_body_limit=True,
                include_sources_in_body=False,
                public_disclaimers=False,
                repair_caption_claims=False,
                owner_context=f"{brief.topic}\n{brief.instruction}",
            )
            polished.caption = [
                p
                for p in polished.caption
                if "公开资料整理" not in p and "未做个人实测" not in p
            ]
            cleaned = []
            for p in polished.caption:
                no_urls = re.sub(r"https?://\S+", "", p).strip()
                no_urls = re.sub(r"[ \t]{2,}", " ", no_urls)
                if no_urls:
                    cleaned.append(no_urls)
            polished.caption = cleaned
            if not polished.caption:
                raise DraftError("polish produced empty caption")
            orig_cards = len(content.storyboard.all_cards()) if content.storyboard else 0
            new_cards = len(polished.storyboard.all_cards()) if polished.storyboard else 0
            if new_cards != orig_cards:
                raise DraftError("polish changed storyboard card count")
            return polished
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Style polish skipped/reverted (%s); keeping original draft", exc
            )
            issue_codes = ", ".join(
                str(i.get("code") or "") for i in report.issues[:5] if i.get("code")
            )
            note = f"style_lint(score={report.score}): {issue_codes or 'style'}"
            if note not in content.uncertainties:
                content.uncertainties.append(note)
            return content

    def _record_submitted_visual(self, artifact) -> None:
        if artifact is None or not artifact.images:
            return
        from bot.visual.selector import record_recent
        from bot.visual.similarity import dhash

        first = artifact.images[0] if artifact.images else {}
        content_dict = artifact.content if isinstance(artifact.content, dict) else {}
        sb = content_dict.get("storyboard") if isinstance(content_dict.get("storyboard"), dict) else {}
        cover = sb.get("cover") if isinstance(sb.get("cover"), dict) else {}
        plan = content_dict.get("editorial_plan") if isinstance(content_dict.get("editorial_plan"), dict) else {}

        theme = str(first.get("theme") or plan.get("visual_theme") or "")
        palette = str(first.get("palette") or "")
        cover_variant = str(cover.get("variant") or "headline")
        cover_hash: int | None = None
        path_str = str(first.get("path") or "")
        if path_str:
            p = Path(path_str)
            if p.is_file():
                try:
                    cover_hash = dhash(p)
                except Exception:
                    cover_hash = None
        record_recent(
            self.db,
            {
                "run_key": self.run_key,
                "theme": theme,
                "palette": palette,
                "cover_variant": cover_variant,
                "cover_dhash": cover_hash,
            },
            clock=self.clock,
        )

    def inspect_layout(self, artifact):
        issues = []
        if artifact is None or not artifact.images:
            return ["no rendered images"]
        for idx, image in enumerate(artifact.images, start=1):
            path = Path(str(image.get("path") or ""))
            ordinal = int(image.get("ordinal") or idx)
            is_html = bool(image.get("theme"))
            try:
                if self.image_inspector is not None:
                    issue = self.image_inspector(path)
                else:
                    issue = self._inspect_image(path, ordinal=ordinal, is_html=is_html)
            except Exception as exc:
                issue = str(exc)
            if issue:
                issues.append(f"{path}: {issue}")
        return issues

    def _inspect_image(self, path: Path, *, ordinal: int = 1, is_html: bool = False):
        if not path.exists():
            return "missing file"
        with Image.open(path) as im:
            if im.width <= 0 or im.height <= 0:
                return "invalid dimensions"
            exp_w = int(self.config.get("render.width", 1080))
            exp_h = int(self.config.get("render.height", 1440))
            if (im.width, im.height) != (exp_w, exp_h):
                return f"unexpected dimensions {im.width}x{im.height}, expected {exp_w}x{exp_h}"

        try:
            from bot.visual.similarity import dhash, hamming, largest_blank_band
        except Exception:
            return ""

        problems: list[str] = []
        max_blank = float(self.config.get("render.max_blank_band", 0.34))
        blank_limit = max_blank if is_html else max(0.90, max_blank)
        _, band_ratio = largest_blank_band(path)
        if band_ratio > blank_limit:
            problems.append(f"excessive blank band ({band_ratio:.0%} > {blank_limit:.0%})")

        if ordinal == 1:
            min_dist = int(self.config.get("render.cover_min_distance", 12))
            if min_dist > 0:
                from bot.visual.selector import load_recent

                cur_hash = dhash(path)
                for item in load_recent(self.db):
                    other_key = str(item.get("run_key") or "")
                    if other_key == self.run_key:
                        continue
                    prev_hash = item.get("cover_dhash")
                    if isinstance(prev_hash, int) and hamming(cur_hash, prev_hash) < min_dist:
                        label = other_key or "recent"
                        problems.append(
                            f"cover too similar to {label}; re-render with another theme"
                        )
        return "; ".join(problems)

    def review_artifact(self, *, brief, artifact, materials):
        issues = list(submission_issues(artifact, {m.material_id: m for m in materials}))
        if artifact is None:
            issues.append("missing artifact")
        if issues:
            return Review(verdict="revise", issues=[
                ReviewIssue(issue_id=f"I{i+1}", severity="blocking", location="artifact",
                            evidence=str(text), action="fix before submission")
                for i, text in enumerate(issues)
            ], summary="deterministic policy review")
        # Optional independent semantic review.  It is deliberately a second,
        # model-backed opinion after the deterministic policy gate; it never
        # overrides a deterministic failure and never grants approval itself.
        if bool(self.config.get("creator_agent.semantic_review", False)):
            evidence, _ = self._evidence([m.material_id for m in materials])
            payload = build_critic_payload(
                event={"title": brief.topic, "summary": brief.instruction or brief.topic},
                evidence=evidence,
                content=artifact.content or {},
                config=self.config,
            )
            try:
                response = self.llm.complete_json(
                    purpose="draft_review", payload=payload,
                    max_output_tokens=int(self.config.get("agent.critic_max_output_tokens", 900)),
                    before_attempt=self._reserve_model_call,
                )
            except BudgetExceeded:
                raise
            except Exception as exc:
                return Review(verdict="revise", issues=[
                    ReviewIssue(issue_id="I1", severity="blocking", location="artifact",
                                evidence=f"semantic review unavailable: {exc}",
                                action="retry submission after review service recovers")
                ], summary="semantic review unavailable")
            parsed = parse_critic_review(
                response.data,
                max_problems=int(self.config.get("agent.max_critic_problems", 5)),
            )
            if parsed.get("verdict") != "pass":
                review_issues = [
                    ReviewIssue(
                        issue_id=f"S{i+1}", severity="blocking", location="artifact",
                        evidence=(p.get("kind") or "other") + ": " + (p.get("instruction") or ""),
                        action="fix the cited evidence mismatch",
                    )
                    for i, p in enumerate(parsed.get("problems") or [])
                ]
                return Review(verdict="revise", issues=review_issues,
                              summary=str(parsed.get("summary") or "semantic review requested revision"))
        return Review(verdict="pass", issues=[], summary="deterministic and semantic review passed")

    # ----- approval bridge -------------------------------------------------
    def _submission_key(self, artifact: ArtifactVersion) -> str:
        return "v2:" + sha256_text(f"{self.run_key}|v{int(artifact.version)}")

    def _submission_hash(self, brief: CreativeBrief, artifact: ArtifactVersion) -> str:
        payload = {
            "brief": brief.brief_hash,
            "run": brief.run_key,
            "version": int(artifact.version),
            "content": artifact.content,
            "images": [
                {
                    "ordinal": int(image.get("ordinal") or index),
                    "sha256": str(image.get("sha256") or ""),
                    "preserved": bool(image.get("preserved")),
                }
                for index, image in enumerate(artifact.images or [], 1)
            ],
        }
        return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))

    def _find_submission_draft(self, submission_key: str):
        return self.db.fetchone(
            "SELECT * FROM drafts WHERE submission_key=?", (submission_key,)
        )

    def _finalize_guard(self, brief: CreativeBrief, expected_hash: str,
                        expected_images_sha: str | None = None):
        def guard(conn, draft):
            run = conn.execute(
                "SELECT status, generation, account_id, brief_json FROM creator_agent_runs "
                "WHERE run_key=?",
                (brief.run_key,),
            ).fetchone()
            if run is None:
                raise DraftError("creator run disappeared before approval")
            if str(run["status"]) != "running":
                raise DraftError(f"run is {run['status']}; refusing to create a new approval")
            if int(run["generation"]) != int(brief.generation):
                raise DraftError("stale generation: refusing to create a new approval")
            if str(run["account_id"]) != str(brief.account_id):
                raise DraftError("account mismatch: refusing to create a new approval")
            if str(draft["submission_hash"] or "") != expected_hash:
                raise DraftError("submission package changed before approval; refusing")
            if expected_images_sha is not None:
                if str(draft["images_sha256"] or "") != expected_images_sha:
                    raise DraftError("final image manifest changed before approval; refusing")
            # Concurrency: only one live approval may exist for a draft.  The
            # unique submission key prevents duplicate drafts; this check
            # prevents two finalizers from creating two approval rows.
            existing = conn.execute(
                "SELECT code FROM approvals WHERE draft_id=? "
                "AND status IN ('pending','approved') ORDER BY id LIMIT 1",
                (int(draft["id"]),),
            ).fetchone()
            if existing is not None:
                raise DraftError(f"approval already exists for this draft: {existing['code']}")
        return guard

    def _existing_submission(self, brief: CreativeBrief, artifact: ArtifactVersion):
        draft_id = int(artifact.draft_id or 0)
        submission_key = self._submission_key(artifact)
        if not draft_id:
            draft = self._find_submission_draft(submission_key)
            if draft is not None:
                draft_id = int(draft["id"])
        if not draft_id:
            return None
        draft = self.db.fetchone("SELECT * FROM drafts WHERE id=?", (draft_id,))
        if draft is None:
            return None
        if str(draft["submission_key"] or "") != submission_key:
            return ToolResult(
                ok=False,
                error="submission package belongs to a different version; create a new artifact version",
            )
        current_hash = self._submission_hash(brief, artifact)
        if str(draft["submission_hash"] or "") != current_hash:
            return ToolResult(
                ok=False,
                error="submission package changed; refusing to reuse the old approval; create a new artifact version",
            )
        row = self.db.fetchone(
            "SELECT * FROM approvals WHERE draft_id=? ORDER BY id DESC LIMIT 1",
            (draft_id,),
        )
        if row is None:
            return None
        if str(row["status"]) not in {"pending", "approved"}:
            return ToolResult(
                ok=False,
                error=f"existing approval is {row['status']}; it cannot be resubmitted",
            )
        event = self.db.fetchone(
            "SELECT event_id FROM drafts WHERE id=?", (draft_id,)
        )
        self._backfill_lineage(
            draft,
            {"approval_id": int(row["id"]), "approval_code": str(row["code"])},
        )
        return ToolResult(ok=True, terminal="submit", data={
            "draft_id": draft_id,
            "approval_code": str(row["code"]),
            "event_id": int(event["event_id"]) if event is not None else None,
            "note": "existing submission reused; no duplicate approval",
        })

    def _lineage_topic_request(self, brief: CreativeBrief, *, event_id: int, version: int) -> int | None:
        """Create an explicit legacy lineage row for manual V2 work.

        Publish quota and pause gates intentionally remain the old authoritative
        path.  Scheduled V2 runs stay automatic; manual/revision runs get a
        real topic_request row instead of relying on event coincidence.
        """
        if brief.origin not in {"manual", "revision"}:
            return None
        message_id = f"v2-submit:{brief.run_key}:v{int(version)}"
        existing = self.db.fetchone(
            "SELECT id FROM topic_requests WHERE message_id=?", (message_id,)
        )
        if existing is not None:
            return int(existing["id"])
        code = "V2-" + sha256_text(f"{brief.run_key}|{version}")[:8].upper()
        now = to_iso(self.clock.now())
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO topic_requests(code, message_id, channel, sender_id, chat_id, chat_type, "
                "raw_text, command, topic, instruction, status, progress, day_key, event_id, "
                "created_at, updated_at, research_mode) "
                "VALUES (?, ?, 'feishu', 'creator-agent', NULL, 'p2p', ?, 'creator_agent', ?, ?, "
                "'completed', 'submitted for approval', ?, ?, ?, ?, ?)",
                (
                    code, message_id, brief.instruction or brief.topic, brief.topic,
                    brief.instruction or brief.topic, now[:10], int(event_id), now, now,
                    brief.research_mode,
                ),
            )
            return int(cur.lastrowid)

    def _backfill_lineage(self, draft, finalized) -> None:
        topic_request_id = draft["topic_request_id"] if "topic_request_id" in draft.keys() else None
        if topic_request_id is None:
            return
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE topic_requests SET draft_id=?, approval_id=?, result_code=?, "
                "progress=?, error=NULL, updated_at=? WHERE id=?",
                (
                    int(draft["id"]), int(finalized["approval_id"]),
                    str(finalized["approval_code"]), "恢复后已生成审批", to_iso(self.clock.now()),
                    int(topic_request_id),
                ),
            )

    def _resume_existing_draft(self, brief, artifact):
        """Finish a draft created before a crash, without inserting another."""
        draft = None
        if artifact is not None and artifact.draft_id:
            draft = self.db.fetchone("SELECT * FROM drafts WHERE id=?", (int(artifact.draft_id),))
        if draft is None and artifact is not None:
            draft = self._find_submission_draft(self._submission_key(artifact))
        if draft is None:
            return None
        current_key = self._submission_key(artifact)
        current_hash = self._submission_hash(brief, artifact)
        if str(draft["submission_key"] or "") != current_key:
            return ToolResult(
                ok=False,
                error="submission package belongs to a different version; create a new artifact version",
            )
        if not str(draft["submission_hash"] or ""):
            return ToolResult(
                ok=False,
                error="existing draft has no original submission package hash; refusing to resume",
            )
        if str(draft["submission_hash"]) != current_hash:
            return ToolResult(
                ok=False,
                error="submission package changed; refusing to resume the old draft; create a new artifact version",
            )
        artifact.draft_id = int(draft["id"])
        self.store.save_artifact(artifact)
        if str(draft["status"]) in {
            "published", "simulated", "exported", "publish_unknown",
            "superseded", "approval_invalidated", "revision_pending",
        }:
            return None
        approval = self.db.fetchone(
            "SELECT * FROM approvals WHERE draft_id=? ORDER BY id DESC LIMIT 1",
            (int(draft["id"]),),
        )
        if approval is not None:
            return self._existing_submission(brief, artifact)
        if not artifact.images:
            return None
        rendered = _persist_artifact_images(
            self.db, self.config, self.clock, draft, artifact.images
        )
        images_sha = images_digest([image.as_dict() for image in rendered])
        try:
            finalized = _finalize_with_approval(
                self.db, self.config, self.clock, draft_id=int(draft["id"]),
                event_id=int(draft["event_id"]), rendered=rendered,
                guard=self._finalize_guard(brief, current_hash, images_sha),
            )
        except DraftError as exc:
            if "approval already exists" in str(exc):
                existing = self._existing_submission(brief, artifact)
                if existing is not None:
                    return existing
            raise
        self._backfill_lineage(draft, finalized)
        try:
            self._record_submitted_visual(artifact)
        except Exception:
            pass
        artifact.status = "submitted"
        self.store.save_artifact(artifact)
        return ToolResult(ok=True, terminal="submit", data={
            "draft_id": int(draft["id"]),
            "approval_code": str(finalized["approval_code"]),
            "event_id": int(draft["event_id"]),
            "note": "resumed existing draft after crash; owner must approve",
        })

    def submit_for_approval(self, *, brief, artifact):
        if artifact is None or not artifact.content:
            return ToolResult(ok=False, error="no complete artifact content")
        run = self.store.get_run(brief.run_key)
        if run is None or int(run.get("generation") or 1) != int(brief.generation):
            return ToolResult(
                ok=False,
                error="stale generation: artifact was produced by an older run revision",
            )
        if str(run.get("status") or "") != "running":
            return ToolResult(
                ok=False,
                error=f"run is {run.get('status')}; it cannot submit a new approval",
            )
        submission_key = self._submission_key(artifact)
        submission_hash = self._submission_hash(brief, artifact)
        existing = self._existing_submission(brief, artifact)
        if existing is not None:
            return existing
        resumed = self._resume_existing_draft(brief, artifact)
        if resumed is not None:
            return resumed
        blocking = submission_issues(
            artifact, {m.material_id: m for m in self._materials.values()}
        )
        if blocking:
            return ToolResult(
                ok=False,
                error="submission blocked by deterministic checks: " + "; ".join(blocking[:5]),
            )
        if bool(self.config.get("creator_agent.semantic_review", False)):
            semantic = self.review_artifact(
                brief=brief, artifact=artifact, materials=list(self._materials.values())
            )
            if semantic.verdict != "pass":
                details = "; ".join(issue.evidence for issue in semantic.issues[:5])
                return ToolResult(
                    ok=False,
                    error="submission blocked by independent semantic review: " + details,
                )
        content = DraftContent.from_dict(artifact.content)
        entries = []
        for mid in (m.material_id for m in self._materials.values()):
            m = self._materials[mid]
            body = self._bodies.get(mid, "")
            entry = SourceEntry(title=m.title or _host(m.url), url=m.url, summary=m.quote,
                                content=body, author="", published_raw=m.published_at or "")
            entry.source_role = m.role
            entry.source_verified = m.role == "official"
            entries.append(entry)
        if not entries:
            return ToolResult(ok=False, error="no materials for approval")
        result = ingest_targeted_entries(
            self.db, self.config, self.clock, request_code=f"v2-{brief.run_key}",
            title=brief.topic, entries=entries, topic_hint=brief.topic, direct_url=None,
        )
        event = self.db.fetchone("SELECT * FROM events WHERE id=?", (int(result["event_id"]),))
        if event is None:
            return ToolResult(ok=False, error="event disappeared")
        scheduled, expires = _schedule(self.config, self.clock)
        topic_request_id = self._lineage_topic_request(
            brief, event_id=int(event["id"]), version=int(artifact.version)
        )
        existing_draft = self._find_submission_draft(submission_key)
        if existing_draft is not None:
            return ToolResult(
                ok=False,
                error=f"existing draft {existing_draft['id']} is {existing_draft['status']} and cannot be submitted again",
            )
        try:
            draft = _insert_draft(
                self.db, self.config, self.clock, event=event, content=content,
                status="drafted", scheduled_at=scheduled, expires_at=expires,
                topic_request_id=topic_request_id, research_mode=brief.research_mode,
                submission_key=submission_key, submission_hash=submission_hash,
                body_text=artifact.body,
            )
        except sqlite3.IntegrityError:
            # A concurrent submit won the unique submission-key race.
            draft = self._find_submission_draft(submission_key)
            if draft is None:
                raise
            existing = self._existing_submission(brief, artifact)
            if existing is not None:
                return existing
            resumed = self._resume_existing_draft(brief, artifact)
            if resumed is not None:
                return resumed
            return ToolResult(ok=False, error=f"existing draft {draft['id']} is {draft['status']} and cannot be resumed")
        # Persist the draft pointer before the render/finalize side effects so a
        # crash or retried submit can find the already-created draft.
        artifact.draft_id = int(draft["id"])
        try:
            self._record_submitted_visual(artifact)
        except Exception:
            pass
        artifact.status = "submitted"
        self.store.save_artifact(artifact)
        if not artifact.images:
            return ToolResult(
                ok=False,
                error="submission blocked: artifact has no final rendered images",
            )
        rendered = _persist_artifact_images(
            self.db, self.config, self.clock, draft, artifact.images
        )
        images_sha = images_digest([image.as_dict() for image in rendered])
        try:
            finalized = _finalize_with_approval(
                self.db, self.config, self.clock, draft_id=int(draft["id"]),
                event_id=int(event["id"]), rendered=rendered,
                guard=self._finalize_guard(brief, submission_hash, images_sha),
            )
        except DraftError as exc:
            if "approval already exists" in str(exc):
                existing = self._existing_submission(brief, artifact)
                if existing is not None:
                    return existing
            raise
        if topic_request_id is not None:
            with self.db.tx() as conn:
                conn.execute(
                    "UPDATE topic_requests SET draft_id=?, approval_id=?, result_code=?, "
                    "updated_at=? WHERE id=?",
                    (int(draft["id"]), int(finalized["approval_id"]),
                     str(finalized["approval_code"]), to_iso(self.clock.now()),
                     int(topic_request_id)),
                )
        try:
            self._record_submitted_visual(artifact)
        except Exception:
            pass
        artifact.status = "submitted"
        self.store.save_artifact(artifact)
        return ToolResult(ok=True, terminal="submit", data={
            "draft_id": draft["id"], "approval_code": finalized["approval_code"],
            "event_id": event["id"], "note": "real pending approval created; owner must approve",
        })

    def ask_owner(self, question: str):
        return None

    def _account_id(self):
        try:
            return str(self.config.account().get("id", "xhs-main"))
        except Exception:
            return "xhs-main"


"""Draft schema, validation and canonical body rendering.

The writer adapter only supplies structured text.  This module is the policy
boundary: it validates a caption and an independent image storyboard, an
editorial plan that explains the chosen format, the title limit, banned
first-person/marketing phrases, citation of every factual claim (including
statements/numbers shown on image cards), and a numeric consistency check that
requires each number to appear in cited evidence.

The plan deliberately does not prescribe one house template.  ``strategy`` is
``image_led``, ``caption_led`` or ``balanced``, and the visual vocabulary is a
set of reusable primitives rather than a required sequence.  A storyboard may
contain only a cover.  Length is bounded by publish/render limits, not by a
universal caption-to-image ratio.

Old drafts remain readable: a body_json without a storyboard deserializes as a
legacy section-only draft and keeps its original deterministic rendering.  New
generation requires the storyboard/caption schema, so the renderer never has to
turn the full caption into images.
"""

# =============================================================================
# 中文导读：这是稿件的“编辑审稿人”
# =============================================================================
# 大模型写出来的东西不能直接相信。这个文件不负责“写”，只负责“验收”：
#
#   1. 结构：新稿必须有正文 caption、editorial_plan 和独立 storyboard；
#      不再强制四个可见章节，卡片数量也可以只有一张封面。
#   2. 标题：不能太长、不能换行。
#   3. 事实：每条 fact / vendor_claim 以及每张图片上的事实，都必须给出
#      证据编号，而且编号必须真实存在。没有证据的断言不允许出现。
#   4. 数字：标题、正文和图片里出现的每个数字，都必须能在被引用的
#      证据原文里找到。这样防止模型凭空编出“提升 300%”之类的数字。
#   5. 话术：禁止第一人称假体验（“我亲测”）、禁止“保证赚钱/涨粉”这类
#      承诺性表达，也禁止配置里列出的其他词。
#   旧稿没有 caption/storyboard 时仍按原来的章节结构读取和渲染。
#
# 生活化类比：
#   模型是“实习生写稿”，这个文件是“编辑复核”。实习生可以说错，但编辑
#   必须对着资料逐条核对，核对不过就不许发。
#
# 为什么正文要“确定性渲染”？
#   render_body() 的输出同时用于：算哈希、给用户预览、最终发布。
#   同一份结构化内容必须每次渲染出完全相同的文本，否则审批单的指纹
#   会对不上——用户明明没改，系统却认为内容变了。
# =============================================================================

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from typing import Any

from .adapters.types import LLMOutputError
from .config import Config

SECTION_HEADINGS = {
    "what": "发生了什么",
    "audience": "对谁有用",
    "how": "具体怎么用",
    "limits": "限制与不确定性",
}
SECTION_ORDER = ["what", "audience", "how", "limits"]
CLAIM_KINDS = {"fact", "vendor_claim", "analysis"}
DEFAULT_LIMITS = "以上为公开资料整理，未做个人实测；具体功能、价格和可用性以官方页面为准。"
# Reusable local visual primitives.  They do not dictate editorial structure:
# the model may use one cover alone, or pick any combination that fits the topic.
IMAGE_LAYOUTS = {
    "cover",
    "comparison",
    "process",
    "keypoints",
    "diagram",
    "argument",
    "annotated",
}
CONTENT_LAYOUTS = IMAGE_LAYOUTS - {"cover"}
EDITORIAL_STRATEGIES = {"image_led", "caption_led", "balanced"}
# A process layout is only appropriate when the source actually describes a
# sequence.  The renderer therefore draws numbered steps without arrowheads; a
# memory architecture must not be turned into a fake pipeline just to fit a card.
SEQUENCE_HINTS = re.compile(
    r"(?:首先|其次|然后|接着|最后|第一步|步骤|流程|先.+再|"
    r"(?:^|\s)\d{1,2}[.、)]|step|first|then|finally)",
    re.I,
)

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?%?")
_FIRST_PERSON_RE = re.compile(
    r"我(?:亲测|实测|试用|体验|买了|赚了|靠这个|亲身)|亲自(?:测试|试用|体验)"
)
# Fabricated engagement metrics are facts about the world that no evidence in
# this pipeline can support.  They are rejected deterministically.
_ENGAGEMENT_RE = re.compile(
    r"(?:点赞|收藏|转发|评论|阅读|播放|粉丝|涨粉|流量)[^。\n]{0,10}"
    r"(?:破|过|超|万|亿|爆|飙升|暴涨|起飞)"
)


@dataclass
class Section:
    # 一个章节：key 是内部标识，heading 是展示标题，paragraphs 是段落列表。
    # key 不直接展示给读者，所以模型不能靠改标题来伪装章节。
    key: str
    heading: str
    paragraphs: list[str]

    def as_dict(self) -> dict:
        # 转成可 JSON 序列化的字典，存入 drafts.body_json。
        return {"key": self.key, "heading": self.heading, "paragraphs": list(self.paragraphs)}


@dataclass
class Claim:
    # 一条“主张”。kind 分三种：
    #   fact         可核实的事实
    #   vendor_claim 厂商自己说的（必须带“厂商称”前缀）
    #   analysis     作者的分析/观点（不要求证据）
    # evidence_ids 指向 evidence 表的记录，是事实的“出处”。
    text: str
    kind: str
    evidence_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"text": self.text, "type": self.kind, "evidence_ids": list(self.evidence_ids)}


@dataclass
class SourceRef:
    # 文末“来源”列表里的一条。date_precision 决定日期怎么显示：
    # time -> 精确到分钟；date -> 只显示日期；unknown -> 写“发布时间未知”。
    # 为什么要区分？有些 RSS 只给日期，硬编一个时间就是造假。
    evidence_id: str
    name: str
    url: str
    published_at: str | None = None
    date_precision: str = "unknown"
    fetched_at: str | None = None

    def as_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "name": self.name,
            "url": self.url,
            "published_at": self.published_at,
            "date_precision": self.date_precision,
            "fetched_at": self.fetched_at,
        }


@dataclass
class ImageColumn:
    """One side of a comparison card."""

    label: str
    text: str

    def as_dict(self) -> dict:
        return {"label": self.label, "text": self.text}

    @classmethod
    def from_dict(cls, data: dict) -> "ImageColumn":
        return cls(label=str(data.get("label", "")), text=str(data.get("text", "")))


@dataclass
class ImageCard:
    """A concise, independently authored visual card.

    ``layout`` controls actual local geometry: comparison gets two columns,
    process gets numbered steps with connectors, keypoints gets a numbered list,
    and cover gets the large hook treatment.  ``evidence_ids`` point at the
    evidence supporting any factual statement/illustrated number on the card.
    """

    layout: str
    headline: str
    kicker: str = ""
    subhead: str = ""
    points: list[str] = field(default_factory=list)
    columns: list[ImageColumn] = field(default_factory=list)
    note: str = ""
    evidence_ids: list[str] = field(default_factory=list)

    def text_parts(self) -> list[str]:
        parts = [self.headline, self.kicker, self.subhead, self.note]
        parts.extend(self.points)
        for column in self.columns:
            parts.extend([column.label, column.text])
        return [part for part in parts if part]

    def text(self) -> str:
        return " ".join(self.text_parts())

    def as_dict(self) -> dict:
        return {
            "layout": self.layout,
            "headline": self.headline,
            "kicker": self.kicker,
            "subhead": self.subhead,
            "points": list(self.points),
            "columns": [column.as_dict() for column in self.columns],
            "note": self.note,
            "evidence_ids": list(self.evidence_ids),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ImageCard":
        columns = [
            ImageColumn.from_dict(item)
            for item in data.get("columns", [])
            if isinstance(item, dict)
        ]
        return cls(
            layout=str(data.get("layout", "")),
            headline=str(data.get("headline", "")),
            kicker=str(data.get("kicker", "")),
            subhead=str(data.get("subhead", "")),
            points=[str(item) for item in data.get("points", []) if str(item).strip()],
            columns=columns,
            note=str(data.get("note", "")),
            evidence_ids=[str(item) for item in data.get("evidence_ids", [])],
        )


@dataclass
class ImageStoryboard:
    """Persisted image plan, deliberately separate from the caption/body."""

    cover: ImageCard
    cards: list[ImageCard] = field(default_factory=list)

    def all_cards(self) -> list[ImageCard]:
        return [self.cover] + list(self.cards)

    def text(self) -> str:
        return " ".join(card.text() for card in self.all_cards())

    def as_dict(self) -> dict:
        return {
            "cover": self.cover.as_dict(),
            "cards": [card.as_dict() for card in self.cards],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ImageStoryboard | None":
        if not isinstance(data, dict):
            return None
        raw_cover = data.get("cover")
        if not isinstance(raw_cover, dict):
            return None
        cover = ImageCard.from_dict(raw_cover)
        cover.layout = "cover"
        cards = [
            ImageCard.from_dict(item)
            for item in data.get("cards", [])
            if isinstance(item, dict)
        ]
        return cls(cover=cover, cards=cards)


@dataclass
class EditorialPlan:
    """Why this post exists and how caption and images divide the work."""

    audience_need: str
    angle: str
    takeaway: str
    caption_image_division: str
    rationale: str
    strategy: str = ""
    visual_approach: str = ""

    def as_dict(self) -> dict:
        return {
            "audience_need": self.audience_need,
            "angle": self.angle,
            "takeaway": self.takeaway,
            "caption_image_division": self.caption_image_division,
            "rationale": self.rationale,
            "strategy": self.strategy,
            "visual_approach": self.visual_approach,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EditorialPlan":
        # Old plans lack strategy/visual_approach; they remain readable and are
        # never retroactively rewritten in an immutable approval.
        return cls(
            audience_need=str(data.get("audience_need", "")),
            angle=str(data.get("angle", "")),
            takeaway=str(data.get("takeaway", "")),
            caption_image_division=str(data.get("caption_image_division", "")),
            rationale=str(data.get("rationale", "")),
            strategy=str(data.get("strategy", "")),
            visual_approach=str(data.get("visual_approach", "")),
        )


def storyboard_sha256(storyboard: ImageStoryboard | None) -> str:
    """Stable digest of the structured image plan (empty for legacy drafts)."""

    if storyboard is None:
        return ""
    import json

    from .security import sha256_text

    canonical = json.dumps(
        storyboard.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return sha256_text(canonical)


@dataclass
class DraftContent:
    # 一份通过校验的完整稿件。它已经不是“模型原始输出”，而是被清洗、
    # 校验、补全过的结构化对象。后面所有环节都只认这个对象。
    title: str
    sections: list[Section]
    claims: list[Claim]
    tags: list[str]
    uncertainties: list[str]
    sources: list[SourceRef]
    generator: str = "unknown"
    model: str = ""
    caption: list[str] = field(default_factory=list)
    editorial_plan: EditorialPlan | None = None
    storyboard: ImageStoryboard | None = None

    def section(self, key: str) -> Section | None:
        # 按 key 找章节；找不到返回 None，调用方自行决定是否报错。
        for item in self.sections:
            if item.key == key:
                return item
        return None

    def factual_claims(self) -> list[Claim]:
        # 只挑“需要证据”的主张；analysis（分析）不需要。
        return [c for c in self.claims if c.kind in {"fact", "vendor_claim"}]

    def as_dict(self) -> dict:
        # 完整快照，用于存库和算正文哈希。
        return {
            "title": self.title,
            "sections": [s.as_dict() for s in self.sections],
            "claims": [c.as_dict() for c in self.claims],
            "tags": list(self.tags),
            "uncertainties": list(self.uncertainties),
            "sources": [s.as_dict() for s in self.sources],
            "generator": self.generator,
            "model": self.model,
            "caption": list(self.caption),
            "editorial_plan": self.editorial_plan.as_dict() if self.editorial_plan else None,
            "storyboard": self.storyboard.as_dict() if self.storyboard else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DraftContent":
        # 从数据库里的 body_json 还原对象。
        # 这里对每一项都做了防御：不是字典就跳过、缺字段就用默认值。
        # 因为数据库里可能存着旧版本、手工改过的 JSON，不能假设它完美。
        sections = [
            Section(
                key=str(item.get("key", "")),
                heading=str(item.get("heading", "")),
                paragraphs=[str(p) for p in item.get("paragraphs", [])],
            )
            for item in data.get("sections", [])
            if isinstance(item, dict)
        ]
        claims = [
            Claim(
                text=str(item.get("text", "")),
                kind=str(item.get("type", "analysis")),
                evidence_ids=[str(x) for x in item.get("evidence_ids", [])],
            )
            for item in data.get("claims", [])
            if isinstance(item, dict)
        ]
        sources = [
            SourceRef(
                evidence_id=str(item.get("evidence_id", "")),
                name=str(item.get("name", "")),
                url=str(item.get("url", "")),
                published_at=item.get("published_at"),
                date_precision=str(item.get("date_precision", "unknown")),
                fetched_at=item.get("fetched_at"),
            )
            for item in data.get("sources", [])
            if isinstance(item, dict) and item.get("url")
        ]
        raw_plan = data.get("editorial_plan")
        editorial_plan = EditorialPlan.from_dict(raw_plan) if isinstance(raw_plan, dict) else None
        raw_storyboard = data.get("storyboard")
        storyboard = ImageStoryboard.from_dict(raw_storyboard) if isinstance(raw_storyboard, dict) else None
        caption = [
            str(item)
            for item in data.get("caption", [])
            if isinstance(item, (str, int, float)) and str(item).strip()
        ]
        return cls(
            title=str(data.get("title", "")),
            sections=sections,
            claims=claims,
            tags=[str(t) for t in data.get("tags", [])],
            uncertainties=[str(u) for u in data.get("uncertainties", [])],
            sources=sources,
            generator=str(data.get("generator", "unknown")),
            model=str(data.get("model", "")),
            caption=caption,
            editorial_plan=editorial_plan,
            storyboard=storyboard,
        )

    def render_body(self, *, include_sources: bool = True) -> str:
        """Deterministic body used for hashing, preview and publishing.

        New drafts render their substantive caption without forcing four visible
        section headings.  Legacy drafts keep the original section rendering so
        an old immutable approval never changes.  ``include_sources=False`` is
        used for the public XHS body: links remain in structured metadata and
        the private approval card, but are not pasted into the post.
        """
        blocks: list[str] = []
        if self.caption:
            blocks.extend(self.caption)
        else:
            for key in SECTION_ORDER:
                section = self.section(key)
                if section is None:
                    continue
                blocks.append(f"【{section.heading}】")
                blocks.extend(section.paragraphs)
                blocks.append("")
        if include_sources:
            blocks.append("【来源】")
            for index, source in enumerate(self.sources, start=1):
                date = self._source_date_label(source)
                blocks.append(f"{index}. {source.name}{date}")
                blocks.append(source.url)
        body = "\n".join(blocks).strip()
        return re.sub(r"\n{3,}", "\n\n", body)

    @staticmethod
    def _source_date_label(source: SourceRef) -> str:
        # 根据日期精度选择展示方式。宁可写“发布时间未知”，也不编一个时间。
        if not source.published_at:
            return "（发布时间未知）"
        if source.date_precision == "date":
            return f"（{source.published_at[:10]}）"
        if source.date_precision == "unknown":
            return "（发布时间未知）"
        return f"（{source.published_at[:19].replace('T', ' ')} UTC）"


def _fit_title(title: str, limit: int) -> str:
    """Fit a title to the platform cap without failing the whole draft.

    Xiaohongshu's title field is capped at 20 characters; that platform limit
    is not negotiable.  A model occasionally returns 21-22 characters, which
    used to abort an otherwise valid draft.  Shorten deterministically with a
    single ellipsis character instead of spending another paid model call.
    """
    if limit <= 0:
        return title[:0]
    if len(title) <= limit:
        return title
    if limit == 1:
        return title[:1]
    body = title[: limit - 1].rstrip()
    return (body + "…") if body else title[:1]


def _fit_rendered_body(content: DraftContent, limit: int, *, include_sources: bool = True) -> None:
    """Keep an over-limit V2 draft approvable instead of aborting the run.

    This is deliberately only used for creator-agent drafts.  The full body is
    still rendered; when it exceeds the platform limit we shorten only the
    caption and append an explicit note.  Sources and the evidence checks are
    untouched.  The owner can reject or revise the result.
    """
    body = content.render_body(include_sources=include_sources)
    if len(body) <= limit:
        return
    if not content.caption:
        raise LLMOutputError(
            f"rendered body is {len(body)} chars, limit is {limit}; "
            "legacy section body cannot be soft-trimmed"
        )
    warning = "注：正文超过平台上限，已自动压缩；请审批时核对关键信息。"
    original = list(content.caption)
    for keep in range(len(original), 0, -1):
        content.caption = original[:keep] + [warning]
        if len(content.render_body(include_sources=include_sources)) <= limit:
            content.uncertainties.append(warning)
            return

    # Even the first paragraph plus the warning is too long.  Reserve the
    # source block (if it is public) and trim at a sentence boundary.
    content.caption = []
    source_only = content.render_body(include_sources=include_sources)
    available = limit - len(source_only) - 1 - len(warning) - 1
    if available < 24:
        raise LLMOutputError(
            f"rendered body is {limit + 1}+ chars and source block leaves no "
            "safe caption space"
        )
    text = original[0]
    cut = text[:available].rstrip()
    boundary = max(cut.rfind("。"), cut.rfind("！"), cut.rfind("？"), cut.rfind("；"))
    if boundary >= 24:
        cut = cut[: boundary + 1]
    else:
        cut = cut.rstrip("，,、；; ") + "…"
    content.caption = [cut, warning]
    content.uncertainties.append(warning)
    if len(content.render_body(include_sources=include_sources)) > limit:
        raise LLMOutputError(
            f"rendered body is {len(content.render_body(include_sources=include_sources))} chars, "
            f"limit is {limit}; soft trim could not fit the source block"
        )


def validate_draft_payload(
    payload: dict,
    *,
    evidence_by_id: dict[str, dict],
    config: Config,
    banned_extra: list[str] | None = None,
    date_unknown: bool = False,
    generator: str = "",
    model: str = "",
    require_storyboard: bool = False,
    soft_body_limit: bool = False,
    include_sources_in_body: bool = True,
    public_disclaimers: bool = True,
    repair_caption_claims: bool = False,
    owner_context: str = "",
) -> DraftContent:
    # 校验入口：按“先便宜后昂贵”的顺序检查，尽早失败、少花力气。
    # 任何一条不满足都抛 LLMOutputError，上层会把这次生成记为失败，
    # 不会进入审批，更不会发布。
    if not isinstance(payload, dict):
        raise LLMOutputError("writer output must be a JSON object")
    title = _clean_line(payload.get("title"), "title")
    title_max = int(config.get("writer.title_max_chars", 20))
    title = _fit_title(title, title_max)
    if "\n" in title:
        raise LLMOutputError("title must be a single line")

    new_schema = payload.get("caption") is not None or payload.get("storyboard") is not None
    if require_storyboard and not new_schema:
        raise LLMOutputError(
            "new drafts require a caption and an independent image storyboard; "
            "the legacy four-section body is not accepted for new generation"
        )
    if new_schema and payload.get("caption") is None:
        raise LLMOutputError("caption is required when a storyboard is supplied")
    if new_schema and payload.get("storyboard") is None:
        raise LLMOutputError("storyboard is required when a caption is supplied")

    by_key: dict[str, Section] = {}
    caption: list[str] = []
    caption_warnings: list[str] = []
    editorial_plan: EditorialPlan | None = None
    storyboard: ImageStoryboard | None = None

    if new_schema:
        raw_caption = payload.get("caption")
        if not isinstance(raw_caption, list) or not raw_caption:
            raise LLMOutputError("caption must be a non-empty list of paragraphs")
        if len(raw_caption) > 8:
            raise LLMOutputError("caption has too many paragraphs")
        caption = [_clean_paragraph(item) for item in raw_caption]
        caption = [item for item in caption if item]
        if not caption:
            raise LLMOutputError("caption has no usable paragraphs")
        if public_disclaimers:
            _apply_disclaimers(caption, date_unknown)
        raw_plan = payload.get("editorial_plan")
        if not isinstance(raw_plan, dict):
            raise LLMOutputError("editorial_plan is required for a new draft")
        editorial_plan = _validate_editorial_plan(raw_plan)
        claims = _validate_claims(payload.get("claims"), evidence_by_id)
        storyboard = _validate_storyboard(
            payload.get("storyboard"),
            evidence_by_id=evidence_by_id,
            claims=claims,
            config=config,
        )
        # Every number in the caption/title must be supported by evidence cited
        # somewhere in this draft (claims or image cards).
        cited_ids = {eid for claim in claims for eid in claim.evidence_ids}
        if storyboard is not None:
            cited_ids.update(
                eid for card in storyboard.all_cards() for eid in card.evidence_ids
            )
        _check_numbers(
            title, "title", evidence_by_id, claims, evidence_ids=sorted(cited_ids)
        )
        if repair_caption_claims:
            caption, removed, owner_preserved = _repair_unsupported_caption(
                caption,
                evidence_by_id,
                claims,
                evidence_ids=sorted(cited_ids),
                owner_context=owner_context,
            )
            if removed:
                preview = "；".join(item[:80] for item in removed[:2])
                caption_warnings.append(
                    "审批前自动删除无法由当前证据支持的日期/优惠表述："
                    f"{preview}。请重点核对。"
                )
            if owner_preserved:
                preview = "；".join(item[:80] for item in owner_preserved[:2])
                caption_warnings.append(
                    "以下日期/优惠来自选题或修改说明，未在抓取来源中核实："
                    f"{preview}。请重点核对。"
                )
        _check_numbers(
            " ".join(caption),
            "caption",
            evidence_by_id,
            claims,
            evidence_ids=sorted(cited_ids),
            extra_text=owner_context,
        )
        sections = []
    else:
        raw_sections = payload.get("sections")
        if not isinstance(raw_sections, list):
            raise LLMOutputError("sections must be a list")
        for raw in raw_sections:
            if not isinstance(raw, dict):
                raise LLMOutputError("each section must be an object")
            key = str(raw.get("key", "")).strip()
            if key not in SECTION_HEADINGS:
                raise LLMOutputError(f"unknown section key: {key!r}")
            if key in by_key:
                raise LLMOutputError(f"duplicate section: {key}")
            raw_paragraphs = raw.get("paragraphs")
            if not isinstance(raw_paragraphs, list) or not raw_paragraphs:
                raise LLMOutputError(f"section {key} needs paragraphs")
            paragraphs = [_clean_paragraph(p) for p in raw_paragraphs]
            paragraphs = [p for p in paragraphs if p]
            if not paragraphs:
                raise LLMOutputError(f"section {key} has no usable paragraphs")
            if len(paragraphs) > 4:
                raise LLMOutputError(f"section {key} has too many paragraphs")
            by_key[key] = Section(key=key, heading=SECTION_HEADINGS[key], paragraphs=paragraphs)
        missing = [k for k in SECTION_ORDER if k not in by_key]
        if missing:
            raise LLMOutputError(f"missing sections: {', '.join(missing)}")
        if public_disclaimers:
            _apply_disclaimers(by_key["limits"].paragraphs, date_unknown)
        claims = _validate_claims(payload.get("claims"), evidence_by_id)
        _check_numbers(title, "title", evidence_by_id, claims)
        _check_numbers(" ".join(by_key["what"].paragraphs), "发生了什么", evidence_by_id, claims)
        sections = [by_key[k] for k in SECTION_ORDER]

    tags = payload.get("tags", [])
    # 标签最多 6 个、每个最多 20 字，自动去掉开头的 # 并去重。
    # 限制数量是为了让平台展示时不至于挤成一团。
    if not isinstance(tags, list):
        raise LLMOutputError("tags must be a list")
    clean_tags: list[str] = []
    for tag in tags[:6]:
        text = _clean_line(tag, "tag").lstrip("#").strip()
        if text and text not in clean_tags:
            clean_tags.append(text[:20])

    uncertainties = payload.get("uncertainties", [])
    if not isinstance(uncertainties, list):
        raise LLMOutputError("uncertainties must be a list")
    clean_uncertainties = [_clean_paragraph(u) for u in uncertainties[:6]]
    clean_uncertainties = [u for u in clean_uncertainties if u]
    if caption_warnings:
        clean_uncertainties = caption_warnings + clean_uncertainties
    if len(clean_uncertainties) > 6:
        clean_uncertainties = clean_uncertainties[:6]
    if not public_disclaimers and DEFAULT_LIMITS not in clean_uncertainties:
        # Keep the caveat in the private approval metadata, not in the public
        # XHS caption.
        clean_uncertainties.append(DEFAULT_LIMITS)

    sources = _sources_from_evidence(evidence_by_id, claims, storyboard=storyboard)
    # 没有来源就不允许成稿——哪怕正文写得再好，也无法让读者核实。
    if not sources:
        raise LLMOutputError("no evidence sources available for a draft")

    content = DraftContent(
        title=title,
        sections=sections,
        claims=claims,
        tags=clean_tags,
        uncertainties=clean_uncertainties,
        sources=sources,
        generator=generator,
        model=model,
        caption=caption,
        editorial_plan=editorial_plan,
        storyboard=storyboard,
    )

    banned = list(config.get("writer.banned_phrases", []) or [])
    # banned_extra 是“账号记忆”里学到的东西：比如用户上次驳回时说过
    # “别用‘颠覆’这个词”，这次就额外禁用。配置里的禁用词是长期规则，
    # banned_extra 是临时追加，两者合并检查。
    if banned_extra:
        banned.extend(banned_extra)
    _check_banned(content, banned)

    body = content.render_body(include_sources=include_sources_in_body)
    # 长度按实际发布正文计算。旧链路把来源列表放在正文里；V2 的公开正文不带
    # 来源 URL，但结构化来源仍保存在 body_json，审批卡可单独展示。
    body_max = int(config.get("writer.body_max_chars", 900))
    if len(body) > body_max:
        if not soft_body_limit:
            raise LLMOutputError(f"rendered body is {len(body)} chars, limit is {body_max}")
        _fit_rendered_body(
            content, body_max, include_sources=include_sources_in_body
        )
    # No universal caption minimum and no caption-to-image ratio.  A short
    # caption with a substantial image sequence and a long caption with one
    # cover are both legitimate; the editorial plan explains the choice and
    # revision review checks it against the actual artifacts.
    return content


def _apply_disclaimers(paragraphs: list[str], date_unknown: bool) -> None:
    # Keep the necessary uncertainty text without forcing visible boilerplate
    # headings.  This is appended in place and participates in the body hash.
    if date_unknown and not any("未知" in p or "无法确认" in p for p in paragraphs):
        paragraphs.append("部分来源未标注发布时间，不能确认是今日新闻。")
    if not any("未" in p and ("实测" in p or "亲测" in p) for p in paragraphs):
        paragraphs.append(DEFAULT_LIMITS)


def _validate_editorial_plan(raw: dict) -> EditorialPlan:
    fields = {
        key: _clean_paragraph(raw.get(key))
        for key in (
            "audience_need",
            "angle",
            "takeaway",
            "caption_image_division",
            "rationale",
            "visual_approach",
        )
    }
    missing = [key for key, value in fields.items() if not value]
    if missing:
        raise LLMOutputError(f"editorial_plan is missing: {', '.join(missing)}")
    strategy = str(raw.get("strategy", "")).strip().lower()
    if strategy not in EDITORIAL_STRATEGIES:
        raise LLMOutputError(
            f"editorial_plan.strategy must be one of {sorted(EDITORIAL_STRATEGIES)}"
        )
    for key, value in fields.items():
        if len(value) > 300:
            raise LLMOutputError(f"editorial_plan.{key} is too long")
    # This field is a compact decision label, not a second essay; the detail
    # belongs in rationale.  It is a display bound, not an editorial ratio.
    if len(fields["caption_image_division"]) > 120:
        raise LLMOutputError("editorial_plan.caption_image_division is too long")
    if len(fields["visual_approach"]) > 120:
        raise LLMOutputError("editorial_plan.visual_approach is too long")
    return EditorialPlan(**fields, strategy=strategy)


def _validate_card(
    raw: Any,
    *,
    label: str,
    evidence_by_id: dict[str, dict],
    claims: list[Claim],
    allow_cover: bool,
) -> ImageCard:
    if not isinstance(raw, dict):
        raise LLMOutputError(f"{label} must be an object")
    layout = str(raw.get("layout", "")).strip()
    if layout not in IMAGE_LAYOUTS:
        raise LLMOutputError(f"{label} layout must be one of {sorted(IMAGE_LAYOUTS)}")
    if allow_cover and layout != "cover":
        raise LLMOutputError("storyboard cover must use layout 'cover'")
    if not allow_cover and layout == "cover":
        raise LLMOutputError("content card cannot use layout 'cover'")
    headline = _clean_line(raw.get("headline"), f"{label}.headline")
    kicker = _clean_paragraph(raw.get("kicker"))
    subhead = _clean_paragraph(raw.get("subhead"))
    note = _clean_paragraph(raw.get("note"))
    points = [_clean_paragraph(item) for item in (raw.get("points") or [])]
    points = [item for item in points if item]
    raw_columns = raw.get("columns") or []
    if not isinstance(raw_columns, list):
        raise LLMOutputError(f"{label}.columns must be a list")
    columns = [
        ImageColumn(
            label=_clean_line(item.get("label"), f"{label}.column.label"),
            text=_clean_paragraph(item.get("text")),
        )
        for item in raw_columns
        if isinstance(item, dict)
    ]
    if len(columns) != len(raw_columns):
        raise LLMOutputError(f"{label}.columns must contain objects")
    ids = raw.get("evidence_ids", [])
    if not isinstance(ids, list):
        raise LLMOutputError(f"{label}.evidence_ids must be a list")
    ids = [str(item) for item in ids]
    missing = [item for item in ids if item not in evidence_by_id]
    if missing:
        raise LLMOutputError(f"{label} cites unknown evidence: {missing}")

    # These are rendering bounds (a card must fit one local PNG), not an
    # editorial mandate that every post use short image text.  A caption-led
    # post may still choose a single short cover.
    limits = {
        "headline": 44,
        "kicker": 20,
        "subhead": 140,
        "note": 100,
        "point": 90,
        "column_label": 24,
        "column_text": 120,
    }
    if len(headline) > limits["headline"]:
        raise LLMOutputError(f"{label}.headline is too long for one card")
    if len(kicker) > limits["kicker"]:
        raise LLMOutputError(f"{label}.kicker is too long")
    if len(subhead) > limits["subhead"]:
        raise LLMOutputError(f"{label}.subhead is too long for one card")
    if len(note) > limits["note"]:
        raise LLMOutputError(f"{label}.note is too long for one card")
    if len(points) > 6:
        raise LLMOutputError(f"{label} has too many points")
    if any(len(point) > limits["point"] for point in points):
        raise LLMOutputError(f"{label} point is too long for one card")
    if any(len(column.label) > limits["column_label"] for column in columns):
        raise LLMOutputError(f"{label} comparison label is too long")
    if any(
        not column.text or len(column.text) > limits["column_text"] for column in columns
    ):
        raise LLMOutputError(f"{label} comparison text is missing or too long")

    if layout == "comparison":
        if len(columns) != 2:
            raise LLMOutputError(f"{label} comparison needs exactly two columns")
        if points:
            raise LLMOutputError(f"{label} comparison must not also carry a points list")
    elif columns:
        raise LLMOutputError(f"{label} columns are only valid for comparison")
    if layout == "process":
        if len(points) < 2:
            raise LLMOutputError(f"{label} process needs at least two steps")
        if not SEQUENCE_HINTS.search(" ".join([headline, subhead, *points])):
            raise LLMOutputError(
                f"{label} uses a process layout without source-supported sequence language; "
                "use diagram/keypoints instead of inventing a pipeline"
            )
    if layout in {"keypoints", "diagram", "argument"} and not points:
        raise LLMOutputError(f"{label} {layout} needs at least one point")
    if layout == "annotated":
        if not subhead:
            raise LLMOutputError(f"{label} annotated needs a source excerpt in subhead")
        if not points:
            raise LLMOutputError(f"{label} annotated needs at least one note")
    if layout == "cover" and (points or columns):
        raise LLMOutputError(f"{label} cover must use headline/subhead, not points/columns")

    card = ImageCard(
        layout=layout,
        headline=headline,
        kicker=kicker,
        subhead=subhead,
        points=points,
        columns=columns,
        note=note,
        evidence_ids=ids,
    )
    # Image statements are public claims too.  Require provenance even for a
    # cover: a citation-free hook could otherwise become an unsupported fact.
    if not ids:
        raise LLMOutputError(f"{label} needs evidence_ids for its image statements")
    has_number = bool(_NUMBER_RE.search(unicodedata.normalize("NFKC", card.text())))
    if has_number and not ids:
        raise LLMOutputError(f"{label} shows a number without cited evidence")
    _check_numbers(
        card.text(),
        label,
        evidence_by_id,
        claims,
        evidence_ids=ids or None,
    )
    return card


def _validate_storyboard(
    raw: Any,
    *,
    evidence_by_id: dict[str, dict],
    claims: list[Claim],
    config: Config,
) -> ImageStoryboard:
    if not isinstance(raw, dict):
        raise LLMOutputError("storyboard must be an object")
    max_cards = int(config.get("writer.storyboard_max_cards", 4))
    raw_cards = raw.get("cards", [])
    if not isinstance(raw_cards, list):
        raise LLMOutputError("storyboard.cards must be a list")
    # One cover can be the whole visual plan.  Card count is a render bound,
    # never a required editorial structure.
    if len(raw_cards) > max_cards:
        raise LLMOutputError(
            f"storyboard has {len(raw_cards)} content cards, limit is {max_cards}"
        )
    cover = _validate_card(
        raw.get("cover"),
        label="storyboard.cover",
        evidence_by_id=evidence_by_id,
        claims=claims,
        allow_cover=True,
    )
    cards = [
        _validate_card(
            item,
            label=f"storyboard.cards[{index}]",
            evidence_by_id=evidence_by_id,
            claims=claims,
            allow_cover=False,
        )
        for index, item in enumerate(raw_cards)
    ]
    storyboard = ImageStoryboard(cover=cover, cards=cards)
    total_max = int(config.get("writer.storyboard_max_chars", 900))
    if len(storyboard.text()) > total_max:
        raise LLMOutputError(
            f"storyboard text is {len(storyboard.text())} chars, limit is {total_max}; "
            "images must stay a concise visual summary"
        )
    return storyboard


def _validate_claims(raw_claims: Any, evidence_by_id: dict[str, dict]) -> list[Claim]:
    # 规则：
    #   - 至少一条主张，最多 10 条（防止模型灌水）；
    #   - 每条主张的 type 必须是 fact / vendor_claim / analysis 之一；
    #   - fact 和 vendor_claim 必须至少引用一条“真实存在”的证据；
    #   - vendor_claim 如果没以“厂商称/官方称/公司称”开头，自动补前缀，
    #     让读者一眼看出这是厂商自己的说法，不是独立事实。
    if not isinstance(raw_claims, list) or not raw_claims:
        raise LLMOutputError("writer must cite at least one claim")
    if len(raw_claims) > 10:
        raise LLMOutputError("too many claims")
    claims: list[Claim] = []
    for raw in raw_claims:
        if not isinstance(raw, dict):
            raise LLMOutputError("each claim must be an object")
        text = _clean_paragraph(raw.get("text"))
        if not text:
            continue
        kind = str(raw.get("type", "")).strip()
        if kind not in CLAIM_KINDS:
            raise LLMOutputError(f"claim type must be one of {sorted(CLAIM_KINDS)}")
        ids = raw.get("evidence_ids", [])
        if not isinstance(ids, list):
            raise LLMOutputError("evidence_ids must be a list")
        ids = [str(x) for x in ids]
        if kind in {"fact", "vendor_claim"}:
            if not ids:
                raise LLMOutputError(f"factual claim has no evidence: {text[:40]}")
            missing = [x for x in ids if x not in evidence_by_id]
            if missing:
                raise LLMOutputError(f"claim cites unknown evidence: {missing}")
            if kind == "vendor_claim" and not text.startswith(("厂商称", "官方称", "公司称")):
                text = "厂商称：" + text
        claims.append(Claim(text=text, kind=kind, evidence_ids=ids))
    if not claims:
        raise LLMOutputError("writer returned no usable claims")
    return claims


def _check_numbers(
    text: str,
    label: str,
    evidence_by_id: dict[str, dict],
    claims: list[Claim],
    *,
    evidence_ids: list[str] | None = None,
    extra_text: str = "",
) -> None:
    # 数字核对的核心思路：
    #   1. 把待检查文本里的所有数字抓出来（支持百分比和小数）；
    #   2. 收集所有“被引用证据”的原文和发布日期；
    #   3. 两边都做 NFKC 归一化并去掉空格/逗号，再判断数字是否出现。
    # 这样 "1,200" 和 "1200"、"１２％" 和 "12%" 都能正确匹配。
    numbers = _NUMBER_RE.findall(unicodedata.normalize("NFKC", text))
    if not numbers:
        return
    haystack = _evidence_haystack(
        evidence_by_id, evidence_ids, claims, extra_text=extra_text
    )
    for token in numbers:
        normalized = token.replace(",", "")
        if normalized not in haystack:
            raise LLMOutputError(
                f"numeric claim in {label!r} is not supported by cited evidence: {token}"
            )


def _normalize_numbers(text: str) -> str:
    # 数字比对前的统一化：全角转半角，去掉空格、英文逗号和中文逗号。
    # 注意：这是“宽松匹配”，只要求数字串出现在证据里，不要求上下文语义。
    # 它的目的不是证明结论正确，而是拦住“证据里根本没有这个数”的编造。
    text = unicodedata.normalize("NFKC", text)
    # "100 per cent" and "100 percent" are the same unit as "100%".  Without
    # this normalization the writer can faithfully copy a percentage from the
    # evidence and still fail the numeric guard because the surface form
    # differs.  This is unit normalization, not a loosening of the check.
    text = re.sub(r"(?i)\bper\s*cent\b", "%", text)
    text = re.sub(r"(?i)\bpercent\b", "%", text)
    return re.sub(r"[\s,，]", "", text)


_PROMO_TERMS = (
    "免费",
    "限时",
    "打折",
    "折扣",
    "五折",
    "半价",
    "优惠",
    "立减",
    "赠送",
    "名额",
    "截止",
    "到期",
    "free",
    "discount",
)


def _evidence_haystack(
    evidence_by_id: dict[str, dict],
    evidence_ids: list[str] | None,
    claims: list[Claim],
    *,
    extra_text: str = "",
) -> str:
    cited = (
        {str(item) for item in evidence_ids}
        if evidence_ids is not None
        else {eid for claim in claims for eid in claim.evidence_ids}
    )
    parts: list[str] = []
    for eid in cited:
        item = evidence_by_id.get(eid, {})
        parts.append(str(item.get("quote", "")))
        if item.get("published_at"):
            parts.append(str(item["published_at"])[:10])
    if extra_text:
        parts.append(extra_text)
    return _normalize_numbers(" ".join(parts))


def _unsupported_numbers(
    text: str,
    evidence_by_id: dict[str, dict],
    claims: list[Claim],
    *,
    evidence_ids: list[str] | None = None,
    extra_text: str = "",
) -> list[str]:
    numbers = _NUMBER_RE.findall(unicodedata.normalize("NFKC", text))
    if not numbers:
        return []
    haystack = _evidence_haystack(
        evidence_by_id, evidence_ids, claims, extra_text=extra_text
    )
    unsupported: list[str] = []
    for token in numbers:
        normalized = token.replace(",", "")
        if normalized not in haystack:
            unsupported.append(token)
    return unsupported


def _unsupported_promo_terms(
    text: str,
    evidence_by_id: dict[str, dict],
    claims: list[Claim],
    *,
    evidence_ids: list[str] | None = None,
    extra_text: str = "",
) -> list[str]:
    haystack = _evidence_haystack(
        evidence_by_id, evidence_ids, claims, extra_text=extra_text
    )
    lowered = text.lower()
    return [
        term
        for term in _PROMO_TERMS
        if term in lowered and _normalize_numbers(term) not in haystack
    ]


def _repair_unsupported_caption(
    caption: list[str],
    evidence_by_id: dict[str, dict],
    claims: list[Claim],
    *,
    evidence_ids: list[str] | None = None,
    owner_context: str = "",
) -> tuple[list[str], list[str], list[str]]:
    """Remove caption sentences that make unsupported numeric/promo claims.

    This is a last-resort safety net for the creator agent.  The writer is
    instructed not to invent dates, prices, discounts, or giveaways.  If it
    still does, we remove only the offending sentence and surface a private
    approval warning, rather than failing the entire revision (or publishing
    the invented promotion).  Valid sentences are preserved.
    """
    kept_paragraphs: list[str] = []
    removed: list[str] = []
    owner_preserved: list[str] = []
    owner_norm = _normalize_numbers(owner_context)
    owner_lower = owner_context.lower()
    for paragraph in caption:
        sentences = [part for part in re.split(r"(?<=[。！？!?；;])", paragraph) if part]
        kept_sentences: list[str] = []
        for sentence in sentences:
            numbers = _unsupported_numbers(
                sentence, evidence_by_id, claims, evidence_ids=evidence_ids
            )
            promos = _unsupported_promo_terms(
                sentence, evidence_by_id, claims, evidence_ids=evidence_ids
            )
            if numbers or promos:
                owner_numbers = [
                    token for token in numbers if token.replace(",", "") in owner_norm
                ]
                owner_promos = [term for term in promos if term in owner_lower]
                if (
                    owner_context
                    and len(owner_numbers) == len(numbers)
                    and len(owner_promos) == len(promos)
                ):
                    # The owner supplied this date/promotion in the topic or
                    # revision instruction.  Keep it, but make clear it is not
                    # yet backed by the fetched sources; the owner decides at
                    # approval time.
                    kept_sentences.append(sentence)
                    owner_preserved.append(sentence.strip())
                    continue
                removed.append(sentence.strip())
                continue
            kept_sentences.append(sentence)
        if kept_sentences:
            kept_paragraphs.append("".join(kept_sentences).strip())
    if not kept_paragraphs:
        kept_paragraphs = [
            "这条消息涉及的日期/优惠信息暂时无法核实，请以官方发布为准。"
        ]
    return kept_paragraphs, removed, owner_preserved


def _sources_from_evidence(
    evidence_by_id: dict[str, dict],
    claims: list[Claim],
    *,
    storyboard: ImageStoryboard | None = None,
) -> list[SourceRef]:
    # 按“主张被引用的顺序”生成来源列表，而不是按数据库顺序。
    # 同一个 URL 只保留一次；最多列 5 条，避免文末太长。
    cited_order = [eid for claim in claims for eid in claim.evidence_ids]
    if storyboard is not None:
        # An image may be the only place a sourced fact appears.  Its source
        # must still be visible in the body's source list.
        for card in storyboard.all_cards():
            cited_order.extend(card.evidence_ids)
    seen_urls: set[str] = set()
    sources: list[SourceRef] = []
    for eid in cited_order:
        item = evidence_by_id.get(eid)
        if not item:
            continue
        url = str(item.get("url") or "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        sources.append(
            SourceRef(
                evidence_id=eid,
                name=(urlsplit(url).hostname or "来源") if str(item.get("source_name") or "").startswith("Topic Search ") else str(item.get("source_name") or "来源"),
                url=url,
                published_at=item.get("published_at"),
                date_precision=str(item.get("date_precision") or "unknown"),
                fetched_at=item.get("fetched_at"),
            )
        )
        if len(sources) >= 5:
            break
    return sources


def _check_banned(content: DraftContent, banned: list[str]) -> None:
    # 三类文字红线：
    #   1. 配置/账号记忆里的禁用词（如夸大宣传词）；
    #   2. 第一人称假体验（正则匹配“我亲测/我实测/亲自试用”等）——
    #      机器人没有身体，不能假装自己用过；
    #   3. “保证/承诺 + 收益/赚钱/涨粉/爆款”这类结果承诺，属于违规营销。
    # Check the caption, every storyboard field and the rendered body.  Image
    # text is public content too, so it cannot bypass the same language rules.
    text = "\n".join(
        [content.title, content.render_body(), " ".join(content.caption)]
        + ([content.storyboard.text()] if content.storyboard else [])
    )
    lowered = text.lower()
    for phrase in banned:
        phrase = str(phrase).strip()
        if phrase and phrase.lower() in lowered:
            raise LLMOutputError(f"banned phrase in draft: {phrase}")
    match = _FIRST_PERSON_RE.search(text)
    if match:
        raise LLMOutputError(f"first-person fabricated experience: {match.group(0)}")
    if re.search(r"(?:保证|承诺)[^。\n]{0,8}(?:收益|赚钱|涨粉|爆款)", text):
        raise LLMOutputError("guaranteed-outcome language is not allowed")
    engagement = _ENGAGEMENT_RE.search(text)
    if engagement:
        raise LLMOutputError(
            f"fabricated engagement claim is not supported by evidence: {engagement.group(0)}"
        )


def _clean_line(value: Any, label: str) -> str:
    # 单行文本清洗：只接受字符串/数字，NFKC 归一化（全角转半角），
    # 把连续空白压成一个空格，再检查不能为空。
    if not isinstance(value, (str, int, float)):
        raise LLMOutputError(f"{label} must be text")
    text = unicodedata.normalize("NFKC", str(value)).strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        raise LLMOutputError(f"{label} must not be empty")
    return text


def _clean_paragraph(value: Any) -> str:
    # 段落清洗比单行宽松：保留换行，但去掉所有控制字符，
    # 把多行压成单行内的换行，最后截到 800 字。
    # 控制字符（如 \x00、\x07）可能破坏 JSON、日志或渲染，必须清掉。
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)):
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = text.replace("\r\n", "\n").strip()
    # Paragraphs are rendered as plain text; never allow control characters.
    text = "".join(ch for ch in text if ch == "\n" or ord(ch) >= 32)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()[:800]


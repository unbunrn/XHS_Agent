"""Durable editorial profile and revision acceptance.

Two different kinds of owner intent are deliberately kept apart:

* the **enduring editorial profile** is a nonsecret, editable local TOML file
  loaded for future generation;
* a **per-post instruction** belongs to one draft/revision request and is never
  written into the profile automatically, and webpage/evidence text is never
  promoted into either.

Revision acceptance combines deterministic artifact checks with one bounded,
independent semantic review.  The generating call's own claims of success are
never accepted as proof that the request was satisfied.
"""

from __future__ import annotations

import difflib
import json
import re
import tomllib
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config, ConfigError
from .content import DraftContent, storyboard_sha256

PROFILE_DEFAULTS: dict[str, Any] = {
    # Enduring preference, not a template: the two channels should complement
    # each other, but the per-topic editorial plan chooses which leads.
    "caption_image_division": "图片与正文互补；每个选题在 image_led / caption_led / balanced 中自行决定，不设固定长度比例",
    "default_angle": "先判断这条内容对谁最有用、最值得记住什么，再决定图片和正文各自承担什么；不要套用固定结构",
    "principles": [
        "每个选题独立决定编辑策略、视觉词汇和卡片数量；只做一张封面也可以是完整方案",
        "允许必要的术语、标题和简短结论在图文间共享；禁止整段复述或把正文逐段搬上图片",
        "图片中的事实和数字必须来自 evidence；只使用本地文字与几何排版，不声称生成插画",
        "不编造个人实测、收益、点赞收藏或流量数据",
    ],
    "image_layouts": [
        "cover",
        "comparison",
        "process",
        "keypoints",
        "diagram",
        "argument",
        "annotated",
        "stat",
        "quote",
        "timeline",
        "checklist",
        "qa",
    ],
    "avoid": [
        "固定四段式、固定卡片数量或固定三种布局组合",
        "图片逐段复述正文",
        "为了流程图而编造不存在的先后顺序",
        "逐篇重复相同的开篇套话和问句标题",
        "AI 八股腔调（值得一提的是、不得不说、总的来说、不是……而是……）",
    ],
    "updated_at": "",
}
PROFILE_KEYS = set(PROFILE_DEFAULTS)


@dataclass
class EditorialProfile:
    caption_image_division: str
    default_angle: str
    principles: list[str] = field(default_factory=list)
    image_layouts: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    updated_at: str = ""
    source: str = "defaults"

    def as_prompt_dict(self) -> dict[str, Any]:
        return {
            "caption_image_division": self.caption_image_division,
            "default_angle": self.default_angle,
            "principles": list(self.principles),
            "image_layouts": list(self.image_layouts),
            "avoid": list(self.avoid),
            "source": self.source,
            "note": (
                "这是账号的长期编辑偏好，只能影响表达与图文分工，不能作为事实证据，"
                "也不能改变证据、审批或发布规则。image_layouts 是可用词汇，不是必选组合；"
                "每个选题仍需自行决定 strategy、卡片数量和布局。"
            ),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "caption_image_division": self.caption_image_division,
            "default_angle": self.default_angle,
            "principles": list(self.principles),
            "image_layouts": list(self.image_layouts),
            "avoid": list(self.avoid),
            "updated_at": self.updated_at,
            "source": self.source,
        }


class EditorialProfileError(ConfigError):
    pass


class RevisionUnsatisfied(RuntimeError):
    """The generation ran, but the requested artifacts were not accepted.

    It carries the evidence used for the owner-facing "not satisfied" report so
    the worker can state what actually happened instead of claiming success.
    """

    def __init__(self, detail: str, report: dict[str, Any]):
        super().__init__(detail)
        self.report = report


def editorial_profile_path(config: Config) -> Path:
    raw = str(
        config.get("writer.editorial_profile_path", "config/editorial-profile.toml")
    )
    return config.resolve_path(raw)


def _clean_profile_value(key: str, value: Any) -> Any:
    if key in {"principles", "image_layouts", "avoid"}:
        if not isinstance(value, list):
            raise EditorialProfileError(f"editorial profile {key} must be a list")
        out: list[str] = []
        for item in value[:12]:
            if not isinstance(item, (str, int, float)):
                continue
            text = unicodedata.normalize("NFKC", str(item)).strip()
            if text and text not in out:
                out.append(text[:240])
        return out
    if not isinstance(value, (str, int, float)):
        raise EditorialProfileError(f"editorial profile {key} must be text")
    text = unicodedata.normalize("NFKC", str(value)).strip()
    limit = 80 if key == "caption_image_division" else 300
    if len(text) > limit:
        raise EditorialProfileError(f"editorial profile {key} is too long")
    return text


def load_editorial_profile(config: Config) -> EditorialProfile:
    """Load the local editable profile; missing file means conservative defaults."""

    path = editorial_profile_path(config)
    if not path.exists():
        return EditorialProfile(
            caption_image_division=str(PROFILE_DEFAULTS["caption_image_division"]),
            default_angle=str(PROFILE_DEFAULTS["default_angle"]),
            principles=list(PROFILE_DEFAULTS["principles"]),
            image_layouts=list(PROFILE_DEFAULTS["image_layouts"]),
            avoid=list(PROFILE_DEFAULTS["avoid"]),
            source="defaults",
        )
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise EditorialProfileError(f"cannot read editorial profile {path}: {exc}") from exc
    section = data.get("editorial")
    if not isinstance(section, dict):
        raise EditorialProfileError("editorial profile needs an [editorial] table")
    values = dict(PROFILE_DEFAULTS)
    for key in PROFILE_KEYS:
        if key in section:
            values[key] = _clean_profile_value(key, section[key])
    if not str(values.get("caption_image_division", "")).strip():
        raise EditorialProfileError("editorial profile caption_image_division is empty")
    return EditorialProfile(
        caption_image_division=str(values["caption_image_division"]),
        default_angle=str(values["default_angle"]),
        principles=[str(item) for item in values.get("principles", [])],
        image_layouts=[str(item) for item in values.get("image_layouts", [])],
        avoid=[str(item) for item in values.get("avoid", [])],
        updated_at=str(values.get("updated_at", "")),
        source=str(path),
    )


def save_editorial_profile(
    config: Config,
    updates: dict[str, Any],
    *,
    explicit_owner_request: bool,
) -> EditorialProfile:
    """Persist an explicitly requested enduring preference.

    There is no implicit caller: per-post instructions, revision text and
    webpage content must never reach this function.  The ``explicit_owner_request``
    flag is a deliberate guard rather than an inference from arbitrary text.
    """

    if not explicit_owner_request:
        raise EditorialProfileError("editorial profile changes require an explicit owner request")
    unknown = set(updates) - PROFILE_KEYS
    if unknown:
        raise EditorialProfileError(f"unknown editorial profile keys: {sorted(unknown)}")
    current = load_editorial_profile(config)
    values = {
        "caption_image_division": current.caption_image_division,
        "default_angle": current.default_angle,
        "principles": list(current.principles),
        "image_layouts": list(current.image_layouts),
        "avoid": list(current.avoid),
        "updated_at": current.updated_at,
    }
    for key, value in updates.items():
        values[key] = _clean_profile_value(key, value)
    values["updated_at"] = datetime.now(timezone.utc).isoformat()

    path = editorial_profile_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    lines = ["# Nonsecret editorial preferences. Edit by hand or via an explicit owner command.", "", "[editorial]"]
    for key in ("caption_image_division", "default_angle", "updated_at"):
        lines.append(f"{key} = {json.dumps(str(values[key]), ensure_ascii=False)}")
    for key in ("principles", "image_layouts", "avoid"):
        lines.append(f"{key} = [")
        for item in values[key]:
            lines.append(f"  {json.dumps(str(item), ensure_ascii=False)},")
        lines.append("]")
    temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temp.replace(path)
    loaded = load_editorial_profile(config)
    return loaded


@dataclass
class RevisionRequest:
    artifacts: tuple[str, ...]
    intents: tuple[str, ...]
    instruction: str
    changes: list[dict[str, str]] = field(default_factory=list)
    before_strategy: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "instruction": self.instruction,
            "artifacts": list(self.artifacts),
            "intents": list(self.intents),
            "before_strategy": self.before_strategy,
            "changes": list(self.changes),
        }

    def artifact_intents(self, artifact: str) -> set[str]:
        for change in self.changes:
            if change.get("artifact") == artifact:
                return {
                    item
                    for item in str(change.get("intent", "")).split(",")
                    if item
                }
        return set()


_BODY_WORDS = re.compile(r"正文|文案|文字|内容|描述|caption|body", re.I)
_IMAGE_WORDS = re.compile(r"图片|配图|卡片|图像|视觉|storyboard|image|visual", re.I)
_DUP_WORDS = re.compile(r"重复|冗余|复述|重叠|一样|duplicate|overlap|repeat|copy", re.I)
_SHORT_WORDS = re.compile(
    r"精简|简短|缩短|太长|过多|压缩|少|短|只做介绍|衬托|点到为止|略写|仅作介绍|"
    r"concise|short",
    re.I,
)
_EXPAND_WORDS = re.compile(r"详写|展开|详细|补充|加长|多写|detail|expand", re.I)
_LAYOUT_WORDS = re.compile(
    r"布局|排版|版式|卡片结构|对比|流程|要点|层级|"
    r"layout|comparison|process|keypoint|diagram|annotated",
    re.I,
)
_TITLE_WORDS = re.compile(r"标题|title", re.I)
_CLAUSE_SPLIT = re.compile(r"[，。；;,.!?！？\n]+")


def _intents_in(text: str) -> set[str]:
    found: set[str] = set()
    if _DUP_WORDS.search(text):
        found.add("deduplicate")
    if _SHORT_WORDS.search(text):
        found.add("shorten")
    if _EXPAND_WORDS.search(text):
        found.add("expand")
    if _LAYOUT_WORDS.search(text):
        found.add("layout")
    return found


def infer_revision_request(instruction: str, before: DraftContent) -> RevisionRequest:
    """Turn owner feedback into a bounded acceptance contract.

    The contract records which artifacts the owner named and clause-local
    intent (for example "图片精简，正文详写" applies shorten to images and
    expand to caption).  It is deliberately not an editorial template: the
    generating model still reads the raw instruction and the previous plan, and
    semantic review checks the chosen strategy.  When the owner offers
    alternatives ("要么…要么…"), conflicting directions are kept so acceptance
    only requires a coherent choice, not both branches.
    """

    text = unicodedata.normalize("NFKC", str(instruction or "")).strip()
    global_intents = _intents_in(text)
    mentions_body = bool(_BODY_WORDS.search(text))
    mentions_images = bool(_IMAGE_WORDS.search(text))
    mentions_title = bool(_TITLE_WORDS.search(text))

    artifacts: list[str] = []
    if mentions_body:
        artifacts.append("caption")
    if mentions_images:
        artifacts.append("images")
    if global_intents & {"deduplicate", "layout"} or (mentions_body and mentions_images):
        # Rebalancing is inherently about the pair, not one artifact.
        artifacts = ["images", "caption"]
    if mentions_title:
        artifacts.append("title")
    if "layout" in global_intents and "images" not in artifacts:
        artifacts.append("images")
    if not artifacts:
        artifacts = ["any"]

    per_artifact: dict[str, set[str]] = {artifact: set() for artifact in artifacts}
    for clause in _CLAUSE_SPLIT.split(text):
        clause = clause.strip()
        if not clause:
            continue
        clause_intents = _intents_in(clause)
        if not clause_intents:
            continue
        if "caption" in per_artifact and _BODY_WORDS.search(clause):
            per_artifact["caption"].update(clause_intents)
        if "images" in per_artifact and (
            _IMAGE_WORDS.search(clause) or _LAYOUT_WORDS.search(clause)
        ):
            per_artifact["images"].update(clause_intents)
        if "title" in per_artifact and _TITLE_WORDS.search(clause):
            per_artifact["title"].update(clause_intents)
    # If an artifact was named but no clause mentioned it directly, it keeps the
    # global intent.  "any" exists only to require a real change.
    for artifact in artifacts:
        per_artifact.setdefault(artifact, set(global_intents))
        if not per_artifact[artifact]:
            per_artifact[artifact].add("express")

    intents: list[str] = []
    for name in ("deduplicate", "shorten", "expand", "layout", "express"):
        if name in global_intents or any(name in values for values in per_artifact.values()):
            intents.append(name)
    ordered_artifacts = tuple(dict.fromkeys(artifacts))
    changes = [
        {
            "artifact": artifact,
            "intent": ",".join(
                name
                for name in ("deduplicate", "shorten", "expand", "layout", "express")
                if name in per_artifact[artifact]
            ),
            "instruction": text[:2000],
        }
        for artifact in ordered_artifacts
    ]
    return RevisionRequest(
        artifacts=ordered_artifacts,
        intents=tuple(dict.fromkeys(intents)),
        instruction=text,
        changes=changes,
        before_strategy=(
            before.editorial_plan.strategy if before.editorial_plan is not None else ""
        ),
    )


def _snapshot(content: DraftContent) -> dict[str, Any]:
    board = content.storyboard
    layouts = [card.layout for card in board.all_cards()] if board else []
    return {
        "title": content.title,
        "body": content.render_body(),
        "caption": list(content.caption),
        "storyboard": board.as_dict() if board else None,
        "storyboard_sha256": storyboard_sha256(board),
        "layout_types": layouts,
        "editorial_plan": content.editorial_plan.as_dict() if content.editorial_plan else None,
    }


def _normalized(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


def duplication_report(content: DraftContent) -> dict[str, Any]:
    """Detect copied paragraphs, not ordinary semantic overlap.

    Shared terminology, the title, a one-line conclusion or a necessary label
    are legitimate across image and caption.  The failure mode is an entire
    caption paragraph (or near-identical paragraph) pasted onto a card.  Only
    such paragraph-level copying is marked heavy.
    """

    caption_raw = " ".join(content.caption)
    caption = _normalized(caption_raw or content.render_body())
    if content.storyboard is None:
        return {
            "max_overlap_ratio": 0.0,
            "copied_cards": [],
            "duplicated_paragraphs": [],
            "paragraph_duplication_count": 0,
            "caption_chars": len(caption),
            "image_chars": 0,
            "heavy_duplication": False,
        }
    paragraph_texts = [
        _normalized(paragraph)
        for paragraph in (content.caption or [])
        if len(_normalized(paragraph)) >= 24
    ]
    worst = 0.0
    copied: list[int] = []
    pairs: list[dict[str, Any]] = []
    image_chars = 0
    for index, card in enumerate(content.storyboard.all_cards()):
        card_text = _normalized(card.text())
        image_chars += len(card_text)
        if not card_text or not caption:
            continue
        matcher = difflib.SequenceMatcher(None, card_text, caption, autojunk=False)
        match = matcher.find_longest_match(0, len(card_text), 0, len(caption))
        worst = max(worst, match.size / max(1, len(card_text)))
        card_flagged = False
        for paragraph_index, paragraph in enumerate(paragraph_texts):
            if not paragraph:
                continue
            # Flag a whole caption paragraph pasted onto an image.  A short
            # excerpt/takeaway that also appears inside the caption is allowed:
            # complementary channels legitimately share a term or conclusion.
            if paragraph in card_text:
                pairs.append(
                    {
                        "caption_paragraph": paragraph_index,
                        "card": index,
                        "mode": "copied_paragraph",
                    }
                )
                card_flagged = True
                break
            ratio = difflib.SequenceMatcher(
                None, paragraph, card_text, autojunk=False
            ).ratio()
            if ratio >= 0.9 and min(len(paragraph), len(card_text)) >= 24:
                pairs.append(
                    {
                        "caption_paragraph": paragraph_index,
                        "card": index,
                        "mode": "near_identical_paragraph",
                    }
                )
                card_flagged = True
                break
        if card_flagged:
            copied.append(index)
    return {
        "max_overlap_ratio": round(worst, 3),
        "copied_cards": copied,
        "duplicated_paragraphs": pairs,
        "paragraph_duplication_count": len(pairs),
        "caption_chars": len(caption),
        "image_chars": image_chars,
        "heavy_duplication": bool(pairs),
    }


def deterministic_revision_check(
    before: DraftContent,
    after: DraftContent,
    request: RevisionRequest,
    config: Config,
) -> dict[str, Any]:
    """Artifact diff plus bounded, artifact-specific checks.

    There is no universal caption minimum, image-to-caption ratio, card count or
    layout combination.  A direction is enforced only when the owner named it
    for that artifact ("图片精简", "正文详写").  If the owner offered both
    directions for the same artifact, either coherent choice is accepted.
    """

    before_snap = _snapshot(before)
    after_snap = _snapshot(after)
    changed = {
        "title": _normalized(before_snap["title"]) != _normalized(after_snap["title"]),
        "caption": _normalized(before_snap["body"]) != _normalized(after_snap["body"]),
        "images": before_snap["storyboard_sha256"] != after_snap["storyboard_sha256"],
        "layout": before_snap["layout_types"] != after_snap["layout_types"],
    }
    if before_snap["storyboard"] is None and after_snap["storyboard"] is not None:
        changed["images"] = True
    issues: list[str] = []
    # "图片和正文重复" is a pair-level problem.  A valid edit may remove the
    # duplication by changing either side (usually the images); it must not be
    # forced to rewrite both.
    pair_dedup = "deduplicate" in request.intents and {
        "images",
        "caption",
    }.issubset(set(request.artifacts))
    if pair_dedup and not (changed["caption"] or changed["images"]):
        issues.append("requested deduplication but neither caption nor images changed")

    for artifact in request.artifacts:
        if artifact in {"caption", "images"} and pair_dedup:
            continue
        if artifact == "caption" and not changed["caption"]:
            issues.append("requested caption/正文 change but the rendered caption is unchanged")
        elif artifact == "images" and not changed["images"]:
            issues.append("requested image change but the storyboard is unchanged")
        elif artifact == "title" and not changed["title"]:
            issues.append("requested title change but the title is unchanged")
        elif artifact == "any" and not any(changed.values()):
            issues.append("revision produced no change in title, caption or images")

    if "layout" in request.intents and not changed["layout"]:
        issues.append("requested layout change but the card layout types are unchanged")

    before_dup = duplication_report(before)
    after_dup = duplication_report(after)
    if after_dup["heavy_duplication"]:
        issues.append(
            "image text copies whole caption paragraphs "
            f"({after_dup['paragraph_duplication_count']} copied paragraph(s))"
        )
    if "deduplicate" in request.intents and before_dup["heavy_duplication"]:
        improved = (
            after_dup["paragraph_duplication_count"]
            < before_dup["paragraph_duplication_count"]
        )
        if not improved:
            issues.append(
                "revision did not reduce paragraph-level caption/image duplication "
                f"({before_dup['paragraph_duplication_count']} -> "
                f"{after_dup['paragraph_duplication_count']})"
            )

    # Directional length checks are scoped to the artifact the owner named.
    for artifact in request.artifacts:
        named = request.artifact_intents(artifact)
        directions = named & {"shorten", "expand"}
        if len(directions) != 1:
            continue
        if artifact == "caption":
            before_len, after_len = before_dup["caption_chars"], after_dup["caption_chars"]
        elif artifact == "images":
            before_len, after_len = before_dup["image_chars"], after_dup["image_chars"]
        else:
            continue
        if "shorten" in directions and after_len >= before_len:
            issues.append(
                f"owner asked to shorten {artifact}, but text did not get shorter "
                f"({before_len} -> {after_len} chars)"
            )
        if "expand" in directions and after_len <= before_len:
            issues.append(
                f"owner asked to expand {artifact}, but text did not get longer "
                f"({before_len} -> {after_len} chars)"
            )

    storyboard = after.storyboard
    if storyboard is None:
        issues.append("new revision has no independent image storyboard")
        board_text = ""
        layouts: list[str] = []
    else:
        board_text = storyboard.text()
        layouts = [card.layout for card in storyboard.all_cards()]
    if len(board_text) > int(config.get("writer.storyboard_max_chars", 900)):
        issues.append("storyboard exceeds the one-render text bound")

    strategy = after.editorial_plan.strategy if after.editorial_plan is not None else ""
    if strategy == "caption_led" and not after.caption:
        issues.append("caption-led plan has no caption")
    if strategy == "image_led" and not board_text:
        issues.append("image-led plan has no visual text")
    if strategy == "balanced" and (not after.caption or not board_text):
        issues.append("balanced plan needs both caption and visual text")

    return {
        "changed": changed,
        "issues": issues,
        "before": before_snap,
        "after": after_snap,
        "duplication": {"before": before_dup, "after": after_dup},
        "accepted": not issues,
    }


def semantic_review_payload(
    before: DraftContent,
    after: DraftContent,
    request: RevisionRequest,
    deterministic: dict[str, Any],
) -> dict[str, Any]:
    return {
        "task": "review_revision_against_owner_request",
        "rules": [
            "只判断修改是否满足 owner 的修改要求，不重新编写稿件。",
            "同时阅读修改前正文、editorial_plan、storyboard 与修改后三者；不能只看生成模型的自我声明。",
            "如果 owner 指定了正文/图片/标题要改而实际没变，satisfied=false。",
            "检查修改后的 strategy 与实际图文分工是否一致；image-led 可以有很短正文，caption-led 可以只有一张封面，balanced 两者都承担信息。不要要求固定比例、固定卡片数或固定布局组合。",
            "判断图文是否整段复述；共享必要术语、标题、简短结论是允许的，不要把正常语义重叠判成重复。",
            "若布局暗示了来源没有说明的顺序/因果关系，satisfied=false。",
            "若出现编造个人实测、收益、点赞/收藏/流量等证据不支持的说法，satisfied=false。",
            "图片文字和正文都只能依据 evidence；不要因为措辞更漂亮就判定满足。",
        ],
        "request": request.as_dict(),
        "deterministic_findings": {
            "changed": deterministic["changed"],
            "issues": deterministic["issues"],
            "duplication": deterministic["duplication"],
        },
        "before": deterministic["before"],
        "after": deterministic["after"],
        "output_schema": {
            "satisfied": "boolean",
            "change_summary": "一句话说明实际改了什么",
            "problems": ["未满足的具体点，最多5条"],
            "remaining_duplication": "boolean（仅整段复述为 true）",
            "strategy_consistent": "boolean（after 的 strategy 与图文分工/plan 一致）",
            "invented_experience_or_engagement": "boolean",
        },
    }


def parse_semantic_review(data: Any, *, max_problems: int = 5) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {
            "satisfied": False,
            "change_summary": "",
            "problems": ["reviewer did not return a JSON object"],
            "remaining_duplication": True,
            "strategy_consistent": False,
            "invented_experience_or_engagement": True,
        }
    problems = data.get("problems", [])
    if not isinstance(problems, list):
        problems = []
    clean_problems = [_clean_review_text(item, 240) for item in problems]
    clean_problems = [item for item in clean_problems if item][: max(0, int(max_problems))]
    summary = _clean_review_text(data.get("change_summary"), 300)
    satisfied = data.get("satisfied") is True
    remaining = data.get("remaining_duplication") is True
    strategy_consistent = data.get("strategy_consistent") is True
    invented = data.get("invented_experience_or_engagement") is True
    if remaining:
        clean_problems.append("reviewer reports paragraph-level caption/image duplication")
    if not strategy_consistent:
        clean_problems.append("reviewer reports the chosen strategy does not match the artifacts")
    if invented:
        clean_problems.append("reviewer reports invented experience/engagement claims")
    accepted = bool(
        satisfied
        and strategy_consistent
        and not remaining
        and not invented
        and not clean_problems
    )
    return {
        "satisfied": accepted,
        "change_summary": summary,
        "problems": clean_problems[: max(1, int(max_problems))] if clean_problems else [],
        "remaining_duplication": remaining,
        "strategy_consistent": strategy_consistent,
        "invented_experience_or_engagement": invented,
    }


def _clean_review_text(value: Any, limit: int) -> str:
    if not isinstance(value, (str, int, float)):
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def change_summary_lines(report: dict[str, Any]) -> list[str]:
    """Concise owner-facing lines describing actual, verified changes."""

    before = report.get("before") or {}
    after = report.get("after") or {}
    changed = report.get("changed") or {}
    lines: list[str] = []
    if changed.get("caption"):
        before_chars = len(_normalized(str(before.get("body") or "")))
        after_chars = len(_normalized(str(after.get("body") or "")))
        lines.append(f"正文已修改（{before_chars} -> {after_chars} 字）")
    if changed.get("images"):
        before_board = before.get("storyboard") or {}
        after_board = after.get("storyboard") or {}
        before_cards = len(before_board.get("cards") or []) + (
            1 if before_board.get("cover") else 0
        )
        after_cards = len(after_board.get("cards") or []) + (
            1 if after_board.get("cover") else 0
        )
        lines.append(f"图片 storyboard 已修改（{before_cards} -> {after_cards} 张）")
    if changed.get("title"):
        lines.append("标题已修改")
    if changed.get("layout"):
        before_layout = "/".join(before.get("layout_types") or []) or "无"
        after_layout = "/".join(after.get("layout_types") or []) or "无"
        lines.append(f"图片布局：{before_layout} -> {after_layout}")
    before_plan = before.get("editorial_plan") or {}
    after_plan = after.get("editorial_plan") or {}
    if after_plan.get("strategy") and (
        after_plan.get("strategy") != before_plan.get("strategy")
        or after_plan.get("visual_approach") != before_plan.get("visual_approach")
    ):
        lines.append(
            "编辑策略："
            f"{before_plan.get('strategy') or '旧稿'}"
            f"/{before_plan.get('visual_approach') or '无'}"
            f" -> {after_plan.get('strategy')}"
            f"/{after_plan.get('visual_approach') or '无'}"
        )
    duplication = report.get("duplication") or {}
    before_dup = duplication.get("before") or {}
    after_dup = duplication.get("after") or {}
    if before_dup and after_dup:
        lines.append(
            "整段图文重复："
            f"{int(before_dup.get('paragraph_duplication_count') or 0)} -> "
            f"{int(after_dup.get('paragraph_duplication_count') or 0)} 段"
        )
    if not lines:
        lines.append("未检测到实际内容变化")
    return lines

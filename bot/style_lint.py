"""Deterministic AI-style linter and recent-post cross-deduplication."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from .content import DraftContent
from .voice import Voice

_QUOTED_SPEECH_RE = re.compile(r"“[^”\n]*”|「[^」\n]*」|\"[^\"\n]*\"")
_NOT_BUT_RE = re.compile(
    r"(?:不是[^。！？\n]{1,36}?而是|不只是[^。！？\n]{1,36}?更是|不仅[^。！？\n]{1,36}?更是)"
)
_SUMMARY_CLOSE_RE = re.compile(
    r"^\s*(?:所以|总之|总的来说|总而言之|我的判断|说到底)\b|^\s*(?:所以|总之|总的来说|总而言之|我的判断|说到底)"
)
_CONNECTOR_START_RE = re.compile(r"^\s*(?:另外|而且|同时|此外|所以)")
_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?；;\n]+")
_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001FAFF\u2600-\u27BF]"
)
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+")
_LATIN_RUN_RE = re.compile(r"[A-Za-z][A-Za-z0-9._+\-]*")
_NUMBER_RUN_RE = re.compile(r"\d+(?:[.,]\d+)?%?")


@dataclass(frozen=True)
class RecentPost:
    title: str
    opening: str
    cover_headline: str = ""
    opening_move: str = ""
    visual_theme: str = ""
    draft_id: int | None = None


@dataclass(frozen=True)
class StyleIssue:
    code: str
    snippet: str
    hint: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "snippet": self.snippet, "hint": self.hint}


@dataclass(frozen=True)
class StyleReport:
    score: int
    issues: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"score": self.score, "issues": list(self.issues)}


def _first_sentence(text: str) -> str:
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(str(text or "")) if p.strip()]
    return parts[0] if parts else ""


def title_skeleton(title: str) -> str:
    """Compute structural title skeleton (numbers->#, Latin->A, Chinese runs->bucket)."""
    text = unicodedata.normalize("NFKC", str(title or "")).strip()
    if not text:
        return ""
    text = _NUMBER_RUN_RE.sub("#", text)
    text = _LATIN_RUN_RE.sub("A", text)

    def _bucket(match: re.Match[str]) -> str:
        n = len(match.group(0))
        if n <= 3:
            return "S"
        if n <= 7:
            return "M"
        return "L"

    text = _CJK_RUN_RE.sub(_bucket, text)
    text = re.sub(r"\s+", "", text)
    return text


def _same_title_skeleton(title_a: str, title_b: str) -> bool:
    sk_a = title_skeleton(title_a)
    sk_b = title_skeleton(title_b)
    if not sk_a or not sk_b:
        return False
    # Require either punctuation/placeholder structure or high similarity
    has_structure = any(ch in sk_a for ch in "?？!！:：,，#A")
    if sk_a == sk_b and (has_structure or len(sk_a) >= 2):
        return True
    if has_structure and SequenceMatcher(None, sk_a, sk_b).ratio() >= 0.85:
        return True
    return False


def _cv(lengths: list[int]) -> float:
    if len(lengths) < 2:
        return 1.0
    mean = sum(lengths) / len(lengths)
    if mean <= 0:
        return 1.0
    var = sum((x - mean) ** 2 for x in lengths) / len(lengths)
    return math.sqrt(var) / mean


def _strip_quoted(text: str) -> str:
    return _QUOTED_SPEECH_RE.sub(" ", str(text or ""))


def recent_posts(db: Any, limit: int = 10) -> list[RecentPost]:
    """Read-only query of the most recent drafts for cross-post deduplication."""
    if db is None:
        return []
    try:
        rows = db.fetchall(
            "SELECT id, title, body_json FROM drafts ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        )
    except Exception:
        return []
    out: list[RecentPost] = []
    for row in rows:
        raw_json = row["body_json"] if hasattr(row, "__getitem__") else None
        try:
            data = json.loads(raw_json) if raw_json else {}
        except (TypeError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        caption = data.get("caption") or []
        first_para = str(caption[0]) if isinstance(caption, list) and caption else ""
        sb = data.get("storyboard") if isinstance(data.get("storyboard"), dict) else {}
        cover = sb.get("cover") if isinstance(sb.get("cover"), dict) else {}
        plan = data.get("editorial_plan") if isinstance(data.get("editorial_plan"), dict) else {}
        out.append(
            RecentPost(
                title=str(row["title"] or ""),
                opening=_first_sentence(first_para),
                cover_headline=str(cover.get("headline") or ""),
                opening_move=str(plan.get("opening_move") or ""),
                visual_theme=str(plan.get("visual_theme") or ""),
                draft_id=int(row["id"]) if row["id"] is not None else None,
            )
        )
    return out


def lint_draft(
    content: DraftContent,
    voice: Voice,
    recent: list[RecentPost] | None = None,
) -> StyleReport:
    """Evaluate a DraftContent against voice rules, rhythm metrics, and recent posts."""
    issues: list[dict[str, str]] = []
    score = 0

    def add(code: str, snippet: str, hint: str, weight: int) -> None:
        nonlocal score
        score += weight
        issues.append({"code": code, "snippet": snippet[:80], "hint": hint})

    caption = [str(p).strip() for p in (content.caption or []) if str(p).strip()]
    title = str(content.title or "").strip()

    # Collect text for cliche check (quote layout & direct quotes exempted)
    unquoted_segments: list[str] = [_strip_quoted(title)]
    for p in caption:
        unquoted_segments.append(_strip_quoted(p))
    card_texts_all: list[str] = [title, *caption]
    if content.storyboard is not None:
        for card in content.storyboard.all_cards():
            card_texts_all.extend(card.text_parts())
            if card.layout == "quote":
                # Quote cards exempt quoted body/attribution, still check headline/note
                unquoted_segments.append(_strip_quoted(card.headline))
                unquoted_segments.append(_strip_quoted(card.note))
            elif card.layout == "cover" and getattr(card, "variant", "headline") == "quote":
                unquoted_segments.append(_strip_quoted(card.kicker))
                unquoted_segments.append(_strip_quoted(card.note))
            else:
                for part in card.text_parts():
                    unquoted_segments.append(_strip_quoted(part))

    unquoted_blob = "\n".join(unquoted_segments)
    full_blob = "\n".join(card_texts_all)

    # 1. cliche (8 pts per hit)
    for phrase in voice.cliches:
        p = str(phrase).strip()
        if p and p in unquoted_blob:
            add("cliche", p, f"删掉套话“{p}”，换成具体事实或直接判断", 8)

    # 2. not_but (>= 2 hits -> 10 pts)
    not_but_hits = _NOT_BUT_RE.findall(full_blob)
    if len(not_but_hits) >= 2:
        add("not_but", " / ".join(not_but_hits[:2]), "少用“不是……而是……”对仗句，直接说重点", 10)

    # 3. neq_symbol (6 pts)
    if "≠" in full_blob:
        add("neq_symbol", "≠", "用正常中文表述替代“≠”符号", 6)

    # 4. summary_close (12 pts)
    if caption and _SUMMARY_CLOSE_RE.match(caption[-1]):
        add("summary_close", caption[-1][:40], "去掉总结式结尾，改用具体建议、下一个节点或直接停在事实上", 12)

    # 5. connector_starts (>= 40% paragraphs start with connector -> 8 pts)
    if caption:
        conn_hits = [p for p in caption if _CONNECTOR_START_RE.match(p)]
        if len(conn_hits) / len(caption) >= 0.4:
            add(
                "connector_starts",
                f"{len(conn_hits)}/{len(caption)} 段用连接词开头",
                "减少“另外/而且/同时/此外/所以”段首连接词，靠内容衔接",
                8,
            )

    # 6. uniform_paragraphs (>= 4 paragraphs and CV < 0.25 -> 8 pts)
    if len(caption) >= 4:
        p_lens = [len(p) for p in caption]
        cv_p = _cv(p_lens)
        if cv_p < 0.25:
            add(
                "uniform_paragraphs",
                f"CV={cv_p:.2f}",
                "段落长度过于均匀，增加短句成段或节奏落差",
                8,
            )

    # 7. flat_rhythm (sentence length CV < 0.3 -> 6 pts)
    sentences = [
        s.strip()
        for p in caption
        for s in _SENTENCE_SPLIT_RE.split(p)
        if s.strip()
    ]
    if len(sentences) >= 4:
        s_lens = [len(s) for s in sentences]
        cv_s = _cv(s_lens)
        if cv_s < 0.3:
            add(
                "flat_rhythm",
                f"句长CV={cv_s:.2f}",
                "句子长度过于整齐，打散成一长一短的交错节奏",
                6,
            )

    # 8. emoji (> emoji_max or at paragraph start -> 4 pts each)
    all_emojis = _EMOJI_RE.findall(full_blob)
    if len(all_emojis) > voice.emoji_max:
        add(
            "emoji",
            f"共 {len(all_emojis)} 个 emoji",
            f"emoji 数量超过上限 {voice.emoji_max}",
            4,
        )
    if not voice.emoji_at_paragraph_start:
        for p in caption:
            if p and _EMOJI_RE.match(p[0]):
                add("emoji", p[:12], "不要在段落开头放 emoji", 4)

    # 9. exclaim (! / ！ > 2 -> 4 pts)
    exclaim_count = full_blob.count("!") + full_blob.count("！")
    if exclaim_count > 2:
        add("exclaim", f"{exclaim_count} 个感叹号", "减少感叹号，保持克制判断", 4)

    recent_list = list(recent or [])
    # 10. recent_opening (first 10 chars of opening sentence similarity >= 0.6 -> 15 pts)
    if caption and recent_list:
        cur_open = _first_sentence(caption[0])[:10]
        if cur_open:
            for prev in recent_list[: voice.recent_window]:
                prev_open = _first_sentence(prev.opening)[:10]
                if prev_open and SequenceMatcher(None, cur_open, prev_open).ratio() >= 0.6:
                    add(
                        "recent_opening",
                        cur_open,
                        f"首句与近期作品“{prev_open}”过像，换一种开头方式",
                        15,
                    )
                    break

    # 11. recent_title_skeleton (15 pts)
    if title and recent_list:
        for prev in recent_list[: voice.recent_window]:
            if prev.title and _same_title_skeleton(title, prev.title):
                add(
                    "recent_title_skeleton",
                    title,
                    f"标题骨架与近期“{prev.title}”重复，换个句式结构",
                    15,
                )
                break

    # 12. question_title_overuse (8 pts)
    if ("?" in title or "？" in title) and recent_list:
        if any("?" in p.title or "？" in p.title for p in recent_list[:5]):
            add(
                "question_title_overuse",
                title,
                "近 5 篇已有问句标题，改用陈述、数字反差或冒号结构",
                8,
            )

    return StyleReport(score=min(100, score), issues=issues)

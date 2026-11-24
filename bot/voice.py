"""Account voice persona loading and deterministic prompt sampling."""

from __future__ import annotations

import hashlib
import random
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config

DEFAULT_CLICHES = [
    "看到外媒报道", "挺有意思", "耐人寻味", "值得一提的是", "不得不说", "话锋一转", "最戳我的",
    "我的判断是", "我自己的感觉", "总的来说", "综上", "总而言之", "值得关注", "让我们", "一起来看看",
    "划重点", "干货", "宝子", "家人们", "说白了", "本质上", "真正关键的是", "这意味着", "某种程度上",
    "先别急着", "一文读懂", "震惊", "炸裂", "王炸", "颠覆", "赋能", "抓手", "底层逻辑", "闭环", "无限可能",
    "范式", "新纪元", "里程碑式", "不容忽视", "引发热议",
]

DEFAULT_OPENING_MOVES = [
    "直接给结论：第一句就是读者最该知道的事",
    "一个数字：用证据里最有冲击力的数字开场",
    "一句原话：引用当事人/机构的原话（需注明来源）",
    "一个反常细节：证据里最出乎意料的那个细节",
    "一个对比：把两件同时发生/前后矛盾的事放在一起",
    "跟读者有关：第一句说清楚这事影响谁、影响什么",
]

DEFAULT_CLOSING_MOVES = [
    "给一个具体可执行的建议（谁、现在该做什么/不该做什么）",
    "指出下一个值得盯的节点（时间/事件/指标，须来自证据或明确标为推测）",
    "留一个证据里尚未回答的具体问题",
    "直接停在最后一个事实上，不加评论",
]

DEFAULT_TITLE_PATTERNS = [
    "数字 + 反差（例：98% 相信，但还不算数）",
    "主体 + 动作 + 关键限定（例：Claude 写的证明，数学家还没读完）",
    "读者视角利害（例：用 Cursor 的可以先别升级）",
    "原话截取（需真实出处）",
    "冒号结构：话题：判断",
]

DEFAULT_GOOD_EXAMPLES = [
    "梅纳德给了一个很数学家的数字：98%～99%。他相信这份证明大概率是对的，但紧接着补了一句——没人读懂之前，它还不算完成。",
    "先说结论：这次更新跟普通用户关系不大，真正该关心的是在用 API 跑批量任务的人，价格那一栏变了。",
    "1700 万行 Lean 代码，整理成文字大约 25 万页。数学界现在面对的不是“对不对”，而是“谁来读”。",
    "如果你只是用它写周报，这次可以跳过；如果你在做长文档检索，值得花十分钟看下新的上下文上限。",
]

DEFAULT_BAD_EXAMPLES = [
    "看到外媒报道，XXX 发布了 YYY，这事挺有意思。",
    "最戳我的是……",
    "所以我的判断是：这件事值得关注，但还需要时间验证。",
    "这不是一次简单的更新，而是一次范式的转变。",
    "总的来说，AI 的发展让我们看到了无限可能。",
]


@dataclass
class Voice:
    persona: str = "z-Morty：一个紧盯 AI 与科技圈动态的观察者兼开发者，在群里给朋友转消息时顺手补一句靠谱的判断。懂行，信息灵通，但不端着。"
    reader: str = "关注 AI、科技行业与开发者生态的中文读者：想用三十秒看懂这件事的核心事实、来龙去脉或实际看点。"
    tone: list[str] = field(
        default_factory=lambda: [
            "像发给懂行朋友的消息，不像报告、不像公众号、不像营销号",
            "按选题类型自适应切入点：如果是工具/模型/API发布，讲清用法、成本或实际变化；如果是公司人事、商业博弈、行业大新闻，直接聚焦事件本身的核心事实与行业看点，严禁强行往“对独立开发者/手上的API和工具没影响”上硬靠",
            "当多家主流媒体已明确转引官方公告或官号表态时，正常注明“据媒体报道/转引官方公告”即可，不要在结尾泼冷水写“等官网原始公告出来再说/别把转述当一手信息”这类套话",
            "名词具体、动词有力；能用数字、原话、型号说清楚的，不用形容词",
            "句子长短交错，允许一句话成段；不要每段都差不多长",
            "判断要明确，并说清依据是什么；分析就说是分析，不要假装事实",
            "第一人称只用于判断和建议，不写体验、不写感受抒情",
        ]
    )
    emoji_max: int = 2
    emoji_at_paragraph_start: bool = False
    opening_moves: list[str] = field(default_factory=lambda: list(DEFAULT_OPENING_MOVES))
    closing_moves: list[str] = field(default_factory=lambda: list(DEFAULT_CLOSING_MOVES))
    title_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_TITLE_PATTERNS))
    good_examples: list[str] = field(default_factory=lambda: list(DEFAULT_GOOD_EXAMPLES))
    bad_examples: list[str] = field(default_factory=lambda: list(DEFAULT_BAD_EXAMPLES))
    cliches: list[str] = field(default_factory=lambda: list(DEFAULT_CLICHES))
    max_score: int = 30
    recent_window: int = 10
    source: str = "defaults"

    def as_prompt_dict(
        self,
        seed: str = "",
        recent_openings: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return a deterministically sampled subset for the writer prompt."""
        digest = hashlib.sha256(str(seed or "default").encode("utf-8")).digest()
        rng = random.Random(int.from_bytes(digest[:8], "big"))

        recent_set = {str(item).strip() for item in (recent_openings or []) if str(item).strip()}
        available_openings = [
            move
            for move in self.opening_moves
            if move not in recent_set and move.split("：", 1)[0] not in recent_set
        ]
        if len(available_openings) < 3:
            available_openings = list(self.opening_moves)

        sampled_openings = rng.sample(available_openings, k=min(3, len(available_openings)))
        sampled_closings = rng.sample(self.closing_moves, k=min(2, len(self.closing_moves)))
        sampled_good = rng.sample(self.good_examples, k=min(2, len(self.good_examples)))
        sampled_bad = rng.sample(self.bad_examples, k=min(2, len(self.bad_examples)))
        sampled_titles = rng.sample(self.title_patterns, k=min(3, len(self.title_patterns)))

        return {
            "persona": self.persona,
            "reader": self.reader,
            "tone": list(self.tone),
            "emoji_max": self.emoji_max,
            "emoji_at_paragraph_start": self.emoji_at_paragraph_start,
            "opening_moves": sampled_openings,
            "closing_moves": sampled_closings,
            "title_patterns": sampled_titles,
            "examples": {
                "good": sampled_good,
                "bad": sampled_bad,
                "note": "风格示例只示范语气和节奏，其中的事实一律不得使用。",
            },
        }


def voice_path(config: Config | None) -> Path | None:
    if config is None:
        return None
    raw = str(config.get("writer.voice_path", "config/voice.toml") or "").strip()
    if not raw:
        return None
    return config.resolve_path(raw)


def load_voice(config: Config | None = None) -> Voice:
    """Load voice configuration from TOML, falling back safely to defaults."""
    path = voice_path(config)
    if path is None or not path.exists():
        return Voice(source="defaults")
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return Voice(source="defaults")

    voice_sec = data.get("voice") if isinstance(data.get("voice"), dict) else {}
    examples_sec = voice_sec.get("examples") if isinstance(voice_sec.get("examples"), dict) else {}
    lint_sec = data.get("lint") if isinstance(data.get("lint"), dict) else {}

    def _str_list(raw: Any, fallback: list[str]) -> list[str]:
        if not isinstance(raw, list):
            return list(fallback)
        items = [str(x).strip() for x in raw if str(x).strip()]
        return items if items else list(fallback)

    defaults = Voice()
    return Voice(
        persona=str(voice_sec.get("persona") or defaults.persona).strip(),
        reader=str(voice_sec.get("reader") or defaults.reader).strip(),
        tone=_str_list(voice_sec.get("tone"), defaults.tone),
        emoji_max=max(0, int(voice_sec.get("emoji_max", defaults.emoji_max))),
        emoji_at_paragraph_start=bool(
            voice_sec.get("emoji_at_paragraph_start", defaults.emoji_at_paragraph_start)
        ),
        opening_moves=_str_list(voice_sec.get("opening_moves"), defaults.opening_moves),
        closing_moves=_str_list(voice_sec.get("closing_moves"), defaults.closing_moves),
        title_patterns=_str_list(voice_sec.get("title_patterns"), defaults.title_patterns),
        good_examples=_str_list(examples_sec.get("good"), defaults.good_examples),
        bad_examples=_str_list(examples_sec.get("bad"), defaults.bad_examples),
        cliches=_str_list(lint_sec.get("cliches"), defaults.cliches),
        max_score=max(0, int(lint_sec.get("max_score", defaults.max_score))),
        recent_window=max(1, int(lint_sec.get("recent_window", defaults.recent_window))),
        source=str(path),
    )

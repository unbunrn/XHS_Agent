"""Local-font PNG card rendering.

The image files are the authoritative preview *and* the files that an approved
publish will send.  Approval hashes the bytes on disk, so re-rendering after an
approval would invalidate it by design.  Content is flowed line by line and the
renderer refuses to silently truncate: if the configured card limit is reached,
it raises ``RenderError`` instead of dropping text.
"""

# =============================================================================
# 中文导读：把文字排成小红书图片卡片
# =============================================================================
# 小红书以图片为主，所以稿子要变成 1080x1440 的 PNG。
# 这个文件用 Pillow 在本地画图，不调用任何在线服务。
#
# 三个关键设计：
#   1. 图片字节就是“最终发布物”。审批时会算图片的 SHA-256；
#      审批之后重新渲染会导致哈希变化，审批自动失效——这是故意的。
#   2. 字号/边距/最大张数都从配置读，换字体或换尺寸不用改代码。
#   3. 绝不悄悄截断。页面放不下就抛 RenderError，让用户去缩短稿子。
#      如果偷偷删掉最后一段，可能正好删掉“限制与不确定性”这种关键声明。
#
# 生活化类比：
#   排版像往固定大小的相框里放文章。相框不够就再加一个相框（分页），
#   但绝不能用剪刀把文章剪掉一截。
# =============================================================================

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .content import DraftContent, ImageCard, ImageStoryboard, SECTION_ORDER
from .security import sha256_file
from .timeutil import local_iso

try:  # Pillow is optional for pure-logic tests; rendering reports a clear error.
    # Pillow 不是每个环境都装了。这里先用 try 导入，真到要渲染时再给出
    # 清晰错误，而不是让整个程序在 import 阶段就崩溃。
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover - exercised on machines without Pillow
    Image = ImageDraw = ImageFont = None  # type: ignore[assignment]


class RenderError(RuntimeError):
    # 渲染失败（没装 Pillow、找不到中文字体、内容超过最大张数）。
    # 这类错误不应该重试——重试也还是同样的结果。
    pass


@dataclass
class RenderedImage:
    # 一张已经生成并写入磁盘的卡片。
    # sha256 是文件内容哈希，审批单和发布都靠它确认“图没被换过”。
    ordinal: int
    path: Path
    sha256: str
    width: int
    height: int
    kind: str = "card"

    def as_dict(self) -> dict:
        return {
            "ordinal": self.ordinal,
            "path": str(self.path),
            "sha256": self.sha256,
            "width": self.width,
            "height": self.height,
            "kind": self.kind,
        }


BG = (247, 244, 238)
# 配色：米白底、近黑字、砖红强调色。RGB 三元组，数值越大越亮。
INK = (33, 32, 30)
MUTED = (116, 110, 100)
ACCENT = (206, 69, 53)
RULE = (220, 213, 201)

TITLE_SIZE = 68
# 字号（像素）。LINE_GAP 是行与行之间额外留的呼吸空间。
KICKER_SIZE = 28
HEADING_SIZE = 38
BODY_SIZE = 33
META_SIZE = 24
LINE_GAP = 12


class _Fonts:
    # 统一加载标题/正文/小字三种字体。标题和正文可以配不同字体，
    # 正文、章节标题、来源共用 render.body_font。
    def __init__(self, config: Config):
        if ImageFont is None:
            raise RenderError("Pillow is not installed; install Pillow or use the bundled Python runtime")
        self.title = self._load(config, "render.title_font", TITLE_SIZE)
        self.body = self._load(config, "render.body_font", BODY_SIZE)
        self.heading = self._load(config, "render.body_font", HEADING_SIZE)
        self.meta = self._load(config, "render.body_font", META_SIZE)
        self.card = self._load(config, "render.body_font", 46)
        self.point = self._load(config, "render.body_font", 30)

    @staticmethod
    def _load(config: Config, key: str, size: int):
        # 字体加载顺序：配置的主字体 -> 配置的备用字体列表 -> 报错。
        # 每个候选都要检查文件存在且能加载，避免某个字体只在 Mac 上有。
        # 一个中文字体都没找到时，不能退化成英文默认字体（中文会变方框），
        # 所以直接报错让用户去配置。
        candidates = [config.get(key)]
        fallbacks = config.get("render.fallback_fonts", []) or []
        candidates.extend(fallbacks)
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(str(candidate))
            if not path.exists():
                continue
            try:
                return ImageFont.truetype(str(path), size=size)
            except Exception:
                continue
        raise RenderError("no usable CJK font found; set render.body_font to a local .ttf/.ttc")


class _Line:
    # 排版的最小单位：一段已经量好宽高、可以直接画的文字。
    # keep_with_next 用于章节标题：不能让它单独留在页底。
    __slots__ = ("text", "font", "height", "space_before", "color", "keep_with_next")

    def __init__(
        self,
        text: str,
        font,
        height: int,
        space_before: int = 0,
        color=INK,
        keep_with_next: bool = False,
    ):
        self.text = text
        self.font = font
        self.height = height
        self.space_before = space_before
        self.color = color
        self.keep_with_next = keep_with_next


_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9._'’\-]*")


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    # 中文仍按字符换行；拉丁词（Fetch/RAG/lang）尽量整体保留，
    # 只有整个词比一行还宽时才退化为逐字符切分。
    lines: list[str] = []
    for raw_line in str(text).splitlines() or [""]:
        raw_line = raw_line.strip()
        if not raw_line:
            lines.append("")
            continue
        current = ""
        index = 0
        while index < len(raw_line):
            match = _LATIN_WORD.match(raw_line, index)
            if match:
                word = match.group()
                if draw.textlength(word, font=font) <= max_width:
                    if not current:
                        current = word
                    elif draw.textlength(current + word, font=font) <= max_width:
                        current += word
                    else:
                        if current.strip():
                            lines.append(current.rstrip())
                        current = word
                    index = match.end()
                    continue
            char = raw_line[index]
            candidate = current + char
            width = draw.textlength(candidate, font=font)
            if width <= max_width or not current:
                current = candidate
            else:
                if current.strip():
                    lines.append(current.rstrip())
                current = char
            index += 1
        if current:
            lines.append(current)
    return lines or [""]


def build_lines(content: DraftContent, config: Config, fonts: _Fonts, max_width: int) -> list[_Line]:
    # 把结构化稿件转换成“待绘制的行列表”：
    #   章节标题（砖红色）-> 段落（自动换行）-> 段后空白 -> 下一个章节；
    #   最后是“来源”区，每条来源分两行：名称+日期、URL。
    # 注意：这里只排版，不画图；分页和绘制在 render_draft 里。
    from PIL import Image as _Image, ImageDraw as _ImageDraw

    probe = _ImageDraw.Draw(_Image.new("RGB", (10, 10)))
    lines: list[_Line] = []

    def add_wrapped(text: str, font, size: int, *, space_before=0, color=INK):
        for index, piece in enumerate(_wrap(probe, text, font, max_width)):
            lines.append(
                _Line(piece, font, size + LINE_GAP, space_before if index == 0 else 0, color)
            )

    for key in SECTION_ORDER:
        section = content.section(key)
        if section is None:
            continue
        lines.append(
            _Line(
                f"【{section.heading}】",
                fonts.heading,
                HEADING_SIZE + 22,
                10,
                ACCENT,
                keep_with_next=True,
            )
        )
        for paragraph in section.paragraphs:
            add_wrapped(paragraph, fonts.body, BODY_SIZE)
            lines.append(_Line("", fonts.body, 8))
    lines.append(
        _Line("【来源】", fonts.heading, HEADING_SIZE + 22, 10, ACCENT, keep_with_next=True)
    )
    for index, source in enumerate(content.sources, start=1):
        if source.published_at and source.date_precision != "unknown":
            stamp = source.published_at[:10]
        else:
            stamp = "发布时间未知"
        add_wrapped(f"{index}. {source.name}（{stamp}）", fonts.meta, META_SIZE, color=MUTED)
        add_wrapped(source.url, fonts.meta, META_SIZE, color=MUTED)
    return lines


def _height(lines: list[_Line]) -> int:
    return sum(line.space_before + line.height for line in lines)


def _paginate(
    lines: list[_Line],
    *,
    available: int,
    cover: list[_Line],
    continuation_header,
    max_cards: int,
) -> list[list[_Line]]:
    pages: list[list[_Line]] = []
    current: list[_Line] = []
    y = 0

    def new_page() -> None:
        nonlocal current, y
        if current:
            pages.append(current)
        current = []
        y = 0

    cover_height = _height(cover)
    if cover_height + 240 > available:
        pages.append(cover)
        current = continuation_header()
        y = _height(current)
    else:
        current = list(cover)
        y = cover_height

    for index, line in enumerate(lines):
        need = line.space_before + line.height
        # A section heading must keep its first content line on the same page.
        if line.keep_with_next:
            # Keep the heading together with the first non-empty content line,
            # including any blank spacer lines between them.
            keep_height = need
            for following in lines[index + 1 :]:
                keep_height += following.space_before + following.height
                if following.text:
                    break
            if y + keep_height > available:
                new_page()
                current.extend(continuation_header())
                y = _height(current)
        if y + need > available:
            new_page()
            current.extend(continuation_header())
            y = _height(current)
        current.append(line)
        y += need
    if current or not pages:
        pages.append(current)
    if len(pages) > max_cards:
        raise RenderError(
            f"content needs {len(pages)} cards but render.max_cards={max_cards}; shorten the draft"
        )
    return pages


def render_draft(
    content: DraftContent,
    *,
    out_dir: str | Path,
    config: Config,
    draft_key: str,
) -> list[RenderedImage]:
    """Render one or more PNG cards.  Raises on overflow instead of truncating."""
    # New drafts carry an independent storyboard.  Each card becomes exactly one
    # PNG; the caption is never flowed into images.  Legacy drafts keep the old
    # deterministic section rendering so immutable approvals are unchanged.
    if content.storyboard is not None:
        return _render_storyboard(content, out_dir=out_dir, config=config, draft_key=draft_key)
    if Image is None or ImageDraw is None:
        raise RenderError("Pillow is not installed; PNG rendering is unavailable")

    width = int(config.get("render.width", 1080))
    height = int(config.get("render.height", 1440))
    margin = int(config.get("render.margin", 72))
    max_cards = int(config.get("render.max_cards", 6))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    fonts = _Fonts(config)

    inner_width = width - margin * 2
    content_top = margin + 150
    content_bottom = height - margin - 70
    lines = build_lines(content, config, fonts, inner_width)

    # Cover block: the top bar already carries the account label, so do not
    # repeat it here.  draft_key is internal bookkeeping and stays in the file
    # name only, not in the publishable pixels.
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    cover: list[_Line] = []
    for piece in _wrap(probe, content.title, fonts.title, inner_width):
        cover.append(_Line(piece, fonts.title, TITLE_SIZE + 20, 12, INK))
    for piece in _wrap(probe, "公开资料整理 · 未做个人实测", fonts.meta, inner_width):
        cover.append(_Line(piece, fonts.meta, META_SIZE + 16, 14, MUTED))
    cover.append(_Line("", fonts.meta, 10))

    available = content_bottom - content_top

    def continuation_header() -> list[_Line]:
        return [
            _Line(content.title, fonts.heading, HEADING_SIZE + 24, 6, INK),
            _Line("", fonts.meta, 10),
        ]

    pages = _paginate(
        lines,
        available=available,
        cover=cover,
        continuation_header=continuation_header,
        max_cards=max_cards,
    )

    rendered: list[RenderedImage] = []
    for page_index, page_lines in enumerate(pages, start=1):
        # 每页都重新画一张完整画布：
        #   顶部砖红条 -> 栏目名 -> 页码 -> 分隔线 -> 正文 -> 底部分隔线 -> 页脚。
        image = Image.new("RGB", (width, height), BG)
        draw = ImageDraw.Draw(image)
        draw.rectangle([0, 0, width, 14], fill=ACCENT)
        draw.text((margin, margin - 14), "AI 工具观察", font=fonts.meta, fill=ACCENT)
        draw.text(
            (width - margin - 140, margin - 14),
            f"{page_index}/{len(pages)}",
            font=fonts.meta,
            fill=MUTED,
        )
        draw.line([(margin, margin + 40), (width - margin, margin + 40)], fill=RULE, width=2)
        y = content_top
        if page_index > 1:
            # Continuation pages flow from the same top margin, header included.
            y = content_top
        for line in page_lines:
            y += line.space_before
            if line.text:
                draw.text((margin, y), line.text, font=line.font, fill=line.color)
            y += line.height
        draw.line([(margin, height - margin - 24), (width - margin, height - margin - 24)], fill=RULE, width=2)
        footer = (
            "公开资料整理，未做个人实测 · "
            + local_iso(datetime.now(timezone.utc), config.timezone_name)[:10]
        )
        draw.text((margin, height - margin - 4), footer, font=fonts.meta, fill=MUTED)
        filename = f"{draft_key}-{page_index:02d}.png"
        # 文件名带草稿编号和两位页码，例如 d_abc123-01.png。
        # 先保存再读回来算哈希：确保哈希对应的是真正写到磁盘的字节，
        # 而不是内存中“以为会写进去”的内容。
        path = out / filename
        image.save(path, format="PNG", optimize=True)
        with path.open("rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        rendered.append(
            RenderedImage(
                ordinal=page_index,
                path=path,
                sha256=digest,
                width=width,
                height=height,
            )
        )
    return rendered


def _storyboard_wrap(
    draw,
    text: str,
    font,
    fill,
    x: int,
    y: int,
    max_width: int,
    *,
    line_height: int,
) -> int:
    for piece in _wrap(draw, text, font, max_width):
        if piece:
            draw.text((x, y), piece, font=font, fill=fill)
        y += line_height
    return y


def _require_room(y: int, bottom: int, label: str) -> None:
    if y > bottom:
        raise RenderError(f"{label} does not fit on one card; shorten it or split the storyboard")


def _draw_storyboard_frame(
    draw,
    *,
    width: int,
    height: int,
    margin: int,
    page: int,
    total: int,
    account_label: str,
    config: Config,
    fonts: "_Fonts",
) -> None:
    draw.rectangle([0, 0, width, 14], fill=ACCENT)
    draw.text((margin, margin - 14), account_label, font=fonts.meta, fill=ACCENT)
    draw.text(
        (width - margin - 140, margin - 14),
        f"{page}/{total}",
        font=fonts.meta,
        fill=MUTED,
    )
    draw.line([(margin, margin + 40), (width - margin, margin + 40)], fill=RULE, width=2)
    draw.line(
        [(margin, height - margin - 24), (width - margin, height - margin - 24)],
        fill=RULE,
        width=2,
    )
    footer = (
        "公开资料整理，未做个人实测 · "
        + local_iso(datetime.now(timezone.utc), config.timezone_name)[:10]
    )
    draw.text((margin, height - margin - 4), footer, font=fonts.meta, fill=MUTED)


def _render_storyboard(
    content: DraftContent,
    *,
    out_dir: str | Path,
    config: Config,
    draft_key: str,
) -> list[RenderedImage]:
    """Render one independent PNG per storyboard card.

    Local text and geometric primitives only: no image model, no stock art and
    no copied source imagery.  A single cover is a complete storyboard.
    """
    if Image is None or ImageDraw is None:
        raise RenderError("Pillow is not installed; PNG rendering is unavailable")
    board = content.storyboard
    if board is None:
        raise RenderError("storyboard is missing")
    width = int(config.get("render.width", 1080))
    height = int(config.get("render.height", 1440))
    margin = int(config.get("render.margin", 72))
    max_cards = int(config.get("render.max_cards", 6))
    cards = board.all_cards()
    if len(cards) > max_cards:
        raise RenderError(
            f"storyboard needs {len(cards)} cards but render.max_cards={max_cards}"
        )
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    fonts = _Fonts(config)
    inner = width - margin * 2
    content_top = margin + 150
    bottom = height - margin - 70
    account = str(config.account().get("display_name") or "AI 工具观察")
    rendered: list[RenderedImage] = []
    for page, card in enumerate(cards, start=1):
        image = Image.new("RGB", (width, height), BG)
        draw = ImageDraw.Draw(image)
        _draw_storyboard_frame(
            draw,
            width=width,
            height=height,
            margin=margin,
            page=page,
            total=len(cards),
            account_label=account,
            config=config,
            fonts=fonts,
        )
        y = content_top
        layout = card.layout
        if layout == "cover":
            y = _draw_cover_card(
                draw, card, x=margin, y=y + 30, max_width=inner, bottom=bottom, fonts=fonts
            )
        else:
            if card.kicker:
                draw.text((margin, y), card.kicker, font=fonts.meta, fill=ACCENT)
                y += 48
            y = _storyboard_wrap(
                draw,
                card.headline,
                fonts.card,
                INK,
                margin,
                y,
                inner,
                line_height=62,
            )
            if card.subhead and layout != "annotated":
                y += 10
                y = _storyboard_wrap(
                    draw,
                    card.subhead,
                    fonts.body,
                    MUTED,
                    margin,
                    y,
                    inner,
                    line_height=46,
                )
            _require_room(y + 20, bottom, card.headline)
            if layout == "comparison":
                y = _draw_comparison_card(
                    draw, card, x=margin, y=y + 30, width=inner, fonts=fonts
                )
            elif layout == "process":
                y = _draw_process_card(
                    draw, card, x=margin, y=y + 30, width=inner, fonts=fonts
                )
            elif layout == "keypoints":
                y = _draw_keypoints_card(
                    draw, card, x=margin, y=y + 30, width=inner, fonts=fonts
                )
            elif layout == "diagram":
                y = _draw_diagram_card(
                    draw, card, x=margin, y=y + 30, width=inner, fonts=fonts
                )
            elif layout == "argument":
                y = _draw_argument_card(
                    draw, card, x=margin, y=y + 30, width=inner, fonts=fonts
                )
            elif layout == "annotated":
                y = _draw_annotated_card(
                    draw, card, x=margin, y=y + 10, width=inner, fonts=fonts
                )
            else:
                raise RenderError(f"unsupported storyboard layout: {layout}")
            if card.note:
                _require_room(y + 20, bottom, "card note")
                y = _storyboard_wrap(
                    draw,
                    card.note,
                    fonts.meta,
                    MUTED,
                    margin,
                    y + 12,
                    inner,
                    line_height=34,
                )
        _require_room(y, bottom, f"storyboard card {page}")
        path = out / f"{draft_key}-{page:02d}.png"
        image.save(path, format="PNG", optimize=True)
        with path.open("rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        rendered.append(
            RenderedImage(
                ordinal=page,
                path=path,
                sha256=digest,
                width=width,
                height=height,
            )
        )
    return rendered


def _draw_cover_card(
    draw,
    card: ImageCard,
    *,
    x: int,
    y: int,
    max_width: int,
    bottom: int,
    fonts: "_Fonts",
) -> int:
    if card.kicker:
        draw.text((x, y), card.kicker, font=fonts.meta, fill=ACCENT)
        y += 56
    y = _storyboard_wrap(
        draw, card.headline, fonts.title, INK, x, y, max_width, line_height=86
    )
    if card.subhead:
        y += 24
        y = _storyboard_wrap(
            draw, card.subhead, fonts.body, MUTED, x, y, max_width, line_height=48
        )
    if card.note:
        y += 28
        y = _storyboard_wrap(
            draw, card.note, fonts.meta, MUTED, x, y, max_width, line_height=36
        )
    _require_room(y, bottom, "cover text")
    return y


def _draw_comparison_card(
    draw, card: ImageCard, *, x: int, y: int, width: int, fonts: "_Fonts"
) -> int:
    gap = 24
    panel_width = (width - gap) // 2
    wrapped = [
        _wrap(draw, column.text, fonts.point, panel_width - 48)
        for column in card.columns
    ]
    panel_height = max(260, 100 + max((len(lines) for lines in wrapped), default=1) * 44)
    for index, column in enumerate(card.columns):
        left = x + index * (panel_width + gap)
        draw.rounded_rectangle(
            [left, y, left + panel_width, y + panel_height],
            radius=18,
            fill=(255, 255, 255),
            outline=RULE,
            width=2,
        )
        draw.text((left + 24, y + 24), column.label, font=fonts.meta, fill=ACCENT)
        _storyboard_wrap(
            draw,
            column.text,
            fonts.point,
            INK,
            left + 24,
            y + 80,
            panel_width - 48,
            line_height=44,
        )
    return y + panel_height


def _draw_process_card(
    draw, card: ImageCard, *, x: int, y: int, width: int, fonts: "_Fonts"
) -> int:
    for index, point in enumerate(card.points):
        lines = _wrap(draw, point, fonts.point, width - 82)
        row_height = max(110, 54 + len(lines) * 44)
        center = (x + 28, y + 28)
        if index + 1 < len(card.points):
            draw.line(
                [(center[0], center[1] + 30), (center[0], y + row_height + 10)],
                fill=RULE,
                width=4,
            )
        draw.ellipse(
            [center[0] - 28, center[1] - 28, center[0] + 28, center[1] + 28],
            fill=ACCENT,
        )
        number = str(index + 1)
        draw.text(
            (center[0] - 8, center[1] - 18),
            number,
            font=fonts.meta,
            fill=(255, 255, 255),
        )
        _storyboard_wrap(
            draw,
            point,
            fonts.point,
            INK,
            x + 82,
            y + 2,
            width - 82,
            line_height=44,
        )
        y += row_height
    return y - 30


def _draw_keypoints_card(
    draw, card: ImageCard, *, x: int, y: int, width: int, fonts: "_Fonts"
) -> int:
    for index, point in enumerate(card.points):
        lines = _wrap(draw, point, fonts.point, width - 84)
        row_height = max(90, len(lines) * 44 + 24)
        draw.rectangle([x, y + 4, x + 5, y + min(56, row_height - 12)], fill=ACCENT)
        draw.text((x + 22, y), f"{index + 1:02d}", font=fonts.meta, fill=ACCENT)
        _storyboard_wrap(
            draw,
            point,
            fonts.point,
            INK,
            x + 84,
            y,
            width - 84,
            line_height=44,
        )
        y += row_height
    return y - 20


def _draw_diagram_card(
    draw, card: ImageCard, *, x: int, y: int, width: int, fonts: "_Fonts"
) -> int:
    # Layered blocks, deliberately without arrows.  A diagram must not imply a
    # sequence the source did not state.
    gap = 18
    for point in card.points:
        lines = _wrap(draw, point, fonts.point, width - 60)
        box_height = max(100, 40 + len(lines) * 44)
        draw.rounded_rectangle(
            [x, y, x + width, y + box_height],
            radius=16,
            fill=(255, 255, 255),
            outline=RULE,
            width=2,
        )
        draw.rectangle([x, y, x + 8, y + box_height], fill=ACCENT)
        _storyboard_wrap(
            draw,
            point,
            fonts.point,
            INK,
            x + 30,
            y + 20,
            width - 60,
            line_height=44,
        )
        y += box_height + gap
    return y - gap


def _draw_argument_card(
    draw, card: ImageCard, *, x: int, y: int, width: int, fonts: "_Fonts"
) -> int:
    for index, point in enumerate(card.points):
        lines = _wrap(draw, point, fonts.point, width - 44)
        box_height = max(108, 66 + len(lines) * 42)
        draw.rounded_rectangle(
            [x, y, x + width, y + box_height],
            radius=14,
            fill=(255, 255, 255),
            outline=RULE,
            width=2,
        )
        draw.text((x + 22, y + 18), f"理由 {index + 1}", font=fonts.meta, fill=ACCENT)
        _storyboard_wrap(
            draw,
            point,
            fonts.point,
            INK,
            x + 22,
            y + 54,
            width - 44,
            line_height=42,
        )
        y += box_height + 18
    return y - 18


def _draw_annotated_card(
    draw, card: ImageCard, *, x: int, y: int, width: int, fonts: "_Fonts"
) -> int:
    quote_lines = _wrap(draw, card.subhead, fonts.point, width - 56)
    quote_height = max(180, 82 + len(quote_lines) * 44)
    draw.rounded_rectangle(
        [x, y, x + width, y + quote_height],
        radius=16,
        fill=(255, 255, 255),
        outline=RULE,
        width=2,
    )
    draw.rectangle([x, y, x + 8, y + quote_height], fill=ACCENT)
    draw.text((x + 28, y + 18), "来源摘录", font=fonts.meta, fill=ACCENT)
    _storyboard_wrap(
        draw,
        card.subhead,
        fonts.point,
        INK,
        x + 28,
        y + 58,
        width - 56,
        line_height=44,
    )
    y += quote_height + 24
    for index, point in enumerate(card.points):
        lines = _wrap(draw, point, fonts.point, width - 76)
        row_height = max(80, len(lines) * 42 + 18)
        draw.text((x, y), f"注 {index + 1}", font=fonts.meta, fill=ACCENT)
        _storyboard_wrap(
            draw,
            point,
            fonts.point,
            INK,
            x + 76,
            y,
            width - 76,
            line_height=42,
        )
        y += row_height
    return y - 18


def images_digest(paths: list[str | Path]) -> str:
    """Digest over image *content*, in ordinal order."""
    # 把所有图片的内容哈希按顺序拼起来再算一次总哈希。
    # 与 security.images_digest 的区别：这里直接读文件，适合“渲染完立刻算”；
    # 那边读数据库里的哈希，适合审批校验。
    parts = []
    for path in paths:
        parts.append(sha256_file(path))
    return hashlib.sha256("\n".join(parts).encode("ascii")).hexdigest()

"""Visual theme registry, palettes, and local font availability probing."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
THEMES_CSS_DIR = ASSETS_DIR / "themes"
DEFAULT_FONT_DIRS = ["assets/fonts", "C:/Windows/Fonts"]


@dataclass(frozen=True)
class Palette:
    name: str
    bg: str
    ink: str
    muted: str
    accent: str
    accent2: str
    surface: str
    rule: str
    extra: dict[str, str] = field(default_factory=dict)

    def css_vars(self) -> dict[str, str]:
        vars_map = {
            "--bg": self.bg,
            "--ink": self.ink,
            "--muted": self.muted,
            "--accent": self.accent,
            "--accent2": self.accent2,
            "--surface": self.surface,
            "--rule": self.rule,
        }
        vars_map.update(self.extra)
        return vars_map


@dataclass(frozen=True)
class Theme:
    id: str
    name: str
    summary: str
    css_file: str
    tags: tuple[str, ...]
    palettes: tuple[Palette, ...]
    fonts: dict[str, tuple[str, ...]]
    folio_style: str = "standard"
    available: bool = True
    resolved_fonts: dict[str, Path] = field(default_factory=dict)


THEMES: dict[str, Theme] = {
    "editorial": Theme(
        id="editorial",
        name="杂志社论",
        summary="米白纸感 + 衬线大标题 + 细线分栏，适合观点、争议与行业评论",
        css_file="editorial.css",
        tags=("argument", "quote", "annotated", "caption_led", "balanced", "cover"),
        palettes=(
            Palette(
                name="terracotta",
                bg="#F3EFE6",
                ink="#1B1A17",
                muted="#6B655A",
                accent="#B8361F",
                accent2="#D49A2A",
                surface="rgba(255, 255, 255, 0.72)",
                rule="#C9C1B2",
                extra={"--accent-soft": "rgba(184, 54, 31, 0.12)"},
            ),
            Palette(
                name="forest",
                bg="#F4F3EC",
                ink="#18221D",
                muted="#5C6860",
                accent="#1F5E45",
                accent2="#B97B2E",
                surface="rgba(255, 255, 255, 0.76)",
                rule="#C6CEC7",
                extra={"--accent-soft": "rgba(31, 94, 69, 0.12)"},
            ),
            Palette(
                name="navy",
                bg="#F2F0EB",
                ink="#171C28",
                muted="#5D6475",
                accent="#1E3A6E",
                accent2="#C44D34",
                surface="rgba(255, 255, 255, 0.78)",
                rule="#C5C9D3",
                extra={"--accent-soft": "rgba(30, 58, 110, 0.12)"},
            ),
        ),
        fonts={
            "display": ("NotoSerifSC-VF.ttf", "NotoSansSC-VF.ttf", "simsun.ttc", "msyhbd.ttc", "msyh.ttc"),
            "body": ("NotoSansSC-VF.ttf", "AlibabaPuHuiTi_2_55_Regular.ttf", "msyh.ttc", "Deng.ttf"),
        },
    ),
    "swiss": Theme(
        id="swiss",
        name="瑞士网格",
        summary="高反差白灰网格 + 超粗黑体 + 高饱和色块，适合清单、要点与对比",
        css_file="swiss.css",
        tags=("comparison", "checklist", "keypoints", "stat", "balanced"),
        palettes=(
            Palette(
                name="klein",
                bg="#F6F7F9",
                ink="#121417",
                muted="#5B626E",
                accent="#1746E0",
                accent2="#121417",
                surface="#FFFFFF",
                rule="#D6DAE0",
                extra={"--accent-soft": "rgba(23, 70, 224, 0.15)"},
            ),
            Palette(
                name="signal",
                bg="#F4F4F2",
                ink="#141413",
                muted="#605E59",
                accent="#E84A1B",
                accent2="#1F6F78",
                surface="#FFFFFF",
                rule="#DCDAD3",
                extra={"--accent-soft": "rgba(232, 74, 27, 0.15)"},
            ),
            Palette(
                name="acid",
                bg="#F2F5F3",
                ink="#101814",
                muted="#54635B",
                accent="#009E54",
                accent2="#101814",
                surface="#FFFFFF",
                rule="#CFE0D6",
                extra={"--accent-soft": "rgba(0, 158, 84, 0.15)"},
            ),
        ),
        fonts={
            "display": ("AlibabaPuHuiTi_2_65_Medium.ttf", "NotoSansSC-VF.ttf", "msyhbd.ttc", "simhei.ttf"),
            "body": ("AlibabaPuHuiTi_2_55_Regular.ttf", "NotoSansSC-VF.ttf", "msyh.ttc", "Deng.ttf"),
        },
        folio_style="swiss",
    ),
    "terminal": Theme(
        id="terminal",
        name="暗色科技",
        summary="深色网格 + 径向辉光 + 玻璃卡片，适合开发者工具、模型与代码",
        css_file="terminal.css",
        tags=("diagram", "process", "comparison", "stat", "code"),
        palettes=(
            Palette(
                name="aurora",
                bg="#0E1116",
                ink="#E9EDF2",
                muted="#8B95A5",
                accent="#7AA2FF",
                accent2="#C18CFF",
                surface="rgba(255, 255, 255, 0.06)",
                rule="rgba(255, 255, 255, 0.14)",
                extra={"--glow": "rgba(91, 140, 255, 0.42)", "--accent-soft": "rgba(122, 162, 255, 0.16)"},
            ),
            Palette(
                name="matrix",
                bg="#091310",
                ink="#E6F4EE",
                muted="#7CA395",
                accent="#2CEAA3",
                accent2="#38BDF8",
                surface="rgba(255, 255, 255, 0.06)",
                rule="rgba(255, 255, 255, 0.14)",
                extra={"--glow": "rgba(44, 234, 163, 0.34)", "--accent-soft": "rgba(44, 234, 163, 0.16)"},
            ),
            Palette(
                name="amber",
                bg="#13100E",
                ink="#F3EDE6",
                muted="#A59586",
                accent="#FFAD42",
                accent2="#FF6B6B",
                surface="rgba(255, 255, 255, 0.06)",
                rule="rgba(255, 255, 255, 0.14)",
                extra={"--glow": "rgba(255, 173, 66, 0.32)", "--accent-soft": "rgba(255, 173, 66, 0.16)"},
            ),
        ),
        fonts={
            "display": ("AlibabaPuHuiTi_2_65_Medium.ttf", "NotoSansSC-VF.ttf", "msyhbd.ttc", "simhei.ttf"),
            "body": ("NotoSansSC-VF.ttf", "AlibabaPuHuiTi_2_55_Regular.ttf", "msyh.ttc", "Deng.ttf"),
            "mono": ("JetBrainsMono-Regular.ttf", "consola.ttf", "NotoSansSC-VF.ttf", "msyh.ttc"),
        },
    ),
    "newsprint": Theme(
        id="newsprint",
        name="报纸剪报",
        summary="灰白新闻纸 + 双线报头 + 印章框，适合新闻事件与时间线",
        css_file="newsprint.css",
        tags=("timeline", "news", "quote", "comparison", "annotated"),
        palettes=(
            Palette(
                name="classic",
                bg="#EAE7DF",
                ink="#161614",
                muted="#59564E",
                accent="#B82923",
                accent2="#284B63",
                surface="rgba(255, 255, 255, 0.40)",
                rule="#A8A295",
                extra={"--accent-soft": "rgba(184, 41, 35, 0.12)"},
            ),
            Palette(
                name="sepia",
                bg="#ECE4D4",
                ink="#231D18",
                muted="#6C5E52",
                accent="#8C3B2B",
                accent2="#3D5A45",
                surface="rgba(255, 255, 255, 0.36)",
                rule="#B3A592",
                extra={"--accent-soft": "rgba(140, 59, 43, 0.12)"},
            ),
            Palette(
                name="slate",
                bg="#E6EAEE",
                ink="#141B24",
                muted="#546070",
                accent="#1E4D7B",
                accent2="#B5382D",
                surface="rgba(255, 255, 255, 0.44)",
                rule="#AEB8C4",
                extra={"--accent-soft": "rgba(30, 77, 123, 0.12)"},
            ),
        ),
        fonts={
            "display": ("NotoSerifSC-VF.ttf", "NotoSansSC-VF.ttf", "simsun.ttc", "msyhbd.ttc"),
            "body": ("NotoSansSC-VF.ttf", "NotoSerifSC-VF.ttf", "msyh.ttc", "Deng.ttf"),
        },
    ),
    "notebook": Theme(
        id="notebook",
        name="手账便签",
        summary="点阵纸 + 胶带贴纸 + 荧光笔高亮，适合教程、上手步骤与建议",
        css_file="notebook.css",
        tags=("process", "checklist", "qa", "keypoints", "tutorial"),
        palettes=(
            Palette(
                name="butter",
                bg="#FBF7EC",
                ink="#26231D",
                muted="#736C5E",
                accent="#2F6F4E",
                accent2="#D96B27",
                surface="#FFFFFF",
                rule="#DFD6C3",
                extra={"--highlight-bg": "#FCE36D", "--accent-soft": "rgba(47, 111, 78, 0.12)"},
            ),
            Palette(
                name="mint",
                bg="#F2F8F4",
                ink="#1D2621",
                muted="#5F7067",
                accent="#D95D39",
                accent2="#2B6CB0",
                surface="#FFFFFF",
                rule="#C9DDD0",
                extra={"--highlight-bg": "#FFD59E", "--accent-soft": "rgba(217, 93, 57, 0.12)"},
            ),
            Palette(
                name="lilac",
                bg="#F6F3FA",
                ink="#221E29",
                muted="#6B627A",
                accent="#5B42B0",
                accent2="#D97706",
                surface="#FFFFFF",
                rule="#D8D0E6",
                extra={"--highlight-bg": "#FDE68A", "--accent-soft": "rgba(91, 66, 176, 0.12)"},
            ),
        ),
        fonts={
            "display": ("LXGWWenKai-Regular.ttf", "NotoSerifSC-VF.ttf", "NotoSansSC-VF.ttf", "simkai.ttf", "msyh.ttc"),
            "body": ("LXGWWenKai-Regular.ttf", "NotoSansSC-VF.ttf", "msyh.ttc", "Deng.ttf"),
        },
    ),
    "poster": Theme(
        id="poster",
        name="大字海报",
        summary="全幅饱和底色 + 超大标题 + 极简元素，适合单封面强钩子",
        css_file="poster.css",
        tags=("cover", "caption_led", "stat", "question", "split"),
        palettes=(
            Palette(
                name="crimson",
                bg="#B83227",
                ink="#FFFFFF",
                muted="rgba(255, 255, 255, 0.80)",
                accent="#FFE45E",
                accent2="#FFE45E",
                surface="rgba(0, 0, 0, 0.22)",
                rule="rgba(255, 255, 255, 0.25)",
                extra={"--accent-soft": "rgba(255, 228, 94, 0.22)"},
            ),
            Palette(
                name="cobalt",
                bg="#193DC4",
                ink="#FFFFFF",
                muted="rgba(255, 255, 255, 0.80)",
                accent="#9BFFB5",
                accent2="#9BFFB5",
                surface="rgba(0, 0, 0, 0.22)",
                rule="rgba(255, 255, 255, 0.25)",
                extra={"--accent-soft": "rgba(155, 255, 181, 0.22)"},
            ),
            Palette(
                name="emerald",
                bg="#0F5A43",
                ink="#FFFFFF",
                muted="rgba(255, 255, 255, 0.80)",
                accent="#F9E56B",
                accent2="#F9E56B",
                surface="rgba(0, 0, 0, 0.22)",
                rule="rgba(255, 255, 255, 0.25)",
                extra={"--accent-soft": "rgba(249, 229, 107, 0.22)"},
            ),
            Palette(
                name="charcoal",
                bg="#16171B",
                ink="#FFFFFF",
                muted="rgba(255, 255, 255, 0.76)",
                accent="#FF6A3D",
                accent2="#FF6A3D",
                surface="rgba(255, 255, 255, 0.08)",
                rule="rgba(255, 255, 255, 0.20)",
                extra={"--accent-soft": "rgba(255, 106, 61, 0.22)"},
            ),
        ),
        fonts={
            "display": ("AlibabaPuHuiTi_2_65_Medium.ttf", "NotoSansSC-VF.ttf", "msyhbd.ttc", "simhei.ttf"),
            "body": ("NotoSansSC-VF.ttf", "AlibabaPuHuiTi_2_55_Regular.ttf", "msyh.ttc", "Deng.ttf"),
        },
        folio_style="minimal",
    ),
}


def resolve_font_dirs(config: Any = None) -> list[Path]:
    raw_dirs = DEFAULT_FONT_DIRS
    if config is not None and hasattr(config, "get"):
        configured = config.get("render.font_dirs", None)
        if isinstance(configured, list) and configured:
            raw_dirs = [str(x) for x in configured if str(x).strip()]
    dirs: list[Path] = []
    for raw in raw_dirs:
        p = Path(raw)
        if not p.is_absolute() and config is not None and hasattr(config, "resolve_path"):
            p = config.resolve_path(raw)
        dirs.append(p)
    return dirs


def _find_font(candidates: tuple[str, ...], font_dirs: list[Path]) -> Path | None:
    for candidate in candidates:
        cand_path = Path(candidate)
        if cand_path.is_absolute():
            if cand_path.is_file():
                return cand_path
            continue
        for directory in font_dirs:
            probe = directory / candidate
            if probe.is_file():
                return probe
    return None


def resolve_theme(theme: Theme, config: Any = None) -> Theme:
    """Probe local fonts for a theme and return a resolved copy with availability."""
    font_dirs = resolve_font_dirs(config)
    resolved: dict[str, Path] = {}
    available = True
    for slot, candidates in theme.fonts.items():
        found = _find_font(candidates, font_dirs)
        if found is None:
            available = False
        else:
            resolved[slot] = found
    return Theme(
        id=theme.id,
        name=theme.name,
        summary=theme.summary,
        css_file=theme.css_file,
        tags=theme.tags,
        palettes=theme.palettes,
        fonts=theme.fonts,
        folio_style=theme.folio_style,
        available=available,
        resolved_fonts=resolved,
    )


def available_themes(
    config: Any = None, *, registry: dict[str, Theme] | None = None
) -> list[Theme]:
    source = registry if registry is not None else THEMES
    out: list[Theme] = []
    for theme in source.values():
        resolved = resolve_theme(theme, config)
        if resolved.available:
            out.append(resolved)
    return out


def theme_catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": t.id,
            "name": t.name,
            "summary": t.summary,
            "tags": list(t.tags),
        }
        for t in THEMES.values()
    ]

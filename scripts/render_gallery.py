"""Render visual_gallery.json across all 6 HTML themes and check §7.2 QA thresholds."""

from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path
import time

from PIL import Image, ImageDraw, ImageFont

from bot.config import DEFAULTS, Config
from bot.content import DraftContent
from bot.visual.engine import render_storyboard_html
from bot.visual.similarity import dhash, hamming, largest_blank_band
from bot.visual.themes import THEMES


def _make_html_config(root: Path) -> Config:
    data = json.loads(json.dumps(DEFAULTS))
    data["app"]["mode"] = "demo"
    data["render"]["engine"] = "html"
    data["render"]["theme_cooldown"] = 0
    return Config(path=root / "gallery.toml", root=root, data=data)


def _stitch_contact_sheet(
    rows: list[tuple[str, list[tuple[str, Path]]]],
    out_path: Path,
    *,
    thumb_w: int = 360,
    thumb_h: int = 480,
    pad: int = 20,
    header_h: int = 44,
) -> Path:
    if not rows:
        raise RuntimeError("no images to stitch")
    cols = max(len(r[1]) for r in rows)
    width = pad + cols * (thumb_w + pad)
    height = pad + len(rows) * (thumb_h + header_h + pad)
    sheet = Image.new("RGB", (width, height), (235, 233, 228))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()

    for r_idx, (case_id, items) in enumerate(rows):
        y0 = pad + r_idx * (thumb_h + header_h + pad)
        for c_idx, (theme_id, img_path) in enumerate(items):
            x0 = pad + c_idx * (thumb_w + pad)
            draw.text((x0 + 6, y0 + 8), f"{case_id} | {theme_id}", font=font, fill=(30, 30, 30))
            with Image.open(img_path) as im:
                thumb = im.convert("RGB").resize((thumb_w, thumb_h), Image.Resampling.LANCZOS)
            sheet.paste(thumb, (x0, y0 + header_h))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, format="PNG")
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render 6-theme visual gallery and check QA thresholds.")
    parser.add_argument("--fixture", default="fixtures/visual_gallery.json")
    parser.add_argument("--out", default="var/gallery")
    parser.add_argument("--strict", action="store_true", help="Exit 1 if any §7.2 threshold misses.")
    args = parser.parse_args(argv)

    project_root = Path(__file__).resolve().parent.parent
    fixture_path = (project_root / args.fixture).resolve()
    out_root = (project_root / args.out).resolve()
    cfg = _make_html_config(project_root)

    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    cases = list(payload.get("cases") or [])
    theme_ids = list(THEMES.keys())

    total_cards = 0
    total_elapsed = 0.0
    max_blank = 0.0
    blank_violations: list[tuple[str, float]] = []
    size_errors: list[str] = []
    render_errors: list[str] = []
    cover_hashes: dict[str, dict[str, int]] = {}
    contact_rows: list[tuple[str, list[tuple[str, Path]]]] = []

    for case in cases:
        case_id = str(case["id"])
        content = DraftContent.from_dict(case["content"])
        row_items: list[tuple[str, Path]] = []
        cover_hashes[case_id] = {}

        for theme_id in theme_ids:
            theme_dir = out_root / theme_id
            t0 = time.perf_counter()
            try:
                imgs = render_storyboard_html(
                    content,
                    out_dir=theme_dir,
                    config=cfg,
                    draft_key=case_id,
                    theme_override=theme_id,
                    run_key=f"{case_id}:{theme_id}",
                )
            except Exception as exc:
                render_errors.append(f"{case_id} [{theme_id}]: {exc}")
                continue
            dt = time.perf_counter() - t0
            total_elapsed += dt
            total_cards += len(imgs)

            for img in imgs:
                if (img.width, img.height) != (1080, 1440):
                    size_errors.append(f"{img.path}: {img.width}x{img.height}")
                _, ratio = largest_blank_band(img.path)
                if ratio > max_blank:
                    max_blank = ratio
                if ratio > 0.28:
                    blank_violations.append((f"{theme_id}/{img.path.name}", ratio))

            if imgs:
                row_items.append((theme_id, imgs[0].path))
                cover_hashes[case_id][theme_id] = dhash(imgs[0].path)

        contact_rows.append((case_id, row_items))

    contact_path = _stitch_contact_sheet(contact_rows, out_root / "contact.png")

    min_pair_dist = 64
    pair_warnings: list[str] = []
    for case_id, theme_map in cover_hashes.items():
        if len(theme_map) < len(theme_ids):
            continue
        for t1, t2 in itertools.combinations(theme_ids, 2):
            dist = hamming(theme_map[t1], theme_map[t2])
            if dist < min_pair_dist:
                min_pair_dist = dist
            if dist < 12:
                pair_warnings.append(f"{case_id}: {t1} vs {t2} dHash={dist}")

    avg_s = total_elapsed / max(1, total_cards)
    print("=== Visual Gallery QA Report ===")
    print(f"themes={len(theme_ids)} cases={len(cases)} total_cards={total_cards}")
    print(f"avg_seconds_per_card={avg_s:.3f} (limit <= 4.000)")
    print(f"max_blank_band={max_blank:.4f} (limit <= 0.2800)")
    print(f"min_pairwise_cover_dhash={min_pair_dist} (limit >= 12)")
    print(
        f"render_errors={len(render_errors)} size_errors={len(size_errors)} "
        f"blank_violations={len(blank_violations)} dhash_warnings={len(pair_warnings)}"
    )
    for err in render_errors:
        print(f"  [render_error] {err}")
    if blank_violations:
        for name, ratio in blank_violations[:10]:
            print(f"  [blank>0.28] {name}: {ratio:.4f}")
    if pair_warnings:
        for warn in pair_warnings[:10]:
            print(f"  [dhash<12] {warn}")
    print(f"contact_sheet={contact_path}")

    failed = bool(render_errors or size_errors or blank_violations or pair_warnings or avg_s > 4.0)
    if args.strict and failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

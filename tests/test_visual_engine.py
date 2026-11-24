from __future__ import annotations

import hashlib
import subprocess
import sys
import unittest
from pathlib import Path

from PIL import Image

from bot.content import DraftContent, EditorialPlan, ImageCard, ImageStoryboard
from bot.render import RenderError
from bot.visual.engine import BrowserLaunchError, _open_page, render_storyboard_html
from bot.visual.similarity import dhash, hamming, largest_blank_band
from tests.helpers import cleanup, new_env


def _can_launch_edge() -> bool:
    try:
        pw, browser, context, _page = _open_page(
            width=1080, height=1440, channel="msedge", executable=""
        )
        for c in (context, browser):
            if c is not None:
                c.close()
        if pw is not None:
            pw.stop()
        return True
    except Exception:
        return False


def _msedge_pids() -> set[int]:
    if sys.platform != "win32":
        return set()
    res = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
            "Where-Object { $_.CommandLine -match '--headless' } | "
            "Select-Object -ExpandProperty ProcessId",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    pids: set[int] = set()
    for line in res.stdout.splitlines():
        token = line.strip()
        if token.isdigit():
            pids.add(int(token))
    return pids


def _sample_content(headline: str = "98%相信是对的，\n但人没懂就不算数") -> DraftContent:
    return DraftContent(
        title="测试草稿",
        sections=[],
        claims=[],
        tags=["AI"],
        uncertainties=[],
        sources=[],
        caption=["正文测试段落。"],
        editorial_plan=EditorialPlan(
            audience_need="理解事实",
            angle="核心判断",
            takeaway="待验证",
            caption_image_division="图片钩子",
            rationale="简洁",
            strategy="image_led",
            visual_approach="封面",
        ),
        storyboard=ImageStoryboard(
            cover=ImageCard(
                layout="cover",
                headline=headline,
                kicker="AI 观察",
                subhead="梅纳德：真正关键的是人的理解",
                note="据科技日报报道",
                evidence_ids=["E1"],
            ),
            cards=[],
        ),
    )


class VisualEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = new_env("visual-eng")
        self.env.db.init()

    def tearDown(self) -> None:
        cleanup(self.env)

    def test_a9_missing_browser_executable_raises_launch_error(self) -> None:
        self.env.config.data["render"]["browser_executable"] = "Z:/nonexistent/msedge.exe"
        with self.assertRaises(BrowserLaunchError):
            render_storyboard_html(
                _sample_content(),
                out_dir=self.env.root / "out",
                config=self.env.config,
                draft_key="missing-exe",
                db=self.env.db,
            )

    @unittest.skipUnless(_can_launch_edge(), "Edge browser unavailable in current sandbox")
    def test_a6_a8_a11_render_security_overflow_and_process_cleanup(self) -> None:
        before_pids = _msedge_pids()

        # 1. A6: Escaped <script> and <img src="http://x"> render safely as literal text
        xss_content = _sample_content('标题 <script>alert(1)</script> <img src="http://x">')
        imgs = render_storyboard_html(
            xss_content,
            out_dir=self.env.root / "xss",
            config=self.env.config,
            draft_key="xss",
            db=self.env.db,
            theme_override="editorial",
        )
        self.assertEqual(len(imgs), 1)
        p = imgs[0].path
        self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(), imgs[0].sha256)
        with Image.open(p) as im:
            self.assertEqual(im.size, (1080, 1440))

        # Verify route guard actively aborts any external http:// request (count >= 1)
        aborted: list[str] = []
        pw, browser, context, page = _open_page(
            width=1080,
            height=1440,
            channel="msedge",
            executable="",
            aborted_counter=aborted,
        )
        try:
            page.set_content('<html><body><img src="http://example.com/pixel.png"></body></html>', wait_until="load")
        finally:
            context.close()
            browser.close()
            pw.stop()
        self.assertGreaterEqual(len(aborted), 1)
        self.assertIn("http://example.com/pixel.png", aborted)

        # 2. A8: 6 keypoints x 90 chars either fits without DOM overflow or raises RenderError
        dense_points = [f"要点{i}：" + "人工智能推理能力与形式化代码验证在实际工程中的边界条件必须逐条核实与检查测试验证完成记录。" * 2 for i in range(1, 7)]
        dense = DraftContent(
            title="压力测试",
            sections=[],
            claims=[],
            tags=[],
            uncertainties=[],
            sources=[],
            caption=["正文"],
            editorial_plan=EditorialPlan("n", "a", "t", "d", "r", "balanced", "v"),
            storyboard=ImageStoryboard(
                cover=ImageCard(layout="cover", headline="压力封面", evidence_ids=["E1"]),
                cards=[ImageCard(layout="keypoints", headline="超长要点卡片", points=dense_points, evidence_ids=["E1"])],
            ),
        )
        try:
            dense_imgs = render_storyboard_html(
                dense,
                out_dir=self.env.root / "dense",
                config=self.env.config,
                draft_key="dense",
                db=self.env.db,
                theme_override="swiss",
            )
            self.assertEqual(len(dense_imgs), 2)
        except RenderError as exc:
            self.assertIn("overflows", str(exc))

        # Extreme impossible overflow MUST raise RenderError (not silently clip)
        impossible = DraftContent(
            title="必溢测试",
            sections=[],
            claims=[],
            tags=[],
            uncertainties=[],
            sources=[],
            caption=["正文"],
            editorial_plan=EditorialPlan("n", "a", "t", "d", "r", "balanced", "v"),
            storyboard=ImageStoryboard(
                cover=ImageCard(layout="cover", headline="必溢封面", evidence_ids=["E1"]),
                cards=[
                    ImageCard(
                        layout="keypoints",
                        headline="必然溢出",
                        points=["超长内容反复重复占位文字用来撑爆容器最小字号无法再缩小。" * 10 for _ in range(6)],
                        evidence_ids=["E1"],
                    )
                ],
            ),
        )
        with self.assertRaises(RenderError) as err:
            render_storyboard_html(
                impossible,
                out_dir=self.env.root / "impossible",
                config=self.env.config,
                draft_key="impossible",
                db=self.env.db,
                theme_override="editorial",
            )
        self.assertIn("overflows", str(err.exception))

        # 3. A11 + similarity check across 2 themes
        imgs_term = render_storyboard_html(
            _sample_content(),
            out_dir=self.env.root / "term",
            config=self.env.config,
            draft_key="term",
            db=self.env.db,
            theme_override="terminal",
        )
        imgs_ed = render_storyboard_html(
            _sample_content(),
            out_dir=self.env.root / "ed",
            config=self.env.config,
            draft_key="ed",
            db=self.env.db,
            theme_override="editorial",
        )
        self.assertGreaterEqual(hamming(dhash(imgs_term[0].path), dhash(imgs_ed[0].path)), 12)
        _y, band = largest_blank_band(imgs_ed[0].path)
        self.assertLessEqual(band, 0.28)

        # No new leftover headless msedge processes spawned by rendering
        import time

        deadline = time.monotonic() + 2.0
        leaked = _msedge_pids() - before_pids
        while leaked and time.monotonic() < deadline:
            time.sleep(0.2)
            leaked = _msedge_pids() - before_pids
        self.assertFalse(leaked, f"leaked headless msedge pids: {leaked}")


if __name__ == "__main__":
    unittest.main()

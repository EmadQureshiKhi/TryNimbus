"""Live layout check: ask real questions, paint every frame of each walkthrough exactly as the app would,
and fail on any overlap, covered anchor or off-screen element. Offscreen and headless.

    .venv/bin/python tools/check_layout.py
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from PyQt6.QtCore import QPointF, QRectF  # noqa: E402
from PyQt6.QtGui import QImage, QPainter  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # runnable as tools/<name>.py

from nimbus.ask import AskService, Walkthrough  # noqa: E402
from nimbus.mac.control import AppContext  # noqa: E402
from nimbus.mac.screen import Capture, Display  # noqa: E402
from nimbus.meta.client import MetaClient  # noqa: E402
from nimbus.ui.overlay import Mark, Overlay, layout_problems  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIX = (ROOT / "tests" / "fixtures" / "ui").as_uri() + "/"
OUT = ROOT / "artifacts" / "evals" / "layout"
CASES = [
    (FIX + "whiteboard.html", "Solve problem 1 step by step"),
    (FIX + "whiteboard.html", "Walk me through solving problem 2"),
    (FIX + "chat_app.html", "How do I switch to Muse Spark 1.3 and send a message?"),
    (FIX + "chat_app.html?theme=dark", "Where do I start a new chat?"),
    (FIX + "video_editor.html", "Walk me through cutting a clip and then exporting the video"),
    (FIX + "video_editor.html", "What are all the tools in the toolbar?"),
    ("https://github.com/meta-models/meta-model-cookbook", "How do I clone this repo?"),
    ("https://vscode.dev", "Show me how to open the command palette and the extensions view"),
    ("https://excalidraw.com", "Which tools would I use to draw a flowchart?"),
]


def frames(walk: Walkthrough):
    """The marks, buddy position and caption on screen after each step, as `NimbusApp.play` shows them."""
    shown = [s for s in walk.steps if s.gx is not None]
    numbered = len(shown) > 1
    marks, n = [], 0
    yield [], None, walk.reply
    for step in walk.steps:
        buddy = None
        if step.kind != "say" and step.gx is not None:
            n += 1
            marks = marks + [Mark(kind={"point": "ring", "box": "box", "note": "note"}[step.kind], x=step.gx, y=step.gy,
                                  box=step.box if step.kind == "box" else None,
                                  label=step.target if step.kind != "note" else "", note=step.note,
                                  step=n if numbered else None, born=time.monotonic() - 2)]
            buddy = (step.gx, step.gy)
        yield marks, buddy, step.say


def paint(png: bytes, marks, buddy, caption, path: Path | None) -> dict:
    ov = Overlay()
    ov.windows = []
    for m in marks:
        ov.add_mark(m)
    ov.follow = False
    ov.buddy = QPointF(*buddy) if buddy else QPointF(720, 450)
    ov.set_caption(caption)
    canvas = QImage.fromData(png)
    canvas.setDevicePixelRatio(2.0)
    p = QPainter(canvas)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    ov.paint(p, QRectF(0, 0, 1440, 900))
    p.end()
    if path:
        canvas.save(str(path))
    return ov.drawn


async def shots() -> list[bytes]:
    out = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        for url, _ in CASES:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000 if url.startswith("http") else 300)
            out.append(await page.screenshot())
        await browser.close()
    return out


async def ask_all(pngs: list[bytes]) -> list[Walkthrough]:
    svc = AskService(MetaClient())

    async def one(i: int) -> Walkthrough:
        img = Image.open(io.BytesIO(pngs[i]))
        cap = Capture(Display(0, 1, 0, 0, 1440, 900, 2.0, True), pngs[i], img.width, img.height, time.time())
        local = AskService(svc.client)
        return await local.ask(CASES[i][1], [cap], AppContext("Chromium", "", CASES[i][0], 0), allow_operate=False)

    return list(await asyncio.gather(*(one(i) for i in range(len(CASES)))))


_APP: QApplication | None = None


def main() -> int:
    global _APP
    _APP = QApplication(sys.argv)  # painting text needs a Qt application
    OUT.mkdir(parents=True, exist_ok=True)
    pngs = asyncio.run(shots())
    walks = asyncio.run(ask_all(pngs))
    report, bad = [], 0
    for i, ((url, question), walk, png) in enumerate(zip(CASES, walks, pngs)):
        problems = []
        all_frames = list(frames(walk))
        for k, (marks, buddy, caption) in enumerate(all_frames):
            last = k == len(all_frames) - 1
            drawn = paint(png, marks, buddy, caption, OUT / f"case{i:02d}_final.png" if last else None)
            problems += [f"frame {k}: {p}" for p in layout_problems(drawn)]
        bad += bool(problems)
        report.append({"question": question, "url": url, "steps": len(walk.steps), "frames": len(all_frames),
                       "problems": problems})
        print(f"{'CLEAN' if not problems else 'ISSUES'}  {len(all_frames):2d} frames  {question}")
        for p in problems[:6]:
            print("        ", p)
    (OUT / "layout.json").write_text(json.dumps(report, indent=2))
    total = sum(r["frames"] for r in report)
    print(f"\n{len(CASES) - bad}/{len(CASES)} walkthroughs clean across {total} painted frames")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

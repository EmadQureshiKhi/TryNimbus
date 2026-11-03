"""Paint what Nimbus would draw for a question onto a fixture screenshot, offscreen.

    .venv/bin/python tools/render_overlay.py whiteboard.html "Solve problem 2 step by step" out.png
"""
from __future__ import annotations

import asyncio
import io
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

from nimbus.ask import AskService  # noqa: E402
from nimbus.mac.control import AppContext  # noqa: E402
from nimbus.mac.screen import Capture, Display  # noqa: E402
from nimbus.meta.client import MetaClient  # noqa: E402
from nimbus.ui.overlay import Mark, Overlay  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


async def shoot(name: str) -> bytes:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        await page.goto((ROOT / "tests" / "fixtures" / "ui" / name).as_uri())
        png = await page.screenshot()
        await browser.close()
    return png


def main() -> None:
    name, question, out = sys.argv[1], sys.argv[2], sys.argv[3]
    app = QApplication(sys.argv)
    png = asyncio.run(shoot(name))
    img = Image.open(io.BytesIO(png))
    cap = Capture(Display(0, 1, 0, 0, 1440, 900, 2.0, True), png, img.width, img.height, time.time())
    walk = asyncio.run(AskService(MetaClient()).ask(question, [cap], AppContext("Chromium", "", name, 0),
                                                    allow_operate=False))
    overlay = Overlay()
    shown = [s for s in walk.steps if s.gx is not None]
    numbered = len(shown) > 1
    for n, s in enumerate(shown, 1):
        overlay.add_mark(Mark(kind={"point": "ring", "box": "box", "note": "note"}[s.kind], x=s.gx, y=s.gy,
                              box=s.box if s.kind == "box" else None, label=s.target if s.kind != "note" else "",
                              note=s.note, step=n if numbered else None, born=time.monotonic() - 2))
    last = shown[-1] if shown else None
    if last:
        overlay.buddy = QPointF(last.gx, last.gy)
    overlay.follow = False
    overlay.set_caption(walk.steps[-1].say if walk.steps else walk.reply)
    canvas = QImage.fromData(png)
    canvas.setDevicePixelRatio(2.0)
    p = QPainter(canvas)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    overlay.paint(p, QRectF(0, 0, 1440, 900))
    p.end()
    canvas.save(out)
    print(walk.reply)
    for s in walk.steps:
        print(f"  [{s.kind}] {s.say}  {s.note}")
    app.quit()


if __name__ == "__main__":
    main()

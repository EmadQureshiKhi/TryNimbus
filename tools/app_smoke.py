"""End-to-end smoke test of the real app controller, entirely offscreen (no windows, no sound).

Drives `NimbusApp.handle_question` exactly as the hotkey or panel would, but the "screen" is a
fixture page rendered by headless Chromium. Checks that the answer reaches the panel and that the
buddy's marks land on the right control.

    .venv/bin/python tools/app_smoke.py
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["NIMBUS_HOME"] = str(Path(__file__).resolve().parent.parent / "artifacts" / "tmp" / "smoke_home")

from PIL import Image  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # runnable as tools/<name>.py

from nimbus.mac.control import AppContext  # noqa: E402
from nimbus.mac.screen import Capture, Display  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CASES = [("chat_app.html", "How do I start a new chat?", "#new-chat"),
         ("video_editor.html", "Where's the blade tool?", "#blade"),
         ("whiteboard.html", "Solve problem 1 step by step", "#eq1")]


async def render(name: str, selector: str):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        await page.goto((ROOT / "tests" / "fixtures" / "ui" / name).as_uri())
        png = await page.screenshot()
        box = await page.locator(selector).bounding_box()
        await browser.close()
    img = Image.open(io.BytesIO(png))
    return Capture(Display(0, 1, 0, 0, 1440, 900, 2.0, True), png, img.width, img.height, time.time()), box


def main() -> int:
    from nimbus.app import NimbusApp

    qt = QApplication(sys.argv)
    app = NimbusApp(qt)
    app.settings.speak_answers = False  # silent
    app.overlay.show()
    failures: list[str] = []
    state = {"i": 0}

    def run_next() -> None:
        if state["i"] >= len(CASES):
            app.runner.stop()
            qt.quit()
            return
        name, question, selector = CASES[state["i"]]
        state["i"] += 1
        cap, box = asyncio.run(render(name, selector))

        async def fake_captures():
            return [cap]

        app.capture_for_question = fake_captures
        import nimbus.app as appmod
        appmod.control.frontmost_app = lambda: AppContext("Chromium", "", name, 0)
        app.overlay.marks.clear()
        app.panel.conversation.findChildren  # noqa: B018 (touch to keep the widget alive)
        started = time.perf_counter()
        future = app.runner.submit(app.handle_question(question))

        def poll() -> None:
            if not future.done():
                QTimer.singleShot(100, poll)
                return
            elapsed = time.perf_counter() - started
            exc = future.exception()
            marks = [(m.kind, round(m.x), round(m.y)) for m in app.overlay.marks]
            inside = [box["x"] - 8 <= m.x <= box["x"] + box["width"] + 8 and
                      box["y"] - 8 <= m.y <= box["y"] + box["height"] + 260 for m in app.overlay.marks]
            ok = exc is None and marks and (inside[0] if "whiteboard" not in name else all(inside))
            print(f"{'PASS' if ok else 'FAIL'} {elapsed:4.1f}s  {question!r} -> marks {marks}  box=({box['x']:.0f},"
                  f"{box['y']:.0f},{box['width']:.0f}x{box['height']:.0f})" + (f"  error={exc}" if exc else ""))
            if not ok:
                failures.append(question)
            QTimer.singleShot(50, run_next)

        QTimer.singleShot(100, poll)

    QTimer.singleShot(200, run_next)
    qt.exec()
    cards = app.panel.convo_lay.count() - 2  # minus the empty state and the stretch
    print(f"panel answers: {cards}")
    return 1 if failures or cards < len(CASES) else 0


if __name__ == "__main__":
    sys.exit(main())

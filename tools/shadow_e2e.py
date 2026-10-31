"""Shadow end to end on the real app, invisibly, with a recording of the run.

The real controller (`NimbusApp`), panel, overlay and `ShadowSession` run on an offscreen Qt screen
the size of a 1440x900 Retina Mac. "The screen" is the Chatbook fixture in headless Chromium, in the
dark theme the tutorial video never shows. A simulated person follows the buddy the way a user
would: clicks where it points, types when asked, and also makes a wrong click, presses Show me, and
lets Nimbus do the last step (the computer-use agent drives the hidden page, never the real Mac).

Checks: every pointer lands on the control the step names, the panel never covers it, wrong clicks do
not advance, every step is verified (or knowingly assumed for focus-only clicks), the step card shows
its whole instruction, and the page ends in the tutorial's final state.

    .venv/bin/python tools/shadow_e2e.py [--plan-dir DIR] [--video out.mp4]
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "artifacts" / "tmp" / "shadow_e2e"
TMP.mkdir(parents=True, exist_ok=True)
SCREEN = TMP / "screen.json"
SCREEN.write_text(json.dumps({"screens": [{"name": "mac", "x": 0, "y": 0, "width": 1440, "height": 900,
                                           "logicalDpi": 72, "logicalBaseDpi": 72, "dpr": 2}]}))
os.environ["QT_QPA_PLATFORM"] = f"offscreen:configfile={SCREEN}"
os.environ["NIMBUS_HOME"] = str(TMP / "home")
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402
from playwright.async_api import Page, async_playwright  # noqa: E402
from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer  # noqa: E402
from PyQt6.QtGui import QColor, QCursor, QFont, QImage, QPainter, QPen, QPolygonF  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from nimbus.mac.screen import Capture, Display  # noqa: E402
from nimbus.runtime import ui, ui_call  # noqa: E402
from nimbus.ui.panel import _body  # noqa: E402

VIDEO = ROOT / "artifacts" / "fixtures" / "tutorial_chat.mp4"
FIXTURE = ROOT / "tests" / "fixtures" / "ui" / "chat_app.html"
DISPLAY = Display(0, 1, 0.0, 0.0, 1440.0, 900.0, 2.0, True)
W, H = 1440, 900


class Recorder:
    """Composites what a person would see (page, overlay, panel, their pointer) into video frames."""

    def __init__(self, app, out_dir: Path) -> None:
        self.app, self.dir = app, out_dir
        shutil.rmtree(out_dir, ignore_errors=True)
        out_dir.mkdir(parents=True)
        self.page_jpeg: bytes | None = None
        self.pointer = QPointF(720, 450)
        self.banner = ""
        self.n = 0
        self.last = 0.0
        self.timer = QTimer()
        self.timer.timeout.connect(self.frame)
        self.timer.start(250)

    def frame(self) -> None:
        if self.page_jpeg is None:
            return
        busy = self.app.shadow is None  # while the video is analysed, a frame every 2 s is plenty
        now = time.monotonic()
        if busy and now - self.last < 2.0:
            return
        self.last = now
        img = QImage(W, H, QImage.Format.Format_RGB32)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.drawImage(QRectF(0, 0, W, H), QImage.fromData(self.page_jpeg))
        self.app.overlay.paint(p, QRectF(0, 0, W, H))
        for w in (self.app.panel, self.app.panel.tab):
            if w.isVisible():
                p.setOpacity(w.windowOpacity())
                p.drawPixmap(w.geometry().topLeft(), w.grab())
                p.setOpacity(1.0)
        _arrow(p, self.pointer)
        if self.banner:
            p.setFont(QFont("Helvetica Neue", 15, QFont.Weight.DemiBold))
            r = QRectF(16, H - 46, p.fontMetrics().horizontalAdvance(self.banner) + 28, 32)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(20, 20, 24, 215))
            p.drawRoundedRect(r, 9, 9)
            p.setPen(QColor(235, 235, 240))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, self.banner)
        p.end()
        img.save(str(self.dir / f"f{self.n:05d}.jpg"), "JPEG", 82)
        self.n += 1

    def encode(self, out: Path) -> None:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", "6", "-i", str(self.dir / "f%05d.jpg"),
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23", str(out)], check=True)


def _arrow(p: QPainter, at: QPointF) -> None:
    pts = [(0, 0), (0, 17), (4.5, 13), (7.5, 20), (10, 19), (7, 12), (12.5, 12)]
    poly = QPolygonF([QPointF(at.x() + x, at.y() + y) for x, y in pts])
    p.setBrush(QColor(255, 255, 255))
    p.setPen(QPen(QColor(0, 0, 0), 1.3))
    p.drawPolygon(poly)


async def element_at(page: Page, x: float, y: float) -> dict:
    return await page.evaluate("""([x, y]) => {
        let el = document.elementFromPoint(x, y);
        const pick = el && el.closest('button, input, textarea, [data-model], a, li, .item');
        el = pick || el;
        return el ? {id: el.id || '', text: (el.innerText || el.value || '').trim().slice(0, 60),
                     label: el.getAttribute('aria-label') || '', placeholder: el.getAttribute('placeholder') || '',
                     model: el.getAttribute('data-model') || ''} : {};
    }""", [x, y])


def names_target(step, el: dict) -> bool:
    """The element under the pointer is the control the step names (by its label, text or role)."""
    hay = " ".join(str(v) for v in el.values()).lower()
    target = step.target.lower().replace("...", "").replace("…", "").strip()
    if target and (target in hay or hay and any(w in hay for w in target.split() if len(w) > 3)):
        return True
    return ("send" in target and el.get("id") == "send") or (step.action == "type" and el.get("id") == "prompt")


async def person(app, rec: Recorder, plan_dir: Path | None) -> dict:
    report: dict = {"steps": [], "problems": []}
    pw = await async_playwright().start()
    browser = await pw.chromium.launch()
    page = await browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=2)
    await page.goto(FIXTURE.as_uri() + "?theme=dark")

    async def capture() -> Capture:
        png = await page.screenshot()
        w, h = Image.open(io.BytesIO(png)).size
        return Capture(DISPLAY, png, w, h, time.time())

    app.capture = capture
    _sandbox_agent(app, page)

    async def film() -> None:
        while True:
            try:
                rec.page_jpeg = await page.screenshot(type="jpeg", quality=75, scale="css")
            except Exception:
                return
            await asyncio.sleep(0.2)

    filming = asyncio.ensure_future(film())

    async def move(x: float, y: float) -> None:  # glide the person's pointer (the buddy follows it)
        x0, y0 = rec.pointer.x(), rec.pointer.y()
        for i in range(1, 13):
            t = i / 12
            px, py = x0 + (x - x0) * t, y0 + (y - y0) * t
            rec.pointer = QPointF(px, py)
            ui(QCursor.setPos, QPoint(int(px), int(py)))
            await page.mouse.move(px, py)
            await asyncio.sleep(0.03)

    async def click(x: float, y: float) -> None:
        await move(x, y)
        await page.mouse.click(x, y)
        app.shadow.on_click(x, y)

    async def press(label: str) -> None:
        from nimbus.ui.widgets import NButton

        if not await ui_call(lambda: app.panel.is_open):  # tucked away: the person clicks the edge tab
            rec.banner = f"Opening the panel from the edge tab to press {label}"
            ui(app.panel.tab.clicked.emit)
            await asyncio.sleep(0.5)

        def go() -> bool:
            for b in app.panel.findChildren(NButton):
                if b.text().strip(" →←").strip() == label and b.isVisible():
                    b.click()
                    return True
            return False
        if not await ui_call(go):
            report["problems"].append(f"panel button {label!r} not visible")

    async def until(cond, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            await asyncio.sleep(0.1)
        return False

    async def settled(index: int, fails: int, timeout: float = 75) -> bool:
        """The check the person's action triggered has finished: advanced, or a failed check counted."""
        s = app.shadow
        return await until(lambda: s.index != index or not s.active or s.failed_checks > fails, timeout)

    rec.banner = "Nimbus watches the tutorial video"
    if plan_dir:
        await app.resume_tutorial(plan_dir)
    else:
        await app.open_tutorial(VIDEO)
    s = app.shadow
    report["plan"] = [f"{st.action}: {st.instruction}" for st in s.plan.steps]
    wrong_done = show_done = False
    seen: set[int] = set()
    while s.active:
        i = s.index
        if i in seen:  # Skip did not move on either: report it instead of trying the step forever
            report["problems"].append(f"step {i + 1}: stuck (Skip did not advance)")
            break
        seen.add(i)
        step = s.step
        if not await until(lambda: s.waiting and not s._lock.locked(), 90):
            report["problems"].append(f"step {i + 1}: never became ready")
            break
        rec.banner = f"Step {i + 1}/{len(s.plan.steps)}: {step.instruction}"
        await asyncio.sleep(0.6)
        marks = await ui_call(lambda: [(m.x, m.y, m.box) for m in app.overlay.marks if m.step == i + 1])
        card = await ui_call(lambda: (app.panel.step_card.instruction.text(),
                                      app.panel.step_card.instruction.height(),
                                      app.panel.step_card.instruction.heightForWidth(
                                          app.panel.step_card.instruction.width())))
        if card[0] != step.instruction:
            report["problems"].append(f"step {i + 1}: card shows {card[0]!r}")
        if card[1] + 1 < card[2]:
            report["problems"].append(f"step {i + 1}: instruction clipped ({card[1]}px of {card[2]}px)")
        entry = {"step": i + 1, "action": step.action, "instruction": step.instruction, "target": step.target}
        if step.needs_target and not marks:
            report["problems"].append(f"step {i + 1}: nothing pointed at")
        x, y = (marks[-1][0], marks[-1][1]) if marks else (720, 450)  # where the buddy's tip points
        el = await element_at(page, x, y)
        entry["pointed_at"] = el
        entry["pointer_ok"] = (not step.needs_target) or names_target(step, el)
        covered = await ui_call(lambda: app.panel.isVisible() and _body(app.panel.geometry()).contains(int(x), int(y)))
        if covered:
            report["problems"].append(f"step {i + 1}: the panel covers the control")

        if i == 1 and not wrong_done:  # a deliberate wrong click must not advance
            wrong_done = True
            box = await page.get_by_text("Dinner ideas for six").bounding_box()
            rec.banner = "Wrong click on purpose: 'Dinner ideas for six'"
            fails = s.failed_checks
            await click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            await settled(i, fails)
            verdict = await ui_call(app.panel.shadow_verdict.text)
            entry["wrong_click"] = {"advanced": s.index != i, "verdict": verdict}
            if s.index != i:
                report["problems"].append("a wrong click advanced the guide")
                report["steps"].append(entry)
                continue

        if i == 2 and not show_done:  # Show me re-points without losing the step
            show_done = True
            rec.banner = "Pressing Show me"
            await press("Show me")
            await asyncio.sleep(0.5)
            await until(lambda: s.waiting and not s._lock.locked(), 60)
            entry["show_me_kept_step"] = s.index == i
            if s.index != i:
                report["problems"].append("Show me changed the step")

        last = i == len(s.plan.steps) - 1
        for attempt in range(3):
            fails = s.failed_checks
            if last and step.action == "click":
                rec.banner = "Pressing Do it for me (agent drives the hidden page)"
                await press("Do it for me")
            elif step.action == "type":
                if not await page.evaluate("() => ['INPUT', 'TEXTAREA'].includes(document.activeElement?.tagName)"):
                    await click(x, y)  # a person clicks into the field before typing
                for ch in step.text:
                    await page.keyboard.type(ch)
                    s.on_key(False)
                    await asyncio.sleep(0.06)
            else:
                await click(x, y)
            await settled(i, fails, timeout=120 if last else 75)
            if s.index != i or not s.active:
                break
            entry.setdefault("retries", []).append(await ui_call(app.panel.shadow_verdict.text))
            marks = await ui_call(lambda: [(m.x, m.y) for m in app.overlay.marks if m.step == i + 1])
            if marks:
                x, y = marks[-1]
        entry["advanced"] = s.index != i or not s.active
        entry["verified"] = i in s.completed
        entry["assumed"] = i in s.assumed
        if not entry["advanced"]:
            report["problems"].append(f"step {i + 1}: did not advance ({entry.get('retries')})")
            await press("Skip")
            await asyncio.sleep(1.0)
        report["steps"].append(entry)
    rec.banner = "Tutorial finished"
    await asyncio.sleep(2.5)
    report["final_page"] = await page.evaluate("""() => ({
        bubbles: [...document.querySelectorAll('.msg.user')].map(e => e.innerText),
        model: (document.querySelector('#model-picker')?.innerText || '').trim()})""")
    filming.cancel()
    await browser.close()
    await pw.stop()
    return report


def _sandbox_agent(app, page: Page) -> None:
    """"Do it for me" must drive the hidden page, never the real Mac."""
    import nimbus.app as appmod
    from nimbus.mac.control import AppContext
    from nimbus.repro.reproduce import PlaywrightDriver

    class PageDriver(PlaywrightDriver):
        def __init__(self, _capturer, display=None, before_action=None, target_pid=-1):
            super().__init__(page)
            self.before_action = before_action

        async def execute(self, action):
            if self.before_action and "x" in action:
                await self.before_action(*self._xy(action), action)  # screenshot pixels -> page points
            await super().execute(action)

    appmod.control.MacDriver = PageDriver
    appmod.control.accessibility_trusted = lambda prompt=False: True
    appmod.control.give_focus_back = lambda pid, timeout=0.8: None
    appmod.control.frontmost_app = lambda: AppContext("Chromium", "", "Chatbook", -1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan-dir", type=Path)
    ap.add_argument("--video", type=Path, default=ROOT / "artifacts" / "evals" / "shadow_e2e.mp4")
    args = ap.parse_args()
    from nimbus.app import NimbusApp

    qt = QApplication(sys.argv)
    app = NimbusApp(qt)
    app.settings.speak_answers = False
    app.overlay.show()
    app.panel.place_tab()
    rec = Recorder(app, TMP / "frames")
    holder: dict = {}

    def kick() -> None:
        holder["future"] = app.runner.submit(person(app, rec, args.plan_dir))

        def poll() -> None:
            f = holder["future"]
            if not f.done():
                QTimer.singleShot(200, poll)
                return
            holder["report"] = f.result() if f.exception() is None else {"problems": [repr(f.exception())]}
            rec.frame()
            app.runner.stop()
            qt.quit()
        QTimer.singleShot(200, poll)

    QTimer.singleShot(300, kick)
    qt.exec()
    report = holder.get("report", {"problems": ["no report"]})
    args.video.parent.mkdir(parents=True, exist_ok=True)
    rec.encode(args.video)
    report["video"] = str(args.video)
    (TMP / "report.json").write_text(json.dumps(report, indent=2))
    for e in report.get("steps", []):
        flag = "PASS" if e.get("pointer_ok") and e.get("advanced") else "FAIL"
        how = "verified" if e.get("verified") else ("assumed (focus only)" if e.get("assumed") else "not verified")
        print(f"{flag} step {e['step']}: {e['instruction']}  | pointed at {e.get('pointed_at')}  | {how}"
              + (f"  | wrong click: {e['wrong_click']}" if "wrong_click" in e else "")
              + (f"  | retries: {e['retries']}" if e.get("retries") else ""))
    print("final page:", report.get("final_page"))
    for p in report.get("problems", []):
        print("PROBLEM:", p)
    print("video:", args.video)
    ok = not report.get("problems") and all(e.get("pointer_ok") and e.get("advanced") for e in report.get("steps", []))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

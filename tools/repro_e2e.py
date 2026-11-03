"""Repro end to end on the real app, invisibly, with a recording of the run.

The real controller (`NimbusApp`) and panel run on an offscreen Qt screen the size of a 1440x900
Retina Mac. A simulated person opens the panel, switches to Repro, imports the narrated bug recording
(`artifacts/fixtures/bug_cart_total.mp4`) with Import..., watches the pipeline and opens the report. The pipeline
runs for real: Meta models plan from the video, Chromium reproduces the bug headless, Muse Code fixes
it, the same steps are run again, and the report is written. It works on a throwaway seeded copy of
`demo/shopapp` served on a free port, so the demo repo is never touched.

The recording shows the panel beside what Nimbus is working on at each stage: the bug recording
while it is analysed, the live Chromium screenshots while it reproduces and verifies, Muse Code's
log while it fixes, both recorded reproductions at real speed, and the report.

Checks: the bug is reproduced with evidence before anything is fixed; the fix changes cart.js, its
regression test fails on the old code and the shop's tests pass on the fix branch; the same steps
afterwards show the expected total; the stages run in order; the report renders and states the
outcome; the panel ends with every stage done and the report button on; the demo repo is unchanged.

    .venv/bin/python tools/repro_e2e.py [--video out.mp4]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "artifacts" / "tmp" / "repro_e2e"
TMP.mkdir(parents=True, exist_ok=True)
SCREEN = TMP / "screen.json"
SCREEN.write_text(json.dumps({"screens": [{"name": "mac", "x": 0, "y": 0, "width": 1440, "height": 900,
                                           "logicalDpi": 72, "logicalBaseDpi": 72, "dpr": 2}]}))
os.environ["QT_QPA_PLATFORM"] = f"offscreen:configfile={SCREEN}"
os.environ["NIMBUS_HOME"] = str(TMP / "home")
sys.path.insert(0, str(ROOT))

from playwright.async_api import async_playwright  # noqa: E402
from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer  # noqa: E402
from PyQt6.QtGui import QColor, QCursor, QFont, QImage, QPainter, QPen, QPolygonF  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from nimbus.runtime import ui, ui_call  # noqa: E402
from tools.demo_repo import digest, seed  # noqa: E402

BUG_VIDEO = ROOT / "artifacts" / "fixtures" / "bug_cart_total.mp4"
TRUTH = json.loads(BUG_VIDEO.with_suffix(".json").read_text()) if BUG_VIDEO.exists() else {}
DEMO = ROOT / "demo" / "shopapp"
W, H = 1440, 900
TICK_S = 0.25  # one video frame per tick at 4 fps is real time
WINDOW = QRectF(24, 24, 968, 852)  # the "screen" Nimbus is working on, left of the docked panel
STAGES = ["analyze", "launch", "reproduce", "fix", "verify", "report"]


class Recorder:
    """Composites the panel and what Nimbus is working on into video frames, fast-forwarding the waits."""

    def __init__(self, app, out_dir: Path) -> None:
        self.app, self.dir = app, out_dir
        shutil.rmtree(out_dir, ignore_errors=True)
        out_dir.mkdir(parents=True)
        self.screen: QImage | None = None
        self.title = ""
        self.terminal: list[str] | None = None  # shown instead of `screen` while Muse Code works
        self.banner = ""
        self.pointer: QPointF | None = None
        self.speed = 1  # 1 = real time; n = one frame every n ticks
        self.clip: list[tuple[QImage, str, str]] = []  # frames to play at real speed before anything else
        self.n = self.ticks = 0
        self.timer = QTimer()
        self.timer.timeout.connect(self.tick)
        self.timer.start(int(TICK_S * 1000))

    def tick(self) -> None:
        self.ticks += 1
        if self.clip:
            img, title, banner = self.clip.pop(0)
            self.frame(img, title, banner, speed=1)
        elif self.ticks % self.speed == 0:
            self.frame(self.screen, self.title, self.banner, speed=self.speed)

    def frame(self, screen: QImage | None, title: str, banner: str, speed: int) -> None:
        img = QImage(W, H, QImage.Format.Format_RGB32)
        img.fill(QColor(24, 22, 26))
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self._window(p, screen, title)
        for w in (self.app.panel, self.app.panel.tab):
            if w.isVisible():
                p.setOpacity(w.windowOpacity())
                p.drawPixmap(w.geometry().topLeft(), w.grab())
                p.setOpacity(1.0)
        if self.pointer is not None:
            _arrow(p, self.pointer)
        if banner:
            _pill(p, banner, QPointF(WINDOW.left() + 14, WINDOW.bottom() - 46))
        if speed > 1:
            _pill(p, f"{speed}x", QPointF(WINDOW.right() - 62, WINDOW.top() + 46), dim=True)
        p.end()
        img.save(str(self.dir / f"f{self.n:05d}.jpg"), "JPEG", 85)
        self.n += 1

    def _window(self, p: QPainter, screen: QImage | None, title: str) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(38, 36, 41))
        p.drawRoundedRect(WINDOW, 12, 12)
        for i, c in enumerate(((237, 106, 94), (245, 191, 79), (98, 197, 84))):
            p.setBrush(QColor(*c))
            p.drawEllipse(QPointF(WINDOW.left() + 20 + 18 * i, WINDOW.top() + 17), 5.5, 5.5)
        p.setPen(QColor(200, 198, 204))
        p.setFont(QFont("Helvetica Neue", 12))
        p.drawText(QRectF(WINDOW.left() + 80, WINDOW.top(), WINDOW.width() - 160, 34), Qt.AlignmentFlag.AlignCenter,
                   title)
        body = QRectF(WINDOW.left() + 1, WINDOW.top() + 34, WINDOW.width() - 2, WINDOW.height() - 35)
        if self.terminal is not None and screen is None:
            p.fillRect(body, QColor(14, 13, 16))
            p.setPen(QColor(208, 214, 206))
            p.setFont(QFont("Menlo", 11))
            for i, line in enumerate(self.terminal[-44:]):
                p.drawText(QPointF(body.left() + 16, body.top() + 24 + 18 * i), line[:120])
            return
        p.fillRect(body, QColor(250, 250, 248))
        if screen is not None and not screen.isNull():
            scaled = screen.size().scaled(body.size().toSize(), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRectF(body.left() + (body.width() - scaled.width()) / 2, body.top(), scaled.width(),
                            scaled.height())
            p.drawImage(target, screen)

    def encode(self, out: Path) -> None:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(int(1 / TICK_S)),
                        "-i", str(self.dir / "f%05d.jpg"), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "22",
                        str(out)], check=True)


def _arrow(p: QPainter, at: QPointF) -> None:
    pts = [(0, 0), (0, 17), (4.5, 13), (7.5, 20), (10, 19), (7, 12), (12.5, 12)]
    p.setBrush(QColor(255, 255, 255))
    p.setPen(QPen(QColor(0, 0, 0), 1.3))
    p.drawPolygon(QPolygonF([QPointF(at.x() + x, at.y() + y) for x, y in pts]))


def _pill(p: QPainter, text: str, at: QPointF, dim: bool = False) -> None:
    p.setFont(QFont("Helvetica Neue", 15, QFont.Weight.DemiBold))
    r = QRectF(at.x(), at.y(), p.fontMetrics().horizontalAdvance(text) + 28, 32)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(20, 20, 24, 150 if dim else 220))
    p.drawRoundedRect(r, 9, 9)
    p.setPen(QColor(235, 235, 240))
    p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def shop_clone(dest: Path) -> Path:
    """The demo shop (seeded bugs and all) in a throwaway repo with no remote."""
    return seed(DEMO, dest, author="Nimbus E2E")


def video_frames(video: Path, out: Path, fps: float) -> list[Path]:
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(video), "-vf", f"fps={fps}", str(out / "%04d.jpg")],
                   check=True)
    return sorted(out.glob("*.jpg"))


def newest(paths) -> Path | None:
    files = [p for p in paths if p.is_file()]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


async def person(app, rec: Recorder, holder: dict) -> dict:
    report: dict = {"checks": {}, "problems": [], "stages": {}}
    check = report["checks"]
    panel = app.panel

    async def point_and_click(button, label: str) -> None:
        """Glide the person's pointer to a panel button and press it."""
        center = await ui_call(lambda: button.mapToGlobal(button.rect().center()))
        start = rec.pointer or QPointF(560, 520)
        for i in range(1, 13):
            t = i / 12
            rec.pointer = QPointF(start.x() + (center.x() - start.x()) * t, start.y() + (center.y() - start.y()) * t)
            ui(QCursor.setPos, QPoint(int(rec.pointer.x()), int(rec.pointer.y())))
            await asyncio.sleep(0.03)
        rec.banner = label
        await asyncio.sleep(0.4)
        ui(button.clicked.emit if button is panel.tab else button.click)
        await asyncio.sleep(0.6)

    bug_frames = await asyncio.to_thread(video_frames, BUG_VIDEO, TMP / "bug_frames", 2)
    rec.screen = QImage(str(bug_frames[0]))
    rec.title = "Fieldnote Supply - the shop with the bug"
    rec.banner = "A developer has a narrated recording of a cart bug"
    await asyncio.sleep(2.0)

    await point_and_click(panel.tab, "Open Nimbus from the edge tab")
    rec.banner = "Switch to Repro"
    ui(panel.show_page, 2)  # what clicking the Repro tab does
    await asyncio.sleep(1.2)

    # Import... opens a native file dialog; picking the recording emits repro_video with its path.
    import_btn = await ui_call(lambda: next(b for b in panel.findChildren(type(panel.report_btn))
                                            if b.text().startswith("Import")))
    center = await ui_call(lambda: import_btn.mapToGlobal(import_btn.rect().center()))
    rec.pointer = QPointF(center.x(), center.y())
    rec.banner = "Import the bug recording (bug_cart_total.mp4)"
    await asyncio.sleep(1.0)
    started = time.monotonic()
    ui(panel.repro_video.emit, str(BUG_VIDEO))
    rec.pointer = None

    run_dirs = TMP / "home" / "repro"
    shown_clips: set[str] = set()
    last_stage = ""
    while "result" not in holder:
        await asyncio.sleep(0.5)
        if time.monotonic() - started > 1500:
            report["problems"].append("the pipeline did not finish within 25 minutes")
            return report
        states = await ui_call(lambda: dict(panel.timeline.state))
        stage = next((k for k in STAGES if states.get(k) == "active"), "")
        if stage and stage != last_stage:
            report["stages"][stage] = round(time.monotonic() - started, 1)
            last_stage = stage
        dirs = [d for d in run_dirs.glob("*") if d.is_dir()] if run_dirs.exists() else []
        run_dir = max(dirs, key=lambda d: d.stat().st_mtime) if dirs else None
        if run_dir is None:
            continue
        rec.speed = 8 if stage in ("analyze", "fix", "launch") else 4
        if not stage and any(v != "pending" for v in states.values()):
            continue  # between stages, or finished: keep showing the last thing Nimbus worked on
        if stage in ("", "analyze"):
            i = int((time.monotonic() - started) * 2) % len(bug_frames)
            rec.screen, rec.terminal = QImage(str(bug_frames[i])), None
            rec.title = "Your recording - bug_cart_total.mp4"
            rec.banner = "Muse Spark watches the recording and listens to the narration"
            continue
        for phase, title, banner in (("before", "Chromium (headless) - Nimbus reproducing the bug",
                                      "Recorded reproduction: the bug, before any fix"),
                                     ("after", "Chromium (headless) - the same steps after the fix",
                                      "Recorded reproduction: the same steps after Muse Code's fix")):
            webm = run_dir / phase / "reproduction.webm"
            if webm.exists() and phase not in shown_clips:
                shown_clips.add(phase)
                frames = await asyncio.to_thread(video_frames, webm, TMP / f"clip_{phase}", 4)
                rec.clip.extend((QImage(str(f)), title, banner) for f in frames)
        if stage == "fix":
            muse_log = run_dir / "fix" / "muse-code.log"
            log = muse_log if muse_log.exists() else newest((run_dir / "fix").glob("*.txt"))
            text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", log.read_text(errors="replace")) if log else ""
            rec.screen, rec.terminal = None, (text.splitlines() or ["Muse Code is starting..."])
            rec.title = "Muse Code - fixing the bug on a new branch"
            rec.banner = "Muse Code fixes the root cause and adds a regression test"
            continue
        phase = "after" if stage in ("verify", "report") else "before"
        shot = newest(list((run_dir / phase / "steps").glob("*.png")) + list((run_dir / phase).glob("final.png")))
        rec.terminal = None
        if shot is not None:
            rec.screen = QImage(str(shot))
        rec.title = f"Chromium (headless) - {app.settings.repro.app_url}"
        rec.banner = {"launch": "Nimbus starts the app", "reproduce": "Nimbus replays the steps in Chromium",
                      "verify": "Nimbus runs the same steps on the fixed code",
                      "report": "Nimbus writes the report"}.get(stage, "")

    report["stages"]["total"] = round(time.monotonic() - started, 1)
    while rec.clip:  # let both reproductions play before the ending
        await asyncio.sleep(0.25)
    result, events = holder["result"], holder["events"]
    rec.speed = 1
    rec.banner = result.message
    await asyncio.sleep(2.5)

    # Panel end state, as the person sees it.
    states = await ui_call(lambda: dict(panel.timeline.state))
    check["panel: every stage done"] = all(states.get(k) == "done" for k in STAGES)
    msg = await ui_call(lambda: (panel.repro_message.text(), panel.repro_message.height(),
                                 panel.repro_message.heightForWidth(panel.repro_message.width())))
    check["panel: outcome message shown in full"] = bool(msg[0]) and msg[1] + 1 >= msg[2]
    check["panel: Open report enabled"] = await ui_call(panel.report_btn.isEnabled)
    details = await ui_call(lambda: dict(panel.timeline.detail))
    report["stage_details"] = details
    check["panel: each stage keeps its own detail"] = len(details) == len(STAGES) and \
        len(set(details.values())) == len(STAGES)

    # Open report: the person presses the button; the report opens in the (hidden) browser.
    holder["opened"] = None
    await point_and_click(panel.report_btn, "Open report")
    check["report: Open report opens it"] = holder["opened"] == str(app._last_report)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1180, "height": 1000})
        await page.goto(Path(result.report_html).as_uri())
        text = await page.inner_text("body")
        full = await page.screenshot(full_page=True)
        await browser.close()
    report["report_text"] = text[:3000]
    check["report: states the outcome and the right total"] = "158" in text and "cart.js" in text
    shot = QImage.fromData(full)
    rec.title = f"Nimbus report - {Path(result.report_html).name}"
    rec.terminal = None
    rec.banner = "The report: reproduction, root cause, the diff, tests and proof"
    view_h = int(shot.width() * (WINDOW.height() - 35) / (WINDOW.width() - 2))
    steps = max(1, (shot.height() - view_h) // 40)
    for i in range(min(steps, 160) + 1):
        top = min(shot.height() - view_h, i * 40) if shot.height() > view_h else 0
        rec.screen = shot.copy(0, top, shot.width(), min(view_h, shot.height()))
        await asyncio.sleep(0.25 if i else 2.0)
    await asyncio.sleep(2.0)

    # The outcome itself, not just the status.
    plan, fix = result.plan, result.fix
    check["status fixed"] = result.status == "fixed"
    check["plan: a bug plan with the two Add clicks"] = bool(plan and plan.kind == "bug" and
                                                            sum(1 for s in plan.steps if s.action == "click") >= 2)
    last_click = max((c["t"] for c in TRUTH.get("clicks", [])), default=0)
    check["plan: bug placed after the click that causes it"] = bool(
        plan and plan.bug_time is not None and last_click - 1.0 <= plan.bug_time <= TRUTH.get("duration_s", 1e9))
    check["before: bug reproduced with evidence"] = bool(result.before and result.before.reproduced == "yes"
                                                         and result.before.evidence)
    check["fix: changes cart.js"] = bool(fix and "cart.js" in fix.changed_files)
    check["fix: regression test fails on the old code"] = bool(fix and fix.regression_test_fails_before_fix is True)
    check["after: expected behaviour visible, bug gone"] = bool(result.after and result.after.expected_visible is True
                                                               and result.after.reproduced == "no")
    check["tests passed"] = result.tests_passed is True
    check["stages ran in order"] = list(dict.fromkeys(s for s, _, _ in events)) == STAGES
    repo = Path(app.settings.repro.repo_path)
    branch = git(repo, "branch", "--show-current")
    check["clone: left on the fix branch"] = branch.startswith("nimbus/repro-")
    node = subprocess.run(["node", "--test"], cwd=repo, capture_output=True, text=True)
    check["clone: node --test passes on the fix branch"] = node.returncode == 0
    check["clone: main still has the seeded bug"] = git(repo, "show", "main:cart.js") == (DEMO / "cart.js").read_text().strip()
    report.update(status=result.status, message=result.message, branch=branch, engine=getattr(fix, "engine", ""),
                  changed=getattr(fix, "changed_files", []), report_html=result.report_html,
                  before=getattr(result.before, "evidence", ""), after=getattr(result.after, "evidence", ""))
    rec.banner = "Fixed, verified and reported. Nothing was merged."
    await asyncio.sleep(3.0)
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", type=Path, default=ROOT / "artifacts" / "evals" / "repro_e2e.mp4")
    args = ap.parse_args()
    if not BUG_VIDEO.exists():
        print(f"no bug recording at {BUG_VIDEO}: record a narrated bug video of the shop there")
        return 2
    demo_before = digest(DEMO)
    shutil.rmtree(TMP / "home", ignore_errors=True)
    shop = shop_clone(TMP / "shop")

    from nimbus.app import NimbusApp
    from nimbus.repro import pipeline as pipeline_mod

    holder: dict = {}
    # Open report hands the file to the Mac's default app; here it must stay in the hidden browser.
    # Patched on the class before the app exists, because the button binds the method at startup.
    NimbusApp._open_report = lambda self: holder.__setitem__("opened", str(self._last_report))
    qt = QApplication(sys.argv)
    app = NimbusApp(qt)
    app.settings.speak_answers = False
    port = free_port()
    app.settings.repro.repo_path = str(shop)
    app.settings.repro.app_url = f"http://127.0.0.1:{port}"
    app.settings.repro.start_command = f"{sys.executable} -m http.server {port} --bind 127.0.0.1"
    app.settings.repro.open_pull_request = False
    app.panel.repo_label.setText("~/shop-e2e  (clone of demo/shopapp)")
    app.panel.url_label.setText(app.settings.repro.app_url)

    real_run = pipeline_mod.ReproPipeline.run

    async def observed_run(self, video, progress=None, should_stop=None):
        events: list[tuple[str, str, float]] = []

        def tap(stage, message, fraction):
            events.append((stage, message, fraction))
            if progress:
                progress(stage, message, fraction)
        result = await real_run(self, video, progress=tap, should_stop=should_stop)
        holder["events"] = events
        holder["result"] = result
        return result

    pipeline_mod.ReproPipeline.run = observed_run
    app.overlay.show()
    app.panel.place_tab()
    rec = Recorder(app, TMP / "frames")

    def kick() -> None:
        holder["future"] = app.runner.submit(person(app, rec, holder))

        def poll() -> None:
            f = holder["future"]
            if not f.done():
                QTimer.singleShot(250, poll)
                return
            holder["report"] = f.result() if f.exception() is None else {"problems": [repr(f.exception())],
                                                                          "checks": {}}
            app.runner.stop()
            qt.quit()
        QTimer.singleShot(250, poll)

    QTimer.singleShot(300, kick)
    qt.exec()
    report = holder.get("report", {"problems": ["no report"], "checks": {}})
    report["checks"]["demo/shopapp untouched"] = digest(DEMO) == demo_before
    args.video.parent.mkdir(parents=True, exist_ok=True)
    rec.encode(args.video)
    report["video"] = str(args.video)
    (TMP / "report.json").write_text(json.dumps(report, indent=2, default=str))
    for name, ok in report.get("checks", {}).items():
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    print("stage start times (s):", report.get("stages"))
    print(f"{report.get('status')}: {report.get('message')}  [{report.get('engine')}, {report.get('branch')}]")
    for p in report.get("problems", []):
        print("PROBLEM:", p)
    print("video:", args.video)
    ok = not report.get("problems") and all(report.get("checks", {}).values())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

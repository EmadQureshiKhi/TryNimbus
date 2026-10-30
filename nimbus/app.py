"""The controller: wires the edge panel, overlay, hotkey, microphone and the three modes together.

Threads: Qt owns the main thread (panel, overlay, animations). Model calls and other slow work
run on the asyncio loop in `AsyncRunner`. pynput delivers keys and clicks on its own threads;
those handlers only hop onto the loop or the UI thread and return.
"""
from __future__ import annotations

import asyncio
import html
import logging
import subprocess
import time
from pathlib import Path

from PyQt6.QtCore import QRect, QRectF
from PyQt6.QtWidgets import QApplication

from nimbus import config
from nimbus.ask import AskService, Walkthrough
from nimbus.engine.steps import PlanStep, StepPlan
from nimbus.engine.video import VideoAnalyzer
from nimbus.mac import control, screen
from nimbus.mac import windows as macwin
from nimbus.mac.io import ClickListener, HoldHotkey, Microphone, Speaker, reading_time
from nimbus.meta.client import MetaClient
from nimbus.meta.computer import AgentResult, run_agent
from nimbus.meta.resilience import MetaAPIError
from nimbus.meta.voice import PushToTalkSession
from nimbus.runtime import AsyncRunner, install_bridge, ui, ui_call, ui_wait
from nimbus.shadow.session import ShadowSession
from nimbus.ui import theme
from nimbus.ui.overlay import Mark, Overlay
from nimbus.ui.panel import Panel, _body

log = logging.getLogger(__name__)

CLOSING_CAPTION_S = 3.0  # how long a result stays on screen after it is said
PERMISSION_URLS = {
    "screen": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "microphone": "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
}


def microphone_authorized() -> bool:
    try:
        import AVFoundation

        status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(AVFoundation.AVMediaTypeAudio)
        return int(status) == 3  # AVAuthorizationStatusAuthorized
    except Exception:
        return True


class NimbusApp:
    def __init__(self, qt: QApplication) -> None:
        self.qt = qt
        install_bridge()
        macwin.become_accessory_app()
        self.store = config.SettingsStore()
        self.settings = self.store.settings
        self.client = MetaClient(self.store)
        self.runner = AsyncRunner()
        self.runner.start()
        self.overlay = Overlay()
        self.panel = Panel(self.settings)
        self.capturer = screen.ScreenCapturer(hide_own_windows=self._hide_for_capture)
        self.ask = AskService(self.client)
        self.speaker = Speaker(self.settings.voice_name)
        self.mic = Microphone()
        self.hotkey = HoldHotkey(self.settings.hotkey, self._ptt_down, self._ptt_up, self._escape, self._on_key)
        self.clicks = ClickListener(self._on_click)
        self._ptt: PushToTalkSession | None = None
        self._walks: list[Walkthrough] = []  # answers that can be shown again
        self._busy: asyncio.Future | None = None
        self._stop_agent = False
        self._confirm_future: asyncio.Future | None = None
        self.shadow: ShadowSession | None = None
        self._repro_run = None
        self._repro_recorder = None
        self._last_report: Path | None = None
        self.overlay.exclusion = self._is_own_ui
        self.overlay.own_windows = lambda: [QRectF(_body(self.panel.geometry()))] if self.panel.isVisible() else []
        self._connect()

    # ------------------------------------------------------------ startup

    def start(self) -> None:
        self.overlay.show()
        self.overlay.set_buddy_visible(self.settings.buddy_enabled)
        self.overlay.dismissed.connect(self._escape)  # the × beside an answer does what Esc does
        self.panel.place_tab()
        self._refresh_status()
        self._refresh_recent_guides()
        try:
            self.hotkey.start()
            self.clicks.start()
        except Exception as exc:
            log.warning("global input listeners unavailable: %s", exc)
            self.overlay.toast("Global hotkey unavailable. Allow Input Monitoring for this app, or type in the panel.",
                               "error", 8)
        if not self.client.has_key():
            self.panel.open()
            self.panel.show_page(3)
            self.overlay.toast("Add your Meta Model API key in Settings to wake Nimbus up.", "info", 8)
        else:
            self.overlay.toast(f"Nimbus is ready. Hold {_pretty(self.settings.hotkey)} and ask anything.", "success", 5)

    def _connect(self) -> None:
        p = self.panel
        p.ask_submitted.connect(lambda text, act: self.submit(self.handle_question(text, act=act)))
        p.shadow_open.connect(lambda path: self.submit(self.open_tutorial(Path(path))))
        p.shadow_resume.connect(lambda d: self.submit(self.resume_tutorial(Path(d))))
        p.shadow_action.connect(lambda a: self.submit(self.shadow_action(a)))
        p.repro_record.connect(lambda on: self.submit(self.repro_record(on)))
        p.repro_video.connect(lambda path: self.submit(self.run_repro(Path(path))))
        p.repro_cancel.connect(self._cancel_repro)
        p.repro_open_report.connect(self._open_report)
        p.setting_changed.connect(self._setting_changed)
        p.api_key_entered.connect(self._save_key)
        p.open_permissions.connect(lambda perm: subprocess.Popen(["open", PERMISSION_URLS[perm]]))
        p.quit_requested.connect(self.quit)
        p.confirmed.connect(self._confirmed)
        p.mic_held.connect(lambda on: self._ptt_down() if on else self._ptt_up())
        p.replay_requested.connect(lambda i: self.submit(self.replay(i)))

    def submit(self, coro) -> None:
        self.runner.submit(self._guard(coro))

    async def _guard(self, coro) -> None:
        try:
            await coro
        except MetaAPIError as exc:
            log.error("model call failed: %s", exc)
            ui(self.overlay.set_state, "idle")
            ui(self.overlay.toast, _friendly(exc), "error", 8)
            ui(self.panel.set_status, "Something went wrong", theme.DANGER)
        except PermissionError as exc:
            ui(self.overlay.toast, str(exc), "error", 10)
        except Exception as exc:
            log.exception("unexpected failure")
            ui(self.overlay.set_state, "idle")
            ui(self.overlay.toast, f"Unexpected error: {exc}", "error", 8)

    def _refresh_status(self) -> None:
        self.panel.set_key_status(self.client.has_key())
        self.panel.set_permission("screen", screen.has_screen_recording_permission())
        self.panel.set_permission("accessibility", control.accessibility_trusted())
        self.panel.set_permission("microphone", microphone_authorized())
        self.panel.set_status("Ready" if self.client.has_key() else "Needs an API key",
                              theme.SUCCESS if self.client.has_key() else theme.THINKING)

    def _hide_for_capture(self, hide: bool) -> None:
        # Fallback path only: runs on a worker thread, so marshal to Qt and give it a frame.
        ui(self.overlay.set_alpha, 0.0 if hide else 1.0)
        ui(macwin.set_alpha, self.panel, 0.0 if hide else 1.0)
        ui(macwin.set_alpha, self.panel.tab, 0.0 if hide else 1.0)
        time.sleep(0.05)

    async def capture(self, display=None) -> screen.Capture:
        return await asyncio.to_thread(self.capturer.capture, display)

    async def capture_for_question(self) -> list[screen.Capture]:
        mouse = screen.mouse_position()
        ds = screen.displays()
        first = screen.display_at(*mouse, ds)
        order = [first] + [d for d in ds if d.display_id != first.display_id]
        return [await self.capture(d) for d in order[:3]]

    # ------------------------------------------------------------ push to talk

    def _ptt_down(self) -> None:  # pynput thread
        if not self.client.has_key():
            ui(self.overlay.toast, "Add your Meta Model API key in Settings first.", "error", 5)
            return
        self.speaker.stop()
        ui(self.overlay.set_state, "listening")
        ui(self.overlay.set_caption, "")
        self.runner.loop.call_soon_threadsafe(self._begin_ptt)

    def _begin_ptt(self) -> None:  # asyncio loop
        session = PushToTalkSession(self.client, keywords=["Nimbus", "Muse", "SAM"])
        session.on_partial = lambda text: ui(self.overlay.set_caption, text)
        session.start()
        self._ptt = session
        loop = self.runner.loop
        try:
            self.mic.start(lambda chunk: loop.call_soon_threadsafe(session.feed, chunk),
                           lambda level: (ui(self.overlay.set_audio_level, level), ui(self.panel.mic.set_level, level)))
        except Exception as exc:
            log.error("microphone failed: %s", exc)
            session.cancel()
            self._ptt = None
            ui(self.overlay.set_state, "idle")
            ui(self.overlay.toast, "Microphone unavailable. Allow Microphone access in System Settings.", "error", 8)

    def _ptt_up(self) -> None:  # pynput thread
        # Queued behind _begin_ptt on the same loop, so a release that arrives while the mic is still
        # opening (the first-run permission prompt blocks there) stops the stream that start opened.
        self.runner.loop.call_soon_threadsafe(self._end_ptt)

    def _end_ptt(self) -> None:  # asyncio loop
        self.mic.stop()
        session, self._ptt = self._ptt, None
        if session is None:
            return
        ui(self.overlay.set_state, "thinking")

        async def finish() -> None:
            text = await session.finish()
            if not text:
                ui(self.overlay.set_state, "idle")
                ui(self.overlay.set_caption, "")
                return
            ui(self.overlay.set_caption, f"“{text}”")
            await self.handle_question(text, act=await ui_call(self.panel.act_toggle.isChecked))

        self.submit(finish())

    def _escape(self) -> None:  # pynput thread: cancel a question mid-sentence, stop talking, clear the screen, stop any agent
        self.runner.loop.call_soon_threadsafe(self._cancel_ptt)
        self._stop_agent = True
        self.speaker.stop()
        ui(self.overlay.clear_marks)
        ui(self.overlay.set_caption, "")
        ui(self.overlay.release)
        ui(self.overlay.set_state, "idle")

    def _cancel_ptt(self) -> None:  # asyncio loop: queued behind _begin_ptt, so it always sees the open session
        session, self._ptt = self._ptt, None
        if session is None:
            return
        self.mic.stop()
        session.cancel()
        ui(self.overlay.set_audio_level, 0.0)
        ui(self.overlay.toast, "Cancelled. Nothing was asked.", "info", 2.5)

    def _on_click(self, x: float, y: float) -> None:  # pynput thread
        if self.shadow is None or self._is_own_ui(x, y):
            return
        self.runner.loop.call_soon_threadsafe(self.shadow.on_click, x, y)

    def _on_key(self, is_return: bool) -> None:  # pynput thread
        if self.shadow is not None and not self.panel.isActiveWindow():  # typing into Nimbus is not the step
            self.runner.loop.call_soon_threadsafe(self.shadow.on_key, is_return)

    def _is_own_ui(self, x: float, y: float) -> bool:
        for w in (self.panel, self.panel.tab):
            if w.isVisible() and w.geometry().contains(int(x), int(y)):
                return True
        return False

    # ------------------------------------------------------------ Ask

    async def handle_question(self, question: str, *, act: bool = False) -> None:
        self._stop_agent = False
        ui(self.overlay.clear_marks)
        ui(self.overlay.set_state, "thinking")
        ui(self.panel.set_status, "Looking at your screen…", theme.THINKING)
        captures = await self.capture_for_question()
        app = await asyncio.to_thread(control.frontmost_app)
        walk, raw = await self.ask.plan(question, captures, app, allow_operate=act, cursor=screen.mouse_position())
        acting = walk.intent == "operate" and bool(walk.operate_task) and act
        if acting and not walk.show_steps:
            walk.steps = []  # just do it: no plan on screen or in the card unless it was asked for
        self._walks.append(walk)
        ui(self.panel.show_answer, question, _walk_html(walk), len(self._walks) - 1, _walk_text(walk))
        ui(self.panel.set_status, "Ready", theme.SUCCESS)
        if acting:
            await self._say(walk.reply)
            await self.operate(walk.operate_task, target=app, narrate=walk.show_steps)
            return
        refining = None
        if self.settings.refine_pointing and any(s.kind in ("point", "box") for s in walk.steps):
            refining = asyncio.ensure_future(self.ask.refine(walk, raw, captures))
        await self.play(walk, refining)

    async def play(self, walk: Walkthrough, refining: asyncio.Future | None = None) -> None:
        ui(self.overlay.set_state, "pointing" if walk.steps else "idle")
        await self._say(walk.reply)  # the zoom refine runs while this is spoken
        if refining is not None:
            try:
                await asyncio.wait_for(refining, timeout=8)
            except Exception as exc:  # a slow or failed refine keeps the first-pass points
                log.warning("pointing refine did not finish: %s", exc)
        numbered = sum(1 for s in walk.steps if s.kind != "say") > 1
        n = 0
        for step in walk.steps:
            if self._stop_agent:
                break
            if step.kind != "say" and step.gx is not None:
                n += 1
                await ui_wait(lambda done, s=step: self.overlay.fly_to(s.gx, s.gy, on_land=done), timeout=4)
                mark = Mark(kind={"point": "ring", "box": "box", "note": "note"}[step.kind], x=step.gx, y=step.gy,
                            box=step.box if step.kind == "box" else None,
                            label=step.target if step.kind == "point" else "",  # a framed area needs no name pill
                            note=step.note, step=n if numbered else None, below=step.below)
                ui(self.overlay.add_mark, mark)
            await self._say(step.say)
        await asyncio.sleep(1.2)
        ui(self.overlay.set_caption, "")
        ui(self.overlay.release)
        ui(self.overlay.set_state, "idle")

    async def replay(self, index: int) -> None:
        """Show an earlier answer again, with the points it already sharpened."""
        if not 0 <= index < len(self._walks):
            return
        self._stop_agent = False
        self.speaker.stop()
        ui(self.overlay.clear_marks)
        await self.play(self._walks[index])

    async def _say(self, text: str) -> None:
        if not text:
            return
        ui(self.overlay.set_caption, text)
        if self.settings.speak_answers:
            await self.speaker.speak(text)
            await asyncio.sleep(0.15)
        else:
            await asyncio.sleep(reading_time(text))

    async def _say_last(self, text: str) -> None:
        """A closing line (a result, nothing after it): said, then taken off the screen after a moment,
        so it does not sit over the person's work until they press Esc."""
        await self._say(text)
        await asyncio.sleep(CLOSING_CAPTION_S)
        if self.overlay.caption == text.strip():
            ui(self.overlay.set_caption, "")

    # ------------------------------------------------------------ acting (computer use)

    async def confirm(self, text: str) -> bool:
        if not self.settings.confirm_before_acting:
            return True
        loop = asyncio.get_running_loop()
        self._confirm_future = loop.create_future()
        ui(self.panel.ask_confirmation, text)
        try:
            return await asyncio.wait_for(self._confirm_future, timeout=120)
        except asyncio.TimeoutError:
            return False

    def _confirmed(self, ok: bool) -> None:  # Qt thread
        fut = self._confirm_future
        if fut is not None:
            self.runner.loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(ok))

    async def operate(self, task: str, target: control.AppContext | None = None, *, narrate: bool = False,
                      ask_first: bool = True, report: bool = True) -> AgentResult:
        """Do `task` with the computer-use agent. Quiet by default: the buddy hops to each spot with a
        busy spinner and the result is spoken at the end. `narrate` labels every action on screen."""
        if not control.accessibility_trusted(prompt=True):
            ui(self.overlay.toast, "Allow Accessibility for this app so Nimbus can use the mouse and keyboard.",
               "error", 10)
            return AgentResult("declined", "Accessibility permission missing", 0)
        if ask_first and not await self.confirm(f"Nimbus wants to control your Mac to: {task}\nPress Esc at any time to stop."):
            await self._say_last("Okay, I won't touch anything.")
            return AgentResult("declined", "Cancelled", 0)
        self._stop_agent = False
        ui(self.overlay.set_state, "acting")
        if not narrate:
            ui(self.overlay.set_caption, "")
        ui(self.panel.collapse)
        await asyncio.sleep(0.3)
        target = target or await asyncio.to_thread(control.frontmost_app)
        await asyncio.to_thread(control.give_focus_back, target.pid)

        async def before_action(gx, gy, action) -> None:
            if gx is not None:
                await ui_wait(lambda done: self.overlay.fly_to(gx, gy, on_land=done, quick=True), timeout=2)
                if narrate:
                    label = action.get("type", "").replace("_", " ")
                    ui(self.overlay.add_mark, Mark("ring", gx, gy, label=label, ttl=2.5))
            if narrate:
                ui(self.overlay.set_caption, _describe_action(action))

        async def approve(checks) -> bool:
            # The person asked for this task and allowed it once; routine checks are acknowledged for
            # them. Only a warning that the screen is trying to steer the agent comes back to them.
            log.info("computer-use safety checks: %s", [c.get("code") for c in checks])
            steering = [c for c in checks if _is_injection_warning(c)]
            if not steering:
                return True
            ok = await self.confirm("Something on screen may be trying to redirect Nimbus: "
                                    + "; ".join(c.get("message", "") for c in steering) + "\nKeep going?")
            await asyncio.to_thread(control.give_focus_back, target.pid)
            return ok

        driver = control.MacDriver(self.capturer, before_action=before_action, target_pid=target.pid)
        self.clicks.suppressed.set()
        try:
            result = await run_agent(self.client, driver, task, max_steps=self.settings.max_agent_steps,
                                     approve=approve, should_stop=lambda: self._stop_agent)
        finally:
            self.clicks.suppressed.clear()
            ui(self.overlay.release)
            ui(self.overlay.set_state, "idle")
        if not report:
            return result
        await self._say_last(result.message if result.status == "done" else f"I stopped: {result.message}")
        outcome = result.message if result.status == "done" else f"Stopped ({result.status}): {result.message}"
        ui(self.panel.show_answer, f"Do: {task}", html.escape(outcome))
        return result

    # ------------------------------------------------------------ Shadow

    async def open_tutorial(self, video: Path) -> None:
        if not self.client.has_key():
            raise MetaAPIError("MODEL_API_KEY is not set", status=401)
        plan_dir = config.data_dir("shadow", f"{int(time.time())}-{_slug(video.stem)}")
        ui(self.panel.open)
        ui(self.panel.shadow_loading, "Preparing…", 0.02)
        ui(self.overlay.set_state, "thinking")
        analyzer = VideoAnalyzer(self.client)
        plan = await analyzer.analyze(video, plan_dir, kind="tutorial",
                                      progress=lambda m, f: ui(self.panel.shadow_loading, m, f))
        ui(self.overlay.set_state, "idle")
        self._refresh_recent_guides_async()
        await self._start_shadow(plan, plan_dir)

    async def resume_tutorial(self, plan_dir: Path) -> None:
        plan = StepPlan.load(plan_dir)
        await self._start_shadow(plan, plan_dir)

    async def _start_shadow(self, plan: StepPlan, plan_dir: Path) -> None:
        if self.shadow:
            await self.shadow.stop()
        ui(self.panel.shadow_ready, plan.title or "Tutorial", f"{len(plan.steps)} steps · {plan.app}")
        self.shadow = ShadowSession(self.client, _ShadowHost(self), plan, plan_dir)
        await self.shadow.start()

    async def shadow_action(self, action: str) -> None:
        if self.shadow is None:
            ui(self.overlay.toast, "Open a tutorial video first.", "info", 4)
            return
        await self.shadow.handle(action)
        if action == "stop":
            self.shadow = None

    def _refresh_recent_guides(self) -> None:
        guides = []
        root = config.data_dir("shadow")
        for d in sorted(root.iterdir(), reverse=True):
            if (d / "plan.json").exists():
                try:
                    plan = StepPlan.load(d)
                    guides.append((str(d), f"{plan.title or d.name}  ·  {len(plan.steps)} steps"))
                except Exception:
                    continue
        self.panel.set_recent_guides(guides)

    def _refresh_recent_guides_async(self) -> None:
        ui(self._refresh_recent_guides)

    # ------------------------------------------------------------ Repro

    async def repro_record(self, start: bool) -> None:
        from nimbus.repro import recorder

        if start:
            self._repro_recorder = recorder.ScreenRecorder(config.data_dir("recordings"))
            try:
                await self._repro_recorder.start()
            except recorder.RecorderError as exc:
                self._repro_recorder = None
                ui(self.panel.record_btn.setChecked, False)
                ui(self.overlay.toast, str(exc), "error", 10)
                return
            ui(self.overlay.toast, "Recording. Reproduce the bug and describe it out loud, then press Stop.", "info", 6)
            ui(self.panel.collapse)
            return
        if self._repro_recorder is None:
            return
        recording = await self._repro_recorder.stop()
        self._repro_recorder = None
        await self.run_repro(Path(recording.path))

    async def run_repro(self, video: Path) -> None:
        from nimbus.repro.pipeline import ReproPipeline

        ui(self.panel.open)
        ui(self.panel.repro_reset)
        stages_done: list[str] = []

        def progress(stage: str, message: str, fraction: float) -> None:
            # A finished stage keeps the last line it reported; only the running stage shows new detail.
            for done in stages_done:
                if done != stage:
                    ui(self.panel.repro_stage, done, "", fraction, "done")
            if stage not in stages_done:
                stages_done.append(stage)
            ui(self.panel.repro_stage, stage, message, fraction, "active")

        self._repro_cancelled = False
        pipeline = ReproPipeline(self.client, self.settings)
        result = await pipeline.run(video, progress=progress, should_stop=lambda: self._repro_cancelled)
        ok = result.success
        last = stages_done[-1] if stages_done else "report"
        outcome = result.message + (f"  PR: {result.pr_url}" if result.pr_url else "")
        if ok:  # the last stage keeps its own line; the outcome goes under the timeline
            ui(self.panel.repro_stage, last, "", 1.0, "done")
            ui(self.panel.repro_message.setText, outcome)
        else:
            ui(self.panel.repro_stage, last, outcome, 1.0, "failed")
        report = result.report_html or result.report_md
        if report:
            self._last_report = Path(report)
            ui(self.panel.report_btn.setEnabled, True)
        await self._say_last("The fix is verified and ready for review." if ok else "I couldn't finish the repro. "
                             "The report says why.")

    def _cancel_repro(self) -> None:
        self._repro_cancelled = True

    def _open_report(self) -> None:
        if self._last_report and self._last_report.exists():
            subprocess.Popen(["open", str(self._last_report)])

    # ------------------------------------------------------------ settings

    def _setting_changed(self, key: str, value) -> None:
        s = self.settings
        if key == "open_weights":
            s.open_weights.enabled = bool(value)
        elif hasattr(s, key):
            setattr(s, key, value)
        if key == "buddy_enabled":
            self.overlay.set_buddy_visible(bool(value))
        self.store.save()

    def _save_key(self, key: str) -> None:
        try:
            config.set_secret("MODEL_API_KEY", key)
        except Exception as exc:
            self.overlay.toast(f"Could not save the key to Keychain: {exc}", "error", 8)
            return
        self.client._clients.clear()
        self._refresh_status()
        self.overlay.toast("Key saved. Nimbus is ready.", "success", 4)

    def quit(self) -> None:
        self.speaker.stop()
        self.hotkey.stop()
        self.clicks.stop()
        self.store.save()
        self.runner.stop()
        self.qt.quit()


class _ShadowHost:
    """ShadowSession's view of the app."""

    def __init__(self, app: NimbusApp) -> None:
        self.app = app

    async def capture(self) -> screen.Capture:
        return await self.app.capture()

    async def show_step(self, plan: StepPlan, step: PlanStep, plan_dir: Path) -> None:
        crop = plan_dir / step.evidence.target_crop if step.evidence.target_crop else None
        ui(self.app.panel.shadow_step, step.index, len(plan.steps), step.instruction, step.expected_after,
           step.evidence.click_time if step.evidence.click_time is not None else step.t_start, crop)

    async def point(self, gx, gy, box, label, step_no) -> None:
        overlay = self.app.overlay
        panel = self.app.panel
        x0, y0, x1, y1 = box or (gx - 20, gy - 20, gx + 20, gy + 20)
        ui(panel.keep_clear, QRect(int(x0), int(y0), int(x1 - x0), int(y1 - y0)))
        await ui_wait(lambda done: overlay.fly_to(gx, gy, on_land=done), timeout=4)
        ui(overlay.add_mark, Mark("box" if box else "ring", gx, gy, box=box, label=label, step=step_no, ttl=600))

    async def clear_marks(self) -> None:
        ui(self.app.overlay.clear_marks)
        ui(self.app.overlay.release)

    async def say(self, text: str, *, speak: bool = True) -> None:
        if speak:
            await self.app._say(text)
        else:
            ui(self.app.overlay.set_caption, text)

    async def verdict(self, text: str, ok) -> None:
        ui(self.app.panel.shadow_verdict_text, text, ok)

    async def finished(self, text: str) -> None:
        ui(self.app.panel.keep_clear, None)
        ui(self.app.panel.shadow_finished, text)
        ui(self.app.overlay.set_caption, "")

    async def run_agent(self, task: str) -> AgentResult:
        # "Do it for me" is the person's go-ahead; asking again would put a prompt on another page.
        # Shadow reports the outcome on its own card, and the panel comes back for the next step.
        panel = self.app.panel
        was_open = await ui_call(lambda: panel.is_open)
        try:
            return await self.app.operate(task, ask_first=False, report=False)
        finally:
            if was_open:
                ui(panel.open)


def _walk_html(walk: Walkthrough) -> str:
    out = [html.escape(walk.reply)]
    items = [f"<li>{html.escape(s.say)}" + (f" <span style='color:{theme.TEXT_MUTED}'>({html.escape(s.target)})</span>"
                                            if s.target else "") + "</li>" for s in walk.steps if s.say]
    if items:
        out.append("<ol style='margin:4px 0 0 -18px'>" + "".join(items) + "</ol>")
    return "".join(out)


def _walk_text(walk: Walkthrough) -> str:
    lines = [walk.reply] + [f"\u2022 {s.say}" + (f"  ({s.note})" if s.note else "") for s in walk.steps if s.say]
    return "\n".join(line for line in lines if line.strip())


def _is_injection_warning(check: dict) -> bool:
    text = f"{check.get('code', '')} {check.get('message', '')}".lower()
    return any(w in text for w in ("malicious", "injection", "instructions on the screen", "untrusted instruction"))


def _describe_action(action: dict) -> str:
    """Caption while acting; empty for pointer actions, where the moving cursor already says it."""
    kind = action.get("type", "")
    if kind in ("click", "double_click", "move", "drag", "scroll", "screenshot"):
        return ""
    if kind == "type":
        return f"Typing “{str(action.get('text', ''))[:60]}”"
    if kind == "keypress":
        return "Pressing " + "+".join(action.get("keys", []))
    if kind == "wait":
        return "Waiting for the app…"
    return kind.replace("_", " ").capitalize()


def _friendly(exc: MetaAPIError) -> str:
    if exc.status == 401:
        return "The Meta Model API key was rejected or is missing. Check it in Settings."
    if exc.status == 429:
        return "Rate limited by the Meta Model API. Give it a few seconds."
    return f"Meta Model API error: {str(exc)[:180]}"


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower())[:40].strip("-") or "video"


def _pretty(hotkey: str) -> str:
    names = {"<ctrl>": "⌃", "<alt>": "⌥", "<cmd>": "⌘", "<shift>": "⇧", "<space>": "Space"}
    return "".join(names.get(p, p.upper()) for p in hotkey.split("+"))

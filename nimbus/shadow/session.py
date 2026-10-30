"""Shadow: walk a person through a tutorial on their own screen, one verified step at a time.

For each step of the plan:
1. Find the control on the live screen, using the tutorial's crop of it as a visual reference, so
   it is found even when the app version, theme or window size differs from the video.
2. Fly the buddy there, frame it, show and say the instruction.
3. When the person clicks (or presses "I did it"), compare before/after screenshots with the
   tutorial's after-frame. Advance only on a verdict that cites visible evidence.
4. After three failed checks, stop guessing and hand the choice to the person: show again, let
   Nimbus do the step, or skip. Progress is saved after every step, so a guide can be resumed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from nimbus.engine.grounding import Grounder
from nimbus.engine.steps import PlanStep, StepPlan
from nimbus.engine.verify import Verdict, Verifier
from nimbus.mac.screen import Capture
from nimbus.meta.client import MetaClient
from nimbus.meta.computer import AgentResult

log = logging.getLogger(__name__)

MAX_FAILED_CHECKS = 3
# Steps whose only effect is keyboard focus ("click into the field") leave nothing to verify from pixels.
FOCUS_ONLY = re.compile(r"\b(focus(?:ed|es)?|cursor|caret|blinking|becomes active|is active|ready (?:for|to) typ\w*)\b",
                        re.I)
FIELD_WORDS = re.compile(r"\b(input|field|text ?box|textarea|search box|message box|composer|prompt|editor)\b", re.I)
CLICK_SETTLE_S = 0.9
TYPING_PAUSE_S = 1.6  # a typing step is checked once the person stops typing for this long


class ShadowHost(Protocol):
    """What the session needs from the app; implemented by the controller, faked in tests."""

    async def capture(self) -> Capture: ...
    async def show_step(self, plan: StepPlan, step: PlanStep, plan_dir: Path) -> None: ...
    async def point(self, gx: float, gy: float, box: tuple[float, float, float, float] | None, label: str,
                    step_no: int) -> None: ...
    async def clear_marks(self) -> None: ...
    async def say(self, text: str, *, speak: bool = True) -> None: ...
    async def verdict(self, text: str, ok: bool | None) -> None: ...
    async def finished(self, text: str) -> None: ...
    async def run_agent(self, task: str) -> AgentResult: ...


@dataclass
class Progress:
    index: int = 0
    completed: list[int] | None = None
    updated: float = 0.0


class ShadowSession:
    def __init__(self, client: MetaClient, host: ShadowHost, plan: StepPlan, plan_dir: Path) -> None:
        self.client = client
        self.host = host
        self.plan = plan
        self.dir = plan_dir
        self.grounder = Grounder(client)
        self.verifier = Verifier(client)
        self.index = 0
        self.completed: set[int] = set()
        self.assumed: set[int] = set()  # advanced on the person's click, nothing visible to verify
        self.failed_checks = 0
        self.before: Capture | None = None
        self.active = False
        self.waiting = False  # waiting for the person to act on the current step
        self._lock = asyncio.Lock()
        self._timer: asyncio.TimerHandle | None = None  # the debounced check after the person acts
        self._voice: asyncio.Task | None = None
        self._load_progress()

    # ------------------------------------------------------------ persistence

    def _progress_path(self) -> Path:
        return self.dir / "progress.json"

    def _load_progress(self) -> None:
        try:
            data = json.loads(self._progress_path().read_text())
            self.index = max(0, min(int(data.get("index", 0)), len(self.plan.steps) - 1))
            self.completed = set(data.get("completed") or [])
        except (OSError, ValueError):
            pass

    def _save_progress(self) -> None:
        self._progress_path().write_text(json.dumps(
            {"index": self.index, "completed": sorted(self.completed), "updated": time.time()}))

    @property
    def step(self) -> PlanStep:
        return self.plan.steps[self.index]

    # ------------------------------------------------------------ flow

    async def start(self) -> None:
        self.active = True
        if self.index >= len(self.plan.steps):
            self.index = 0
        intro = f"{self.plan.title or 'This tutorial'}, in {len(self.plan.steps)} steps."
        start = self.plan.start_state.strip().rstrip(".")
        precondition = ""
        if start and len(start) <= 90:  # a long scene description is for the model, not to be read out
            first, _, rest = start.partition(" ")
            if first in ("The", "A", "An", "Your", "You", "This", "That", "It"):  # keep names capitalised
                start = f"{first.lower()} {rest}"
            precondition = f"First, make sure {start}."
        async with self._lock:
            await self._present_locked(intro=intro, precondition=precondition)

    async def present(self) -> None:
        async with self._lock:
            await self._present_locked()

    def _say(self, text: str) -> None:
        """Speak without holding up the guide: the person may act while Nimbus is still talking."""
        if text:
            self._voice = asyncio.ensure_future(self.host.say(text))

    async def _present_locked(self, intro: str = "", precondition: str = "") -> None:
        if not self.active:
            return
        step = self.step
        self.failed_checks = 0
        self.waiting = False
        await self.host.clear_marks()
        await self.host.show_step(self.plan, step, self.dir)
        self.before = await self.host.capture()
        if step.needs_target:
            await self.host.verdict(f"Finding “{step.target}” on your screen…", None)
        located = await self._locate(step, self.before)
        if located is not None:
            gx, gy, box = located
            await self.host.point(gx, gy, box, step.target or "here", step.index + 1)
            await self.host.verdict("", None)
        elif step.needs_target:
            await self.host.verdict(f"I can't see “{step.target}” on screen yet. "
                                    + (f"It should be {step.target_description}" if step.target_description else
                                       "Open the right window, then press Show me."), None)
        self.waiting = True
        if located is not None:
            precondition = ""  # the first control is already on screen: the starting point is met, skip it
        self._say(" ".join(t for t in (intro, precondition, step.instruction or step.target) if t))

    async def _locate(self, step: PlanStep, cap: Capture):
        if not (step.target or step.needs_target):
            return None
        reference = None
        if step.evidence.target_crop and (self.dir / step.evidence.target_crop).exists():
            reference = (self.dir / step.evidence.target_crop).read_bytes()
        context = f"This is step {step.index + 1} of a tutorial for {self.plan.app or 'this app'}: {step.instruction}"
        hit = await self.grounder.locate(cap.png, step.target or step.instruction, description=step.target_description,
                                         reference=reference, context=context,
                                         refine=self.client.settings.refine_pointing)
        if hit is None:
            return None
        gx, gy = cap.pixel_to_global(hit.x, hit.y)
        box = None
        if hit.box:
            a, b = cap.pixel_to_global(hit.box[0], hit.box[1]), cap.pixel_to_global(hit.box[2], hit.box[3])
            box = (a[0], a[1], b[0], b[1])
        return gx, gy, box

    def on_click(self, x: float, y: float) -> None:
        """A real click anywhere (on the asyncio loop). Never dropped: if Nimbus is still pointing or
        checking, the check waits its turn; it is skipped only if the step has moved on by then."""
        if not self.active or self.step.action in ("type", "keypress"):
            return  # typing steps are checked when the typing stops or Return is pressed
        self._schedule_check(CLICK_SETTLE_S, "click")

    def on_key(self, is_return: bool) -> None:
        """A key press anywhere. Typing steps are checked when the person pauses or presses Return."""
        if not self.active or self.step.action not in ("type", "keypress", "select"):
            return
        self._schedule_check(0.6 if is_return else TYPING_PAUSE_S, "key")

    def on_return_key(self) -> None:
        self.on_key(True)

    def _schedule_check(self, delay: float, trigger: str) -> None:
        if self._timer is not None:
            self._timer.cancel()
        index = self.index
        self._timer = asyncio.get_running_loop().call_later(
            delay, lambda: asyncio.ensure_future(self.check(trigger=trigger, index=index)))

    def _focus_only(self, step: PlanStep) -> bool:
        """A click whose only effect is keyboard focus. Said in words ("cursor appears"), or structural: a
        click on a text field that the very next step types into."""
        if step.action != "click":
            return False
        if FOCUS_ONLY.search(f"{step.expected_after} {step.instruction}"):
            return True
        nxt = self.plan.steps[step.index + 1] if step.index + 1 < len(self.plan.steps) else None
        if not (nxt and nxt.action == "type"):
            return False
        same_control = step.target and step.target.strip(" .").lower() == nxt.target.strip(" .").lower()
        return bool(same_control or FIELD_WORDS.search(f"{step.target} {step.instruction}"))

    async def check(self, trigger: str = "manual", index: int | None = None) -> Verdict | None:
        """Verify the current step. `trigger` says why: "click"/"key" (the person acted), "manual"
        ("I did it"), or "probe" (a background check with no action behind it). `index` is the step
        the action was meant for; a check that arrives after that step is done is dropped."""
        async with self._lock:
            if not self.active or (index is not None and index != self.index):
                return None
            step = self.step
            await self.host.verdict("Checking…", None)
            after = await self.host.capture()
            reference = None
            if step.evidence.after_frame and (self.dir / step.evidence.after_frame).exists():
                reference = (self.dir / step.evidence.after_frame).read_bytes()
            verdict = await self.verifier.check(
                instruction=step.instruction, expected=step.expected_after,
                before=self.before.png if self.before else None, after=after.png, reference_after=reference,
                context=f"Tutorial: {self.plan.title}. App: {self.plan.app}.",
            )
            if verdict.passed:
                self.completed.add(step.index)
                await self.host.verdict(f"✓ {verdict.evidence}", True)
                await self._advance_locked(spoken="Nice.")
                return verdict
            if (self._focus_only(step) and trigger in ("click", "manual", "key")
                    and verdict.status in ("uncertain", "not_done")):
                self.assumed.add(step.index)
                await self.host.verdict("Clicked into the field. Focus has no visible change, so this one is "
                                        "assumed rather than verified.", None)
                await self._advance_locked()
                return verdict
            self.failed_checks += 1
            if self.failed_checks >= MAX_FAILED_CHECKS:
                await self.host.verdict(
                    f"I still can't confirm this step ({verdict.problem or 'no visible change'}). "
                    "Press Show me, Do it for me, or Skip.", False)
                self._say("I can't confirm that one. Want me to show you again, or do it for you?")
            else:
                text = verdict.hint or verdict.problem or "Not quite yet."
                await self.host.verdict(text, False if verdict.status != "uncertain" else None)
                self._say(text)
            return verdict

    async def _advance_locked(self, spoken: str = "") -> None:
        self._save_progress_next()
        if self.index >= len(self.plan.steps):
            self.active = False
            self.waiting = False
            await self.host.clear_marks()
            done = len(self.completed)
            extra = f", {len(self.assumed)} focus-only" if self.assumed else ""
            await self.host.finished(f"Done! You finished all {len(self.plan.steps)} steps ({done} verified{extra}).")
            self._say("That's the whole tutorial. Nice work.")
            return
        await self._present_locked(intro=spoken)

    def _save_progress_next(self) -> None:
        self.index += 1
        self._save_progress()

    # ------------------------------------------------------------ panel actions

    async def handle(self, action: str) -> None:
        if action == "show":
            await self.present()
        elif action == "check":
            await self.check()
        elif action == "skip":
            async with self._lock:
                if self.active:
                    await self._advance_locked()
        elif action == "back":
            async with self._lock:
                self.index = max(0, self.index - 1)
                self._save_progress()
                await self._present_locked()
        elif action == "do":
            await self.do_for_me()
        elif action == "stop":
            await self.stop()

    async def do_for_me(self) -> None:
        async with self._lock:
            step = self.step
            self.waiting = False
            task = (f"Perform exactly this one step and nothing more: {step.instruction}. "
                    f"The control is '{step.target}' ({step.target_description}). "
                    + (f"Type exactly: {step.text}. " if step.action == "type" and step.text else "")
                    + (f"Press: {step.text}. " if step.action == "keypress" and step.text else "")
                    + f"Expected result: {step.expected_after}.")
            await self.host.verdict("Doing this step for you…", None)
            result = await self.host.run_agent(task)
            if result.status != "done":
                await self.host.verdict(f"I couldn't finish it: {result.message}", False)
                self.waiting = True
                return
            after = await self.host.capture()
            verdict = await self.verifier.check(instruction=step.instruction, expected=step.expected_after,
                                                before=self.before.png if self.before else None, after=after.png)
            if verdict.passed:
                self.completed.add(step.index)
                await self.host.verdict(f"✓ Done for you: {verdict.evidence}", True)
                await self._advance_locked()
            else:
                await self.host.verdict(f"I acted but can't confirm it worked: {verdict.problem}", False)
                self.waiting = True

    async def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self.active = False
        self.waiting = False
        self._save_progress()
        await self.host.clear_marks()
        await self.host.finished(f"Paused at step {self.index + 1}. Open it from Recent guides to resume.")

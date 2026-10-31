"""Shadow end-to-end eval: learn a tutorial from video, then guide a simulated person through it.

1. VideoAnalyzer turns artifacts/fixtures/tutorial_chat.mp4 into a step plan (Muse Spark watches
   the video, SAM 3.1 tracks the cursor and crops each clicked control).
2. The same app is opened in a DIFFERENT theme (dark) in headless Chromium, so Shadow has to find
   controls that look different from the video.
3. A `ShadowHost` backed by the page drives a real `ShadowSession`. For every step:
   - pointing is scored against the true element from the tutorial script;
   - a negative check runs first (the person did nothing): it must NOT pass;
   - the simulated person performs the step, then the check must pass.

    .venv/bin/python tools/eval_shadow.py [--plan-dir DIR]   # reuse an analysed plan
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image
from playwright.async_api import Page, async_playwright

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # runnable as tools/<name>.py

from nimbus.engine.steps import StepPlan  # noqa: E402
from nimbus.engine.video import VideoAnalyzer  # noqa: E402
from nimbus.mac.screen import Capture, Display  # noqa: E402
from nimbus.meta.client import MetaClient  # noqa: E402
from nimbus.meta.computer import AgentResult  # noqa: E402
from nimbus.shadow.session import ShadowSession  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
VIDEO = ROOT / "artifacts" / "fixtures" / "tutorial_chat.mp4"
FIXTURE = ROOT / "tests" / "fixtures" / "ui" / "chat_app.html"
OUT = ROOT / "artifacts" / "evals"


@dataclass
class StepScore:
    index: int
    instruction: str
    expected_selector: str
    pointed: bool = False
    point_hit: bool = False
    negative_check_held: bool = False
    positive_check_passed: bool = False
    evidence: str = ""
    assumed: bool = False


@dataclass
class Report:
    plan_steps: int
    script_steps: int
    analyze_s: float
    scores: list[StepScore] = field(default_factory=list)


class PageHost:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.last_point: tuple[float, float] | None = None
        self.messages: list[str] = []

    async def capture(self) -> Capture:
        png = await self.page.screenshot()
        img = Image.open(io.BytesIO(png))
        return Capture(Display(0, 1, 0, 0, 1440, 900, 2.0, True), png, img.width, img.height, time.time())

    async def show_step(self, plan, step, plan_dir) -> None:
        self.last_point = None

    async def point(self, gx, gy, box, label, step_no) -> None:
        self.last_point = (gx, gy)

    async def clear_marks(self) -> None:
        pass

    async def say(self, text, *, speak=True) -> None:
        self.messages.append(text)

    async def verdict(self, text, ok) -> None:
        self.messages.append(f"[{ok}] {text}")

    async def finished(self, text) -> None:
        self.messages.append(f"[finished] {text}")

    async def run_agent(self, task) -> AgentResult:
        return AgentResult("declined", "not used in this eval", 0)


SELECTORS = [  # checked in order against the step's target first, then its instruction
    (("new chat",), "#new-chat"),
    (("option", "menu item", "list item", "top item", "popup list", "menu, top"), "#menu [data-model='Muse Spark 1.3']"),
    (("model",), "#model-picker"),
    (("send",), "#send"),
    (("message", "prompt", "input", "reply", "text box", "textbox"), "#prompt"),
]


def _selector_for(step) -> str | None:
    """Map a plan step to the fixture element the simulated person should use."""
    name = step.target.split(" - ")[0].split(",")[0]  # the control's name, not its surroundings
    for text in (name.lower(), step.target.lower(), step.instruction.lower()):
        for keys, selector in SELECTORS:
            if any(k in text for k in keys):
                return selector
    return None


_ELEMENT_AT = """([x, y]) => {
  let el = document.elementFromPoint(x, y);
  while (el && !el.id && !(el.dataset && el.dataset.model)) el = el.parentElement;
  if (!el) return null;
  return el.dataset && el.dataset.model ? `#menu [data-model='${el.dataset.model}']` : `#${el.id}`;
}"""


async def _person_target(page, step) -> str | None:
    """Which element the simulated person uses for a step. Ground truth is what the presenter clicked:
    SAM measured that point in the video, and the fixture has the same layout in both themes, so the
    element under it is the answer. Typing goes to the message box; keywords are a last resort."""
    if step.action == "type":
        return "#prompt"
    if step.evidence.click_px and step.evidence.frame_size == (1440, 900):
        selector = await page.evaluate(_ELEMENT_AT, list(step.evidence.click_px))
        if selector and selector not in ("#thread", "#menu"):
            return selector
    return _selector_for(step)


async def main(plan_dir: Path | None) -> None:
    client = MetaClient()
    script = json.loads(VIDEO.with_suffix(".json").read_text())["script"]
    started = time.perf_counter()
    if plan_dir and (plan_dir / "plan.json").exists():
        plan = StepPlan.load(plan_dir)
    else:
        plan_dir = OUT / "shadow_plan"
        plan = await VideoAnalyzer(client).analyze(VIDEO, plan_dir, kind="tutorial",
                                                   progress=lambda m, f: print(f"  [{f:4.0%}] {m}"))
    analyze_s = time.perf_counter() - started
    print(f"plan: {plan.title!r}, {len(plan.steps)} steps (script has {len(script)}) in {analyze_s:.0f}s")
    for s in plan.steps:
        print(f"  {s.index}: {s.action:6} {s.target!r:28} t={s.t_start:.1f}-{s.t_end:.1f} "
              f"cursor={s.evidence.cursor_source} crop={bool(s.evidence.target_crop)}")
    report = Report(len(plan.steps), len(script), round(analyze_s, 1))
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        await page.goto(FIXTURE.as_uri() + "?theme=dark")
        host = PageHost(page)
        session = ShadowSession(client, host, plan, plan_dir)
        session.index, session.completed = 0, set()
        await session.start()
        typed_text = next((t for _, a, t, _ in script if a == "type"), "")
        for _ in range(len(plan.steps) + 2):
            if not session.active or session.index >= len(plan.steps):
                break
            step = session.step
            selector = await _person_target(page, step)
            score = StepScore(step.index, step.instruction, selector or "(none)")
            box = await page.locator(selector).bounding_box() if selector else None
            if host.last_point and box:
                x, y = host.last_point
                score.pointed = True
                score.point_hit = (box["x"] - 6 <= x <= box["x"] + box["width"] + 6
                                   and box["y"] - 6 <= y <= box["y"] + box["height"] + 6)
            neg = await session.check(trigger="probe")  # the person has not acted yet
            score.negative_check_held = bool(neg) and not neg.passed
            if session.index != step.index:  # a false pass advanced; step back for a fair positive check
                session.index = step.index
            if step.action == "type":
                await page.locator("#prompt").click()
                await page.keyboard.type(step.text or typed_text)
            elif selector:
                await page.locator(selector).click()
            await page.wait_for_timeout(400)
            before_index = session.index
            verdict = await session.check(trigger="click")
            score.positive_check_passed = bool(verdict and verdict.passed) or step.index in session.assumed
            score.assumed = step.index in session.assumed
            score.evidence = verdict.evidence if verdict else ""
            if session.index == before_index and session.active:  # failed check: move on for scoring
                await session.handle("skip")
            report.scores.append(score)
            print(f"step {step.index} {step.action:6} {selector!s:40} point={'hit' if score.point_hit else 'miss'} "
                  f"neg_held={score.negative_check_held} pos={score.positive_check_passed}  {score.evidence[:60]}")
        await browser.close()
    OUT.mkdir(parents=True, exist_ok=True)
    n = len(report.scores)
    summary = {
        "steps_found": f"{report.plan_steps} (script {report.script_steps})",
        "pointing_hits": f"{sum(s.point_hit for s in report.scores)}/{n}",
        "false_passes_blocked": f"{sum(s.negative_check_held for s in report.scores)}/{n}",
        "real_completions_verified": f"{sum(s.positive_check_passed and not s.assumed for s in report.scores)}/{n}",
        "focus_only_assumed": sum(s.assumed for s in report.scores),
        "analyze_seconds": report.analyze_s,
    }
    (OUT / "shadow.json").write_text(json.dumps({"summary": summary, **asdict(report)}, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan-dir", type=Path)
    asyncio.run(main(ap.parse_args().plan_dir))

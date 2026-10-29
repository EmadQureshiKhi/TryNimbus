"""The step plan: the one representation shared by Shadow and Repro.

A screen recording becomes an ordered list of steps. Each step says what was done, on which
control, and what the screen looked like afterwards. Shadow hands the plan to a person and guides
them through it; Repro hands it to an automated executor. The model fills `PLAN_SCHEMA`; the
pipeline then attaches evidence it measured itself (where the cursor was, a crop of the control,
the frame after the step).
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

ActionType = Literal[
    "click", "double_click", "right_click", "type", "keypress", "drag", "scroll",
    "navigate", "open_app", "select", "wait", "observe",
]

ACTIONS: list[str] = list(ActionType.__args__)  # type: ignore[attr-defined]


class Evidence(BaseModel):
    """Measured, not model-claimed: where things were in the source video."""

    click_time: float | None = None          # seconds into the source video
    click_px: tuple[int, int] | None = None  # cursor position in the source frame, pixels
    frame_size: tuple[int, int] | None = None
    target_box_px: tuple[int, int, int, int] | None = None  # clicked control, from SAM
    cursor_source: Literal["sam", "grounding", "none"] = "none"
    before_frame: str | None = None  # file names inside the plan directory
    after_frame: str | None = None
    target_crop: str | None = None


class PlanStep(BaseModel):
    index: int
    t_start: float = 0.0
    t_end: float = 0.0
    action: ActionType = "click"
    target: str = ""
    target_description: str = ""
    text: str = ""
    instruction: str = ""
    expected_after: str = ""
    narration: str = ""
    why: str = ""
    evidence: Evidence = Field(default_factory=Evidence)

    @property
    def needs_target(self) -> bool:
        return self.action in ("click", "double_click", "right_click", "drag", "select", "scroll") or (
            self.action == "type" and bool(self.target)
        )


class StepPlan(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:10])
    kind: Literal["tutorial", "bug"] = "tutorial"
    title: str = ""
    app: str = ""
    summary: str = ""
    start_state: str = ""
    steps: list[PlanStep] = Field(default_factory=list)
    source_video: str = ""
    duration_s: float = 0.0
    created_at: float = Field(default_factory=time.time)
    model: str = ""
    # Bug reports only.
    url: str = ""
    expected: str = ""
    actual: str = ""
    bug_time: float | None = None

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "plan.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(self.model_dump_json(indent=2))
        tmp.replace(path)
        return path

    @classmethod
    def load(cls, directory: Path) -> "StepPlan":
        return cls.model_validate(json.loads((directory / "plan.json").read_text()))


_STEP_PROPS = {
    "t_start": {"type": "number", "description": "seconds into the video where the step begins"},
    "t_end": {"type": "number", "description": "seconds where the step's effect is visible"},
    "action": {"type": "string", "enum": ACTIONS},
    "target": {"type": "string", "description": "the control's visible label or a short plain-English name, e.g. 'New chat button' (words, not identifiers); empty if none"},
    "target_description": {"type": "string", "description": "where the control is and what it looks like, for finding it on a different screen"},
    "text": {"type": "string", "description": "the exact value only, no prose: text typed, keys pressed (e.g. 'cmd+s'), the bare URL navigated to, or the app opened"},
    "instruction": {"type": "string", "description": "one imperative sentence telling a learner what to do"},
    "expected_after": {"type": "string", "description": "one plain-English sentence describing the visible change once the step is done, e.g. 'A menu opens listing three models' (no timestamps, no identifiers)"},
    "narration": {"type": "string", "description": "what the narrator said during this step, verbatim if possible"},
    "why": {"type": "string", "description": "one short sentence on why this step matters"},
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "app": {"type": "string", "description": "application (and version if visible) the video uses"},
        "summary": {"type": "string"},
        "start_state": {"type": "string", "description": "what must be open/visible before step 1"},
        "steps": {"type": "array", "items": {"type": "object", "properties": _STEP_PROPS,
                                             "required": list(_STEP_PROPS)}},
    },
    "required": ["title", "app", "summary", "start_state", "steps"],
}

BUG_SCHEMA = {
    "type": "object",
    "properties": {
        **PLAN_SCHEMA["properties"],
        "url": {"type": "string", "description": "the bare page URL visible in the recording (e.g. http://localhost:3000/cart), or empty"},
        "expected": {"type": "string", "description": "what should have happened, with the exact values"},
        "actual": {"type": "string", "description": "what happened instead (the bug), with the exact values"},
        "bug_time": {"type": "number", "description": "seconds into the video where the bug is visible"},
    },
    "required": [*PLAN_SCHEMA["required"], "url", "expected", "actual", "bug_time"],
}


_LEADING_TIME = re.compile(r"^\s*(?:t|time|seconds?|at)\s*[=:]?\s*\d+(?:\.\d+)?\s*s?\s*[,:;-]\s*", re.I)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9]+(?:[_.-][A-Za-z0-9.]+)+$")


def _humanize(text: str) -> str:
    """Models occasionally answer in identifiers ("composer_input_field"); people should read words."""
    t = (text or "").strip()
    if _IDENTIFIER.match(t) and "_" in t:
        t = t.replace("_", " ")
        t = t[:1].upper() + t[1:]
    return t


_ACTION_ALIASES = {"tap": "click", "press": "keypress", "shortcut": "keypress", "hotkey": "keypress",
                   "input": "type", "enter_text": "type", "goto": "navigate", "open": "open_app",
                   "launch": "open_app", "choose": "select", "dblclick": "double_click", "rightclick": "right_click"}


def plan_from_model(data: dict, *, kind: str, source_video: str, duration_s: float, model: str) -> StepPlan:
    steps = []
    for i, raw in enumerate(data.get("steps") or []):
        if raw.get("action") not in ACTIONS:  # partners without JSON mode can drift off the enum
            raw = {**raw, "action": _ACTION_ALIASES.get(str(raw.get("action", "")).lower(), "observe")}
        step = PlanStep(index=i, **{k: raw.get(k, PlanStep.model_fields[k].default) for k in _STEP_PROPS if k in raw})
        step.expected_after = _LEADING_TIME.sub("", step.expected_after).strip()
        for name in ("target", "target_description", "expected_after", "instruction"):
            setattr(step, name, _humanize(getattr(step, name)))
        step.t_start = max(0.0, min(float(step.t_start), duration_s or float(step.t_start)))
        step.t_end = max(step.t_start, min(float(step.t_end), duration_s or float(step.t_end)))
        steps.append(step)
    steps.sort(key=lambda s: (s.t_start, s.index))
    for i, s in enumerate(steps):
        s.index = i
    plan = StepPlan(
        kind=kind,  # type: ignore[arg-type]
        title=data.get("title", ""),
        app=data.get("app", ""),
        summary=data.get("summary", ""),
        start_state=data.get("start_state", ""),
        steps=steps,
        source_video=source_video,
        duration_s=duration_s,
        model=model,
    )
    if kind == "bug":
        plan.url = data.get("url", "")
        plan.expected = _untimed(data.get("expected", ""))
        plan.actual = _untimed(data.get("actual", ""))
        bt = data.get("bug_time")
        plan.bug_time = float(bt) if isinstance(bt, (int, float)) else None
        # The bug cannot be visible before the actions that cause it. A time earlier than the last
        # reproduction step (or past the end) is a misread; place it just after the last step instead.
        acting = [s for s in steps if s.action not in ("observe", "wait")]
        floor = acting[-1].t_start if acting else 0.0
        end = max(0.0, duration_s - 0.2) if duration_s else None
        if plan.bug_time is None or plan.bug_time < floor or (end is not None and plan.bug_time > end):
            guess = (acting[-1].t_end + 1.0) if acting else (plan.bug_time or 0.0)
            plan.bug_time = min(guess, end) if end is not None else guess
    return plan


_TIME_SPAN = re.compile(r"^\s*\(?\d{1,2}:\d{2}(?:\.\d+)?(?:\s*[-–to]+\s*\d{1,2}:\d{2}(?:\.\d+)?)?\)?\s*[:,-]?\s*")


def _untimed(text: str) -> str:
    """Drops a leading video timestamp ("00:23-00:32.3 Balances show...") the model sometimes adds."""
    return _TIME_SPAN.sub("", str(text or ""), count=1).strip()


# Phrases that describe the recording rather than the app ("32.3s video - ...", "The recording shows").
# A bug about a video player ("the video does not play") must still pass.
_ABOUT_THE_RECORDING = re.compile(
    r"\b\d+(?:\.\d+)?\s*s(?:ec(?:ond)?s?)?\s+(?:video|recording|screen ?cast)\b"
    r"|^\s*(?:the |this )?(?:screen )?(?:video|recording|screen ?cast)\b(?!\s+(?:does|doesn't|is|isn't|won't|fails|stops|plays))"
    r"|\baddress bar visible\b", re.I)


def describes_the_bug(data: object) -> bool:
    """A bug plan whose `actual` describes the recording itself ("32.3s video - Lisbon trip ...") instead
    of the wrong behaviour is rejected, so the call is asked again; the report shows that line."""
    if not isinstance(data, dict) or not data.get("steps"):
        return False
    actual = _untimed(data.get("actual", ""))
    return bool(actual) and not _ABOUT_THE_RECORDING.search(actual)


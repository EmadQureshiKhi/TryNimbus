"""Nimbus Ask: a question plus what is on screen becomes a short spoken answer and a walkthrough.

The model returns an ordered list of steps. Each step can point at a control, frame a region,
or pin a note (a line of working for a maths problem, say) to a spot on screen, and carries the
sentence Nimbus says while showing it. Pointed steps are then re-grounded on a zoomed crop so the
buddy lands on the control itself, not near it. If the person asked Nimbus to *do* something, the
answer carries an operate task for the computer-use agent instead.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from nimbus.engine.grounding import Grounder
from nimbus.mac.control import AppContext
from nimbus.mac.screen import Capture
from nimbus.meta.client import MetaClient, image_part, text_part, user

log = logging.getLogger(__name__)

MODEL_MAX_SIDE = 1800
REGION_MAX = 0.8  # a framed area is re-grounded only if it fits well inside the zoomed crop

INSTRUCTIONS = """You are Nimbus, a friendly expert who sits next to the user's cursor on their Mac and
can see their screen. You teach by showing: you point at the exact control you are talking about.
Write for the ear: short, warm, plain sentences, no markdown, never read coordinates or URLs aloud,
and never use em dashes (a plain hyphen or a comma instead).
In anything spoken (reply, say), write symbols as words a person would say ("x squared minus 5x",
"equals", "command shift T"), name a site rather than its URL, and spell out abbreviations; written
notes keep compact maths.

Return a walkthrough:
- reply: what you say first (1-2 sentences). If no steps are needed, this is the whole answer.
- steps: in order, one per thing to show. Each has `say` (one sentence spoken while it is shown) and
  `kind`:
  * "point": point at one control. Give `target` (its visible label or a short name),
    `target_description` (where it is and what it looks like) and x,y at its centre.
  * "box": frame a region (a paragraph, a code block, a chart, an equation). Give the box.
  * "note": pin a short written note to a spot, e.g. one line of working in a maths or code
    explanation: `note` is the text to write (keep math compact, like "2x = 8  →  x = 4", and use real
    symbols, never ASCII stand-ins: λ not l, x² not x^2, − × ÷ ≠ ≤ √ π, matrices as [[4−λ, 1], [2, 3−λ]],
    and no "Step N:" prefix, Nimbus numbers the cards itself). x,y must
    sit ON the exact visible thing the note explains (the term, number, operator or line of code),
    never on empty space; Nimbus draws the note card beside it and a leader line to that spot.
  * "say": speech only, nothing to show.
  Use several steps for multi-step answers ("go here, then here"). For a problem visible on screen
  (an equation, an error, a diagram), solve it step by step with "note" or "box" steps anchored on
  the relevant parts. Every note must show working that is actually correct for the cell or term it
  sits on. End a worked problem with a note that states the final answer, every part the user asked
  for (the value, the score, the alignment, the result), never only the last intermediate step.
  Keep it to at most 8 steps.
- Coordinates: integers on a 0-1000 grid of the image named by `image` (0 = first image), (0,0)
  top-left. Every coordinate must sit on something actually visible in that image. If the thing the
  user wants is not on screen, say where to find it and point at the menu or control that leads there.
- intent: "operate" only if the user asks you to do something for them (open an app, click
  through a flow, change a setting) AND acting is allowed; then put a precise task in operate_task.
  Otherwise "explain".
- show_steps (operate only): by default Nimbus just does the task, quietly: reply is one short
  sentence ("On it."), steps is empty and show_steps is false. Set show_steps true and list the plan
  as steps only when the user asks to see, learn or be walked through HOW it is done ("show me the
  steps", "walk me through it as you go", "I want to learn how"), or when the task is long and
  technical enough that watching each step helps (a multi-screen settings change, a developer
  workflow). Asking about the RESULT is not asking for the steps: "search X and tell me what the
  first result says" is quiet; the agent's final spoken answer covers the result. Put any such
  question into operate_task so the agent answers it when done. For "explain" answers, show_steps is
  false and steps work as above.
"""

ASK_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "intent": {"type": "string", "enum": ["explain", "operate"]},
        "operate_task": {"type": "string"},
        "show_steps": {"type": "boolean"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "say": {"type": "string"},
                    "kind": {"type": "string", "enum": ["point", "box", "note", "say"]},
                    "target": {"type": "string"},
                    "target_description": {"type": "string"},
                    "note": {"type": "string"},
                    "image": {"type": "integer"},
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "x_min": {"type": "integer"},
                    "y_min": {"type": "integer"},
                    "x_max": {"type": "integer"},
                    "y_max": {"type": "integer"},
                },
                "required": ["say", "kind", "target", "target_description", "note", "image", "x", "y",
                             "x_min", "y_min", "x_max", "y_max"],
            },
        },
    },
    "required": ["reply", "intent", "operate_task", "show_steps", "steps"],
}


@dataclass
class ShowStep:
    say: str
    kind: str
    target: str = ""
    note: str = ""
    gx: float | None = None  # global points
    gy: float | None = None
    box: tuple[float, float, float, float] | None = None  # global points
    refined: bool = False
    below: float | None = None  # note: global y where the text block under the anchor ends


@dataclass
class Walkthrough:
    question: str
    reply: str
    steps: list[ShowStep] = field(default_factory=list)
    intent: str = "explain"
    operate_task: str = ""
    show_steps: bool = False  # operate: narrate each action on screen (asked for, or a long technical task)
    model: str = ""


@dataclass
class Turn:
    question: str
    reply: str


def _encode(png: bytes, max_side: int) -> tuple[bytes, tuple[int, int]]:
    img = Image.open(io.BytesIO(png))
    img.load()
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=90)
    return out.getvalue(), img.size


class AskService:
    def __init__(self, client: MetaClient) -> None:
        self.client = client
        self.grounder = Grounder(client)
        self.history: list[Turn] = []

    async def ask(self, question: str, captures: list[Capture], app: AppContext, *, allow_operate: bool,
                  cursor: tuple[float, float] | None = None) -> Walkthrough:
        """Answer and, if enabled, sharpen every pointed step before returning."""
        walk, raw = await self.plan(question, captures, app, allow_operate=allow_operate, cursor=cursor)
        if self.client.settings.refine_pointing:
            await self.refine(walk, raw, captures)
        return walk

    async def plan(self, question: str, captures: list[Capture], app: AppContext, *, allow_operate: bool,
                   cursor: tuple[float, float] | None = None) -> tuple[Walkthrough, dict]:
        """First pass only: the spoken reply and coarse points. The app starts speaking on this while
        `refine` sharpens the points, so the zoom pass costs no perceived time."""
        if not captures:
            raise ValueError("no screen capture")
        parts = []
        context = [f"Frontmost app: {app.name or 'unknown'}" + (f" - window: {app.window_title}" if app.window_title else "")]
        if len(captures) > 1:
            context.append(f"{len(captures)} images, one per display; image 0 is the display with the cursor.")
        if cursor:
            c0 = captures[0]
            nx = (cursor[0] - c0.display.x) / c0.display.width * 1000
            ny = (cursor[1] - c0.display.y) / c0.display.height * 1000
            context.append(f"The user's mouse is at ({nx:.0f}, {ny:.0f}) on image 0.")
        context.append("Acting for the user is " + ("ALLOWED." if allow_operate else "NOT allowed (explain and point instead)."))
        if self.history:
            recent = "\n".join(f"User: {t.question}\nNimbus: {t.reply}" for t in self.history[-4:])
            context.append(f"Earlier in this conversation:\n{recent}")
        parts.append(text_part("\n".join(context) + f"\n\nUser's question: {question}"))
        for cap in captures:
            data, _ = _encode(cap.png, MODEL_MAX_SIDE)
            parts.append(image_part(data))

        data, reply = await self.client.respond_json(
            self.client.settings.models.ask, [user(*parts)], schema=ASK_SCHEMA, schema_name="walkthrough",
            what="ask", instructions=INSTRUCTIONS, reasoning_effort=effort_for(question, self.client.settings),
            max_output_tokens=6000,
        )
        walk = self._to_walkthrough(question, data, captures, reply.model)
        self.history.append(Turn(question, walk.reply))
        self.history = self.history[-12:]
        return walk, data

    def _to_walkthrough(self, question: str, data: dict, captures: list[Capture], model: str) -> Walkthrough:
        walk = Walkthrough(question=question, reply=_plain(data.get("reply", "")), model=model,
                           intent=str(data.get("intent", "explain")), operate_task=str(data.get("operate_task", "")),
                           show_steps=bool(data.get("show_steps", False)))
        for raw in data.get("steps") or []:
            kind = raw.get("kind", "say")
            cap = captures[max(0, min(int(raw.get("image") or 0), len(captures) - 1))]
            step = ShowStep(say=_plain(raw.get("say", "")), kind=kind, target=str(raw.get("target", "")),
                            note=_plain(raw.get("note", "")))
            if kind in ("point", "note", "box"):
                x, y = raw.get("x"), raw.get("y")
                if kind == "box" and _valid_box(raw):
                    x0, y0 = cap.norm_to_global(raw["x_min"], raw["y_min"])
                    x1, y1 = cap.norm_to_global(raw["x_max"], raw["y_max"])
                    step.box = (x0, y0, x1, y1)
                    step.gx, step.gy = (x0 + x1) / 2, (y0 + y1) / 2
                elif isinstance(x, (int, float)) and isinstance(y, (int, float)):
                    step.gx, step.gy = cap.norm_to_global(x, y)
                    if kind == "point" and _valid_box(raw):
                        x0, y0 = cap.norm_to_global(raw["x_min"], raw["y_min"])
                        x1, y1 = cap.norm_to_global(raw["x_max"], raw["y_max"])
                        step.box = (x0, y0, x1, y1)
                if step.gx is None:
                    step.kind = "say"
                elif kind == "note":
                    step.below = block_end(cap, step.gx, step.gy)
            walk.steps.append(step)
        return walk

    async def refine(self, walk: Walkthrough, data: dict, captures: list[Capture]) -> None:
        raw_steps = data.get("steps") or []

        async def one(i: int, step: ShowStep) -> None:
            # A framed area often comes back unnamed; what Nimbus says about it names it well enough.
            target = step.target or (step.say if step.kind == "box" else "")
            if step.kind not in ("point", "box") or step.gx is None or not target:
                return
            raw = raw_steps[i] if i < len(raw_steps) else {}
            cap = captures[max(0, min(int(raw.get("image") or 0), len(captures) - 1))]
            region = step.kind == "box"
            if region and not _fits_crop(step.box, cap):
                return  # a whole paragraph or chart: the full view frames those well
            px, py = cap.global_to_pixel(step.gx, step.gy)
            # The first pass's box keeps the zoomed answer honest: without it a refine was free to
            # drift 20-27 pt off a control the first pass had boxed correctly (measured on a soft
            # 1x screenshot of GitHub, where the crop's small text misled the zoomed look).
            box = None
            if step.box:
                bx0, by0 = cap.global_to_pixel(step.box[0], step.box[1])
                bx1, by1 = cap.global_to_pixel(step.box[2], step.box[3])
                box = (bx0, by0, bx1, by1)
            context = "This is an area to frame (an equation, a line, a block): the box must bound all of it." if region else ""
            located = await self.grounder.refine(cap.png, px, py, target, box=box, region=region, context=context,
                                                 description=str(raw.get("target_description", "")))
            if located.refined:
                step.gx, step.gy = cap.pixel_to_global(located.x, located.y)
                if located.box:
                    a = cap.pixel_to_global(located.box[0], located.box[1])
                    b = cap.pixel_to_global(located.box[2], located.box[3])
                    step.box = (a[0], a[1], b[0], b[1])
                step.refined = True

        await asyncio.gather(*(one(i, s) for i, s in enumerate(walk.steps)))


_WORKING = re.compile(r"\b(solve|calculat\w*|comput\w*|work (it |this |that )?out|derive|prove|simplify|evaluate|"
                      r"step by step|fill (it |this )?(in|out)|finish (this|the|it)|how (much|many) is)\b", re.I)


def needs_working(question: str) -> bool:
    """A question that asks Nimbus to work a problem out, not to find or explain something on screen."""
    return bool(_WORKING.search(question))


def effort_for(question: str, settings) -> str:
    """Pointing stays on the fast effort; working a problem out gets more thought, because at minimal
    effort Muse Spark got a dynamic-programming table's values right but wrote wrong working for one."""
    return getattr(settings, "solve_effort", settings.ask_effort) if needs_working(question) else settings.ask_effort


def _plain(text) -> str:
    """House style for what Nimbus shows: no em dashes (the model writes them despite being asked not to)."""
    return str(text or "").replace(" \u2014 ", " - ").replace("\u2014", " - ").strip()


def _fits_crop(box: tuple[float, float, float, float] | None, cap: Capture) -> bool:
    from nimbus.engine.grounding import REFINE_HALF_H, REFINE_HALF_W

    if box is None:
        return False
    w, h = cap.display.width, cap.display.height
    return box[2] - box[0] <= 2 * REFINE_HALF_W * w * REGION_MAX and box[3] - box[1] <= 2 * REFINE_HALF_H * h * REGION_MAX


def block_end(cap: Capture, gx: float, gy: float, *, width: float = 300, gap: float = 12,
              reach: float = 320) -> float | None:
    """Where the text block under a note's anchor ends (global y): the first blank band at least `gap`
    points tall below the anchor, across the width a note card takes. The card goes there, so it never
    covers the rest of the paragraph or footnote the anchor sits in (seen: a card over a footnote's
    formula, the very evidence it explained). None if the screen is busy all the way down."""
    try:
        img = Image.open(io.BytesIO(cap.png)).convert("L")
    except Exception:
        return None
    s = cap.display.scale or 1.0
    px, py = cap.global_to_pixel(gx, gy)
    x0, x1 = int(max(0, px - 30 * s)), int(min(img.width, px + width * s))
    y0, y1 = int(max(0, py)), int(min(img.height, py + reach * s))
    if x1 - x0 < 10 or y1 - y0 < 10:
        return None
    rows = np.asarray(img.crop((x0, y0, x1, y1)), dtype=np.int16)
    blank = (rows.max(axis=1) - rows.min(axis=1)) < 24
    need, run = max(2, int(gap * s)), 0
    for i, b in enumerate(blank):
        run = run + 1 if b else 0
        if run >= need:
            return cap.pixel_to_global(px, y0 + i - run + 1)[1]
    return None


def _valid_box(raw: dict) -> bool:
    try:
        return 0 <= raw["x_min"] < raw["x_max"] <= 1001 and 0 <= raw["y_min"] < raw["y_max"] <= 1001
    except (KeyError, TypeError):
        return False

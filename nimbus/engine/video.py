"""Screen recording -> step plan with measured evidence.

1. Muse Spark watches the whole recording (pictures and narration in one call) and writes the
   step plan. Narrated video goes to Spark 1.2 first because the docs flag 1.3 audio as partial.
2. SAM 3.1 tracks the mouse cursor through a low-fps proxy of the video. Clicks happen where the
   cursor dwells, so each step gets the exact pixel the presenter clicked.
3. SAM segments the control under that pixel in the step's frame, giving a crop of what the button
   looked like. If cursor tracking fails, Muse Spark grounding finds the control by description.
"""
from __future__ import annotations

import asyncio
import io
import logging
import math
from collections.abc import Callable
from pathlib import Path

from PIL import Image

from nimbus import media
from nimbus.engine.grounding import Grounder
from nimbus.engine.steps import BUG_SCHEMA, PLAN_SCHEMA, PlanStep, StepPlan, describes_the_bug, plan_from_model
from nimbus.meta.client import MetaClient, image_part, text_part, user
from nimbus.meta.resilience import MetaAPIError
from nimbus.meta.sam import Sam, SegmentResult, Track

log = logging.getLogger(__name__)

PROXY_FPS = 4.0
Progress = Callable[[str, float], None]

TUTORIAL_PROMPT = """You are turning a screen-recorded software tutorial into a precise, replayable step plan.
Watch the whole video and listen to the narration. Produce one step per distinct user action
(click, typing, shortcut, menu choice, drag, opening an app or URL). Do not merge separate clicks.
Skip pure talk with no action unless it is a necessary 'observe' checkpoint.
For each step give accurate start/end timestamps in seconds, the control acted on, a description
precise enough to find that control on a different screen (location, icon, label text, colour),
the exact text typed or keys pressed, and the visible result. Total video length: {duration:.1f}s."""

BUG_PROMPT = """You are turning a screen recording of a software bug into a precise reproduction.
The person may narrate what they expected. Produce the minimal ordered steps that reproduce the bug
from a fresh page load (navigate, click, type, select, ...), with timestamps, the exact controls and
the exact values typed. Then state the expected behaviour, the actual (buggy) behaviour, the URL shown
in the address bar if visible, and the time where the bug is visible. Total video length: {duration:.1f}s."""


REVIEW_PROMPT = """These are the steps of a tutorial plan ("{title}", app: {app}) written by a model that
watched the whole video, each followed by the video frame just before and just after that step's click.
Correct the plan so a learner can follow it on their own screen:
- keep=false for anything that is not its own user action (hovering, looking, "validate", "confirm",
  a duplicate of the neighbouring step). Each kept step is exactly one click, typing, key press,
  drag or menu choice the learner performs.
- target: the control's visible label exactly as the frames show it (e.g. "Muse Spark 1.3"), or a
  short plain name for an unlabeled icon ("Send button"). A text field is named by its placeholder
  without trailing dots ("Reply to Chatbook"). Trust the frames over the old text.
- instruction: one imperative sentence of at most 12 words, in plain words a person would say.
  Name the control by its label. For typing steps include the exact text: Type "...". No colours,
  pixel positions, symbols like + or arrows, or parentheses.
- text: for typing steps the exact text typed (read it from the after frame); otherwise empty.
- expected_after: at most 12 words describing the visible change.
Also write:
- title: what the tutorial achieves, starting with a verb, at most 7 plain words (e.g. "Draft a
  launch tweet with Muse Spark").
- start_state: what must be on screen before step one, as a short clause that follows "make sure",
  at most 12 words (e.g. "Chatbook is open in your browser"). No quotes, symbols or descriptions of
  colours and layout."""

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {"steps": {"type": "array", "items": {"type": "object", "properties": {
        "index": {"type": "integer"}, "keep": {"type": "boolean"}, "target": {"type": "string"},
        "instruction": {"type": "string"}, "text": {"type": "string"}, "expected_after": {"type": "string"},
    }, "required": ["index", "keep", "target", "instruction", "text", "expected_after"]}},
        "title": {"type": "string"}, "start_state": {"type": "string"}},
    "required": ["steps", "title", "start_state"],
}


def apply_review(plan: StepPlan, data: dict) -> None:
    by_index = {r["index"]: r for r in data.get("steps", []) if isinstance(r, dict) and "index" in r}
    kept = []
    for step in plan.steps:
        r = by_index.get(step.index)
        if r is None:
            kept.append(step)
            continue
        if not r.get("keep", True):
            continue
        for name in ("target", "instruction", "expected_after"):
            value = str(r.get(name) or "").strip()
            if value:
                setattr(step, name, value)
        if step.action == "type" and str(r.get("text") or "").strip():
            step.text = str(r["text"]).strip()
        if step.action == "type" and step.text and f'"{step.text}' not in step.instruction:
            step.instruction = step.instruction.replace(step.text, f'"{step.text}"', 1)  # what to type stands out
        kept.append(step)
    for name in ("title", "start_state"):
        value = str(data.get(name) or "").strip().rstrip(".")
        if value:
            setattr(plan, name, value)
    if kept:
        for i, step in enumerate(kept):
            step.index = i
        plan.steps = kept


class VideoAnalyzer:
    def __init__(self, client: MetaClient) -> None:
        self.client = client
        self.sam = Sam(client)
        self.grounder = Grounder(client)

    async def analyze(self, video: Path, workdir: Path, *, kind: str = "tutorial",
                      progress: Progress | None = None, extra_context: str = "") -> StepPlan:
        report = progress or (lambda msg, frac: None)
        workdir.mkdir(parents=True, exist_ok=True)
        report("Preparing video", 0.03)
        mp4 = await asyncio.to_thread(media.normalize, video, workdir)
        info = await asyncio.to_thread(media.probe, mp4)
        if info.duration <= 0:
            raise media.MediaError("video has no duration")

        report("Watching the video with Muse Spark", 0.08)
        plan_task = asyncio.ensure_future(self._plan(mp4, info, kind, extra_context))
        cursor_task = asyncio.ensure_future(self._cursor_track(mp4, workdir, info))
        try:
            plan = await plan_task
        except BaseException:
            # Without a plan the SAM track is useless; stop paying for it and do not leave an
            # orphaned task whose exception is never retrieved.
            cursor_task.cancel()
            raise
        report(f"Found {len(plan.steps)} steps; locating each click", 0.55)
        try:
            cursor = await cursor_task
        except Exception as exc:  # SAM trouble must not sink the plan; grounding covers it
            log.warning("cursor tracking failed: %s", exc)
            cursor = None

        sem = asyncio.Semaphore(4)
        done = 0

        async def enrich(step: PlanStep) -> None:
            nonlocal done
            async with sem:
                try:
                    await self._evidence(step, mp4, workdir, info, cursor)
                except Exception as exc:
                    log.warning("step %d evidence failed: %s", step.index, exc)
            done += 1
            report(f"Located step {done}/{len(plan.steps)}", 0.55 + 0.4 * done / max(1, len(plan.steps)))

        await asyncio.gather(*(enrich(s) for s in plan.steps))
        if kind == "tutorial":
            report("Checking each step against the video", 0.96)
            try:
                await self._review(plan, workdir)
            except Exception as exc:  # the unreviewed plan is still usable
                log.warning("plan review failed: %s", exc)
        plan.source_video = str(mp4)
        plan.save(workdir)
        report("Ready", 1.0)
        return plan

    # -- 1. plan

    async def _plan(self, mp4: Path, info: media.VideoInfo, kind: str, extra: str) -> StepPlan:
        prompt = (BUG_PROMPT if kind == "bug" else TUTORIAL_PROMPT).format(duration=info.duration)
        if extra:
            prompt += "\n" + extra
        schema = BUG_SCHEMA if kind == "bug" else PLAN_SCHEMA
        models = list(self.client.settings.models.video)
        if not info.has_audio:  # silent recordings do not need the audio-capable model first
            models = sorted(models, key=lambda m: 0 if m.endswith("1.3") else 1)

        async def build(file_id: str):
            return await self.client.respond_json(
                models, [user(text_part(prompt), {"type": "input_file", "file_id": file_id})],
                schema=schema, schema_name="step_plan", what="video plan",
                reasoning_effort="medium", max_output_tokens=20000, stream=True,
                validate=describes_the_bug if kind == "bug" else (lambda d: isinstance(d, dict) and bool(d.get("steps"))),
            )

        data, reply = await self.client.respond_with_file(mp4, build)
        plan = plan_from_model(data, kind=kind, source_video=str(mp4), duration_s=info.duration, model=reply.model)
        if not plan.steps:
            raise MetaAPIError("the model found no steps in this video")
        return plan

    # -- 1b. review

    async def _review(self, plan: StepPlan, workdir: Path) -> None:
        """Muse Spark re-reads every step next to the frames measured for it (just before and just
        after the click) and corrects the plan: steps that are not a separate action are dropped,
        labels are taken from what is visible, and each instruction becomes one short sentence a
        person can follow. Watching a whole video, the model can mislabel or invent a step; a still
        frame of the moment settles it."""
        parts = [text_part(REVIEW_PROMPT.format(title=plan.title, app=plan.app))]
        for step in plan.steps:
            parts.append(text_part(
                f"Step {step.index}: action={step.action}; target={step.target!r}; text={step.text!r}; "
                f"instruction={step.instruction!r}; expected={step.expected_after!r}; "
                f"narration={getattr(step, 'narration', '')!r}"))
            for label, name in (("before", step.evidence.before_frame), ("after", step.evidence.after_frame)):
                if name and (workdir / name).exists():
                    parts += [text_part(f"Step {step.index} {label}:"),
                              image_part((workdir / name).read_bytes(), detail_max=900)]
        indices = {s.index for s in plan.steps}

        def valid(d) -> bool:
            rows = d.get("steps") if isinstance(d, dict) else None
            return bool(rows) and all(isinstance(r, dict) and r.get("index") in indices for r in rows) \
                and any(r.get("keep") for r in rows)

        data, _ = await self.client.respond_json(
            self.client.settings.models.verify, [user(*parts)], schema=REVIEW_SCHEMA, schema_name="plan_review",
            what="plan review", reasoning_effort="low", max_output_tokens=4000, validate=valid,
        )
        apply_review(plan, data)

    # -- 2. cursor

    async def _cursor_track(self, mp4: Path, workdir: Path, info: media.VideoInfo) -> "CursorPath | None":
        prox = await asyncio.to_thread(media.proxy, mp4, workdir, fps=PROXY_FPS)
        proxy_info = await asyncio.to_thread(media.probe, prox)
        fps = PROXY_FPS if prox != mp4 else info.fps
        result: SegmentResult = await self.sam.track_video(prox, "mouse cursor")
        track = _best_cursor_track(result, proxy_info.width * proxy_info.height)
        if track is None:
            log.info("SAM found no cursor-like track")
            return None
        sx, sy = info.width / proxy_info.width, info.height / proxy_info.height
        return CursorPath(track, fps, sx, sy)

    # -- 3. per-step evidence

    async def _evidence(self, step: PlanStep, mp4: Path, workdir: Path, info: media.VideoInfo,
                        cursor: "CursorPath | None") -> None:
        ev = step.evidence
        ev.frame_size = (info.width, info.height)
        rest = cursor.dwell(step.t_start, step.t_end) if cursor else None
        if rest is not None:
            click_t, left_t = rest  # the cursor arrives on the control, clicks there, then moves on
            before_t = max(0.0, click_t - 0.15)
            after_t = max(click_t + 0.8, left_t - 0.05)
        else:
            click_t = step.t_start + 0.6 * max(0.0, step.t_end - step.t_start)
            before_t = click_t
            after_t = max(step.t_end + 0.6, click_t + 0.8)
        after_t = min(after_t, max(0.0, info.duration - 0.05))
        ev.click_time = round(click_t, 2)
        before = await asyncio.to_thread(media.frame_at, mp4, before_t)
        after = await asyncio.to_thread(media.frame_at, mp4, after_t)
        stem = f"step{step.index:02d}"
        (workdir / f"{stem}_before.png").write_bytes(before)
        (workdir / f"{stem}_after.png").write_bytes(after)
        ev.before_frame, ev.after_frame = f"{stem}_before.png", f"{stem}_after.png"
        if not step.needs_target and not step.target:
            return

        point = cursor.position(click_t) if cursor else None
        if point is not None:
            ev.click_px, ev.cursor_source = point, "sam"
        elif step.target:
            hit = await self.grounder.locate(before, step.target, description=step.target_description)
            if hit:
                ev.click_px, ev.cursor_source = (int(hit.x), int(hit.y)), "grounding"
                if hit.box:
                    ev.target_box_px = tuple(int(v) for v in hit.box)  # type: ignore[assignment]
        if ev.click_px is None:
            return
        if ev.target_box_px is None:
            cursor_box = cursor.box(click_t) if cursor and ev.cursor_source == "sam" else None
            ev.target_box_px = await self._control_box(before, ev.click_px, (info.width, info.height), cursor_box)
        crop = media.crop_png(before, ev.target_box_px, pad=14, min_size=THUMB_MIN)  # enough to recognise it
        (workdir / f"{stem}_target.png").write_bytes(crop)
        ev.target_crop = f"{stem}_target.png"

    async def _control_box(self, frame: bytes, point: tuple[int, int], size: tuple[int, int],
                           cursor_box: tuple[float, float, float, float] | None = None) -> tuple[int, int, int, int]:
        """The control under the click: the smallest segment containing it that is not the cursor glyph."""
        w, h = size
        fallback = (max(0, point[0] - 120), max(0, point[1] - 50), min(w, point[0] + 120), min(h, point[1] + 50))
        try:
            seg = await self.sam.segment_image_at(frame, size, point=point)
        except Exception as exc:
            log.info("SAM control segmentation failed (%s); using a fixed crop", exc)
            return fallback
        best = None
        for track in seg.objects:
            for box in track.boxes.values():
                inside = box.left <= point[0] <= box.right and box.top <= point[1] <= box.bottom
                if (inside and box.area < 0.2 * w * h and not _mostly_inside(box, cursor_box)
                        and (best is None or box.area < best.area)):
                    best = box
        if best is None or best.area < 64:
            return fallback
        return int(best.left), int(best.top), int(best.right), int(best.bottom)


def _mostly_inside(box, cursor_box: tuple[float, float, float, float] | None) -> bool:
    """True when a segment is (mostly) the cursor glyph itself rather than the control under it."""
    if cursor_box is None or box.area <= 0:
        return False
    x0, y0, x1, y1 = cursor_box
    pad = 0.5 * max(x1 - x0, y1 - y0)  # the proxy video's track is coarser than the full frame
    ix = max(0.0, min(box.right, x1 + pad) - max(box.left, x0 - pad))
    iy = max(0.0, min(box.bottom, y1 + pad) - max(box.top, y0 - pad))
    return ix * iy >= 0.8 * box.area


THUMB_MIN = (240, 84)  # step-card thumbnail: a short label still shows with its surroundings
STILL_PX = 2.0  # cursor travel per proxy frame (proxy pixels) that still counts as resting


class CursorPath:
    """Cursor positions over time, from a SAM track on the proxy video."""

    def __init__(self, track: Track, fps: float, sx: float, sy: float) -> None:
        self.fps, self.sx, self.sy = fps, sx, sy
        self.points: dict[int, tuple[float, float]] = {}
        self.boxes: dict[int, tuple[float, float, float, float]] = {}
        for frame, box in track.boxes.items():
            w, h = box.right - box.left, box.bottom - box.top
            self.boxes[frame] = (box.left * sx, box.top * sy, box.right * sx, box.bottom * sy)
            # The hotspot of the macOS arrow and pointing hand sits near the top-left of the glyph.
            self.points[frame] = ((box.left + 0.15 * w) * sx, (box.top + 0.1 * h) * sy)

    def position(self, t: float) -> tuple[int, int] | None:
        frame = round(t * self.fps)
        for delta in range(0, 4):
            for f in (frame - delta, frame + delta):
                if f in self.points:
                    x, y = self.points[f]
                    return int(x), int(y)
        return None

    def box(self, t: float) -> tuple[float, float, float, float] | None:
        """The cursor glyph's box (full-resolution pixels) nearest to time `t`."""
        frame = round(t * self.fps)
        for delta in range(0, 4):
            for f in (frame - delta, frame + delta):
                if f in self.boxes:
                    return self.boxes[f]
        return None

    def dwell(self, t0: float, t1: float) -> tuple[float, float] | None:
        """When the cursor came to rest on this step's control, and when it moved on, in seconds.

        A person moves to the control, pauses, clicks and rests, all at one spot: one still stretch.
        Its start is before the click (the screen as it was), its end is after the click has taken
        effect and before the next move. The window is widened a little because the model's step
        times are approximate; the longest still stretch that follows a move is the step's.
        """
        lo, hi = (t0 - 1.0) * self.fps, (t1 + 0.5) * self.fps
        frames = sorted(f for f in self.points if lo <= f <= hi)
        if len(frames) < 2:
            return None
        runs: list[tuple[int, int]] = []  # still stretches as (first frame, last frame)
        start = None
        for a, b in zip(frames, frames[1:]):
            (x0, y0), (x1, y1) = self.points[a], self.points[b]
            still = math.hypot(x1 - x0, y1 - y0) / max(1, b - a) <= STILL_PX * max(self.sx, self.sy)
            if still and start is None:
                start = a
            elif not still and start is not None:
                runs.append((start, a))
                start = None
        if start is not None:
            runs.append((start, frames[-1]))
        after_a_move = [r for r in runs if r[0] > frames[0]] or runs
        if not after_a_move:
            return None
        first, last = max(after_a_move, key=lambda r: (r[1] - r[0], -r[0]))
        return first / self.fps, last / self.fps


def _best_cursor_track(result: SegmentResult, frame_area: float) -> Track | None:
    """The cursor is small and present for many frames; prefer the longest small-box track."""
    best, best_score = None, 0.0
    for track in result.objects:
        if not track.boxes:
            continue
        areas = sorted(b.area for b in track.boxes.values())
        median = areas[len(areas) // 2]
        if median > 0.01 * frame_area:  # bigger than 1% of the frame is not a cursor
            continue
        score = len(track.boxes) / (1 + median / 400)
        if score > best_score:
            best, best_score = track, score
    return best


def png_dims(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as img:
        return img.size

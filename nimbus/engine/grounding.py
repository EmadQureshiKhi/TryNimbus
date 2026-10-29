"""Find a control in a screenshot and return exact pixel coordinates.

Two passes, because a full-screen screenshot downscaled for the model loses small controls:

1. Coarse: the whole screenshot (long side capped) on Muse Spark's 0-1000 grounding grid.
2. Refine: a full-resolution crop around the coarse hit, asked again. The crop is small, so a
   16 px icon becomes a 60 px icon and the model can centre on it.

Optionally a reference crop (the same control as it looked in a tutorial video) is sent with the
screenshot, so the model matches what the control looks like, not only what it is called. That is
what lets Shadow find a button that moved between app versions.
"""
from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass

from PIL import Image

from nimbus.meta.client import MetaClient, image_part, text_part, user
from nimbus.meta.resilience import MetaAPIError

log = logging.getLogger(__name__)

COARSE_MAX_SIDE = 1600
REFINE_HALF_W = 0.16  # refine window: 32% of the image width
REFINE_HALF_H = 0.18

LOCATE_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "x": {"type": "integer", "description": "0-1000, left to right"},
        "y": {"type": "integer", "description": "0-1000, top to bottom"},
        "x_min": {"type": "integer"},
        "y_min": {"type": "integer"},
        "x_max": {"type": "integer"},
        "y_max": {"type": "integer"},
        "confidence": {"type": "number", "description": "0 to 1"},
        "note": {"type": "string", "description": "what you matched, or why it is not visible"},
    },
    "required": ["found", "x", "y", "x_min", "y_min", "x_max", "y_max", "confidence", "note"],
}


@dataclass
class Located:
    x: float  # pixels in the input image
    y: float
    box: tuple[float, float, float, float] | None
    confidence: float
    note: str = ""
    refined: bool = False


def _clamp01k(v: float) -> float:
    return max(0.0, min(1000.0, float(v)))


def _encode(img: Image.Image, max_side: int | None = None, quality: int = 90) -> bytes:
    if max_side and max(img.size) > max_side:
        img = img.copy()
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    if img.mode != "RGB":
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality)
    return out.getvalue()


def _prompt(target: str, description: str, has_reference: bool, context: str) -> str:
    lines = [
        f'Point to this control in the screenshot: "{target}".',
    ]
    if description:
        lines.append(f"Details: {description}")
    if context:
        lines.append(f"Context: {context}")
    if has_reference:
        lines.append("The second image shows the same control as it looked elsewhere (it may differ in theme, "
                     "size or position). Match it by meaning and appearance.")
    lines.append(
        'Answer in the format {"found": bool, "x": int, "y": int, "x_min": int, "y_min": int, "x_max": int, '
        '"y_max": int, "confidence": number, "note": string}. x,y is the centre of the control (where a click '
        "would land); the box tightly bounds it. Coordinates are on a 0-1000 grid of the FIRST image, (0,0) top-left. "
        "If it is not visible, set found=false."
    )
    return "\n".join(lines)


class Grounder:
    def __init__(self, client: MetaClient) -> None:
        self.client = client

    async def _ask(self, img_bytes: bytes, prompt: str, reference: bytes | None) -> dict:
        parts = [text_part(prompt), image_part(img_bytes)]
        if reference:
            parts.append(image_part(reference))
        data, _ = await self.client.respond_json(
            self.client.settings.models.grounding, [user(*parts)],
            schema=LOCATE_SCHEMA, schema_name="locate", what="grounding", reasoning_effort="low",
        )
        if isinstance(data, list):
            data = data[0] if data else {"found": False}
        return data

    async def locate(self, image: bytes, target: str, *, description: str = "", reference: bytes | None = None,
                     context: str = "", refine: bool = True) -> Located | None:
        img = Image.open(io.BytesIO(image))
        img.load()
        w, h = img.size
        coarse = await self._ask(_encode(img, COARSE_MAX_SIDE), _prompt(target, description, reference is not None, context),
                                 reference)
        if not coarse.get("found"):
            log.info("grounding: %r not found (%s)", target, coarse.get("note"))
            return None
        cx, cy = _clamp01k(coarse["x"]) * w / 1000, _clamp01k(coarse["y"]) * h / 1000
        box = _box(coarse, w, h)
        result = Located(cx, cy, box, float(coarse.get("confidence") or 0.5), coarse.get("note", ""))
        if not refine:
            return result
        try:
            refined = await self._refine(img, result, target, description, reference, context)
            return refined or result
        except MetaAPIError as exc:
            log.warning("grounding refine failed (%s); keeping the coarse point", exc)
            return result

    async def _refine(self, img: Image.Image, coarse: Located, target: str, description: str,
                      reference: bytes | None, context: str, region: bool = False) -> Located | None:
        w, h = img.size
        half_w, half_h = max(160, int(w * REFINE_HALF_W)), max(120, int(h * REFINE_HALF_H))
        # Always centred on the coarse hit, padded past the screen edge if need be: pointing drifts
        # when the control sits at the edge of the crop (measured: 5-17 pt upward near the bottom).
        left, top = int(coarse.x - half_w), int(coarse.y - half_h)
        crop = img.convert("RGB").crop((left, top, left + 2 * half_w, top + 2 * half_h))
        cw, ch = crop.size
        data = await self._ask(_encode(crop, 1400), _prompt(target, description, reference is not None,
                                                            (context + " This image is a zoomed-in crop.").strip()),
                               reference)
        if not data.get("found"):
            return None
        rx, ry = left + _clamp01k(data["x"]) * cw / 1000, top + _clamp01k(data["y"]) * ch / 1000
        # A refine that jumps far from the coarse hit matched something else in the crop.
        if abs(rx - coarse.x) > half_w * 0.9 or abs(ry - coarse.y) > half_h * 0.9:
            return None
        if region:
            # A framed area: the full view gets its size right but was measured 45 pt off an equation
            # it named correctly, so the zoomed box wins as long as it overlaps the coarse one.
            rbox = _box(data, cw, ch)
            if rbox is None or coarse.box is None:
                return None
            box = (rbox[0] + left, rbox[1] + top, rbox[2] + left, rbox[3] + top)
            if not _overlaps(box, coarse.box):
                log.info("grounding: refined region for %r does not touch the coarse box; keeping it", target)
                return None
            return Located((box[0] + box[2]) / 2, (box[1] + box[3]) / 2, box, coarse.confidence,
                           data.get("note", ""), refined=True)
        if coarse.box and not _near(coarse.box, rx, ry):
            # The full view's box was right in every measurement; a zoomed answer outside it is the
            # crop being misread (it drifted 5-25 px off small controls), so keep the coarse hit.
            log.info("grounding: refine left the coarse box for %r; keeping the coarse hit", target)
            return None
        rbox = _box(data, cw, ch)
        box = (rbox[0] + left, rbox[1] + top, rbox[2] + left, rbox[3] + top) if rbox else coarse.box
        return Located(rx, ry, box, max(coarse.confidence, float(data.get("confidence") or 0.5)),
                       data.get("note", ""), refined=True)

    async def refine(self, image: bytes, x: float, y: float, target: str, *, description: str = "",
                     box: tuple[float, float, float, float] | None = None, reference: bytes | None = None,
                     context: str = "", region: bool = False) -> Located:
        """Sharpen a point another call already produced (pixels in `image`); keeps it if refining fails.
        `region`: the target is an area (an equation, a code block) framed by `box`, not a control."""
        img = Image.open(io.BytesIO(image))
        img.load()
        coarse = Located(x, y, box, 0.6)
        try:
            return await self._refine(img, coarse, target, description, reference, context, region) or coarse
        except MetaAPIError as exc:
            log.warning("refine failed for %r (%s); keeping the first point", target, exc)
            return coarse

    async def locate_many(self, image: bytes, targets: list[tuple[str, str]], **kwargs) -> list[Located | None]:
        return list(await asyncio.gather(*(self.locate(image, t, description=d, **kwargs) for t, d in targets),
                                         return_exceptions=False))


def _box(data: dict, w: int, h: int) -> tuple[float, float, float, float] | None:
    try:
        x0, y0, x1, y1 = (_clamp01k(data[k]) for k in ("x_min", "y_min", "x_max", "y_max"))
    except (KeyError, TypeError, ValueError):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    return x0 * w / 1000, y0 * h / 1000, x1 * w / 1000, y1 * h / 1000


def _overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _near(box: tuple[float, float, float, float], x: float, y: float) -> bool:
    """`(x, y)` lies in `box` grown by a quarter of its size (at least 8 px) on every side."""
    x0, y0, x1, y1 = box
    mx, my = max(8.0, 0.25 * (x1 - x0)), max(8.0, 0.25 * (y1 - y0))
    return x0 - mx <= x <= x1 + mx and y0 - my <= y <= y1 + my

"""SAM 3.1 through the Meta Model API: segment and track objects in images and videos.

Requests stream over the Responses API and the `meta_sam_parser` package decodes the
segmentation grammar (boxes and masks per frame). Prompts are either a short noun phrase
("mouse cursor") or object prompts built with `build_object_prompt` (a point or box on a frame).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from meta_sam_parser import (
    FramePrompt,
    PromptBox,
    PromptObject,
    PromptPoint,
    build_object_prompt,
    image_segmentation_format,
    parse_responses_stream,
    video_segmentation_format,
)

from nimbus.meta.client import MetaClient, image_part, text_part, user
from nimbus.meta.resilience import MetaAPIError, RetryPolicy, with_retries

log = logging.getLogger(__name__)


@dataclass
class Box:
    left: float
    top: float
    right: float
    bottom: float
    confidence: float | None = None

    @property
    def center(self) -> tuple[float, float]:
        return (self.left + self.right) / 2, (self.top + self.bottom) / 2

    @property
    def area(self) -> float:
        return max(0.0, self.right - self.left) * max(0.0, self.bottom - self.top)


@dataclass
class Track:
    object_id: str
    boxes: dict[int, Box] = field(default_factory=dict)  # frame index -> box

    def box_near(self, frame: int, window: int = 3) -> Box | None:
        if frame in self.boxes:
            return self.boxes[frame]
        for delta in range(1, window + 1):
            for f in (frame - delta, frame + delta):
                if f in self.boxes:
                    return self.boxes[f]
        return None


@dataclass
class SegmentResult:
    width: int
    height: int
    tracks: dict[str, Track]

    @property
    def objects(self) -> list[Track]:
        return list(self.tracks.values())


async def _events(stream: Any) -> AsyncIterator[dict[str, Any]]:
    """Plain dict events for the parser; synthesize `output_text.done` from `content_part.done`
    (the API streams the latter only), exactly as Meta's SAM cookbook does."""
    done: set[tuple[Any, Any, Any]] = set()
    async for event in stream:
        data = event.model_dump() if hasattr(event, "model_dump") else dict(event)
        lane = (data.get("item_id"), data.get("output_index"), data.get("content_index"))
        if data.get("type") == "response.output_text.done":
            if lane not in done:
                done.add(lane)
                yield data
            continue
        yield data
        part = data.get("part")
        if (data.get("type") == "response.content_part.done" and lane not in done
                and isinstance(part, dict) and part.get("type") == "output_text"
                and isinstance(part.get("text"), str)):
            done.add(lane)
            yield {"type": "response.output_text.done", "item_id": data.get("item_id"),
                   "output_index": data.get("output_index"), "content_index": data.get("content_index"),
                   "text": part["text"]}


def _collect(result: Any) -> SegmentResult:
    if result.outcome.status != "completed":
        raise MetaAPIError(f"SAM segmentation incomplete: {getattr(result.outcome, 'reason', '?')}",
                           retryable=True)
    tracks: dict[str, Track] = defaultdict(lambda: Track(object_id=""))
    width = height = 0
    media = getattr(result, "media", None)
    if media is not None:
        width = int(getattr(media, "width", 0) or 0)
        height = int(getattr(media, "height", 0) or 0)
    for record in result.records:
        if getattr(record, "kind", "") != "box":
            continue
        frame = record.frame.frame_index if record.frame is not None else 0
        track = tracks[record.object_id]
        track.object_id = record.object_id
        track.boxes[frame] = Box(record.left, record.top, record.right, record.bottom,
                                 getattr(record, "confidence", None))
    return SegmentResult(width=width, height=height, tracks=dict(tracks))


class Sam:
    def __init__(self, client: MetaClient) -> None:
        self.client = client

    @property
    def model(self) -> str:
        return self.client.settings.models.sam

    async def _run(self, content: list[dict[str, Any]], fmt: Any, metadata: dict[str, str] | None) -> SegmentResult:
        async def call() -> SegmentResult:
            api = self.client._api()
            kwargs: dict[str, Any] = {"model": self.model, "stream": True, "input": [user(*content)]}
            if metadata:
                kwargs["metadata"] = metadata
            stream = await api.responses.create(**kwargs)
            async with stream, aclosing(_events(stream)) as events:
                parsed = parse_responses_stream(events, fmt)
                async with parsed:
                    async for _ in parsed:
                        pass
                    return _collect(await parsed.final_result())

        return await with_retries(call, key=f"sam:{self.model}", policy=RetryPolicy(attempts=3, base_delay=1.5))

    async def segment_image(self, image: bytes, concept: str) -> SegmentResult:
        if not concept.strip():
            raise ValueError("SAM needs a short noun phrase")
        return await self._run([text_part(concept.strip()), image_part(image)], image_segmentation_format(), None)

    async def segment_image_at(self, image: bytes, size: tuple[int, int], *, point: tuple[int, int] | None = None,
                               box: tuple[int, int, int, int] | None = None) -> SegmentResult:
        """Segment the object under a point (or inside a box) of an image."""
        prompt = build_object_prompt(size=size, objects=[PromptObject(id=0, prompts=[_frame_prompt(0, point, box, size)])])
        return await self._run([text_part(prompt), image_part(image)], image_segmentation_format(), None)

    async def track_video(self, video: Path, concept: str, *, direction: str = "both") -> SegmentResult:
        async def build(file_id: str) -> SegmentResult:
            return await self._run(
                [text_part(concept.strip()), {"type": "input_video", "file_id": file_id}],
                video_segmentation_format(), {"propagation_direction": direction},
            )
        return await self.client.respond_with_file(video, build)

    async def track_video_objects(self, video: Path, size: tuple[int, int],
                                  prompts: Sequence[tuple[int, tuple[int, int] | None, tuple[int, int, int, int] | None]],
                                  *, direction: str = "both") -> SegmentResult:
        """Track objects seeded by (frame, point, box) prompts; up to 16 objects per request."""
        objects = [PromptObject(id=i, prompts=[_frame_prompt(f, p, b, size)]) for i, (f, p, b) in enumerate(prompts[:16])]
        text = build_object_prompt(size=size, objects=objects)

        async def build(file_id: str) -> SegmentResult:
            return await self._run([text_part(text), {"type": "input_video", "file_id": file_id}],
                                   video_segmentation_format(), {"propagation_direction": direction})
        return await self.client.respond_with_file(video, build)


def _clamp(v: int, hi: int) -> int:
    return max(0, min(int(v), hi - 1))


def _frame_prompt(frame: int, point: tuple[int, int] | None, box: tuple[int, int, int, int] | None,
                  size: tuple[int, int]) -> FramePrompt:
    w, h = size
    if box is not None:
        l, t, r, b = box
        l, t = _clamp(l, w), _clamp(t, h)
        r, b = max(l + 1, min(int(r), w)), max(t + 1, min(int(b), h))
        return FramePrompt(frame=frame, box=PromptBox(l, t, r, b),
                           points=[PromptPoint(_clamp(point[0], w), _clamp(point[1], h))] if point else [])
    if point is None:
        raise ValueError("need a point or a box")
    return FramePrompt(frame=frame, points=[PromptPoint(_clamp(point[0], w), _clamp(point[1], h))])

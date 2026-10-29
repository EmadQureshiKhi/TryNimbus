"""Decide from screenshots whether a step really happened, and say what proves it.

Follows the verifiable-agent rules Meta teaches: a pass must cite visible evidence, a claim with no
evidence is treated as a fail, and an uncertain verdict is reported as uncertain rather than
rounded up. The caller escalates to the person after repeated failures instead of guessing.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass

from PIL import Image

from nimbus.meta.client import MetaClient, image_part, text_part, user

log = logging.getLogger(__name__)

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["done", "not_done", "wrong_action", "uncertain"]},
        "evidence": {"type": "string", "description": "the specific visible change in the AFTER image that proves the status"},
        "problem": {"type": "string", "description": "if not done: what is different from what was expected"},
        "hint": {"type": "string", "description": "if not done: one short sentence telling the person what to do"},
        "confidence": {"type": "number"},
    },
    "required": ["status", "evidence", "problem", "hint", "confidence"],
}


@dataclass
class Verdict:
    status: str
    evidence: str
    problem: str
    hint: str
    confidence: float
    model: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "done" and bool(self.evidence.strip()) and self.confidence >= 0.5


def _jpeg(data: bytes, max_side: int = 1400) -> bytes:
    img = Image.open(io.BytesIO(data))
    img.load()
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=88)
    return out.getvalue()


class Verifier:
    def __init__(self, client: MetaClient) -> None:
        self.client = client

    async def check(self, *, instruction: str, expected: str, before: bytes | None, after: bytes,
                    reference_after: bytes | None = None, context: str = "") -> Verdict:
        parts = [text_part(
            "You are checking whether a person completed one step on their computer.\n"
            f"Step: {instruction}\nExpected visible result: {expected or '(not specified)'}\n"
            + (f"Context: {context}\n" if context else "")
            + "Images: " + ("BEFORE screenshot, then " if before else "")
            + "AFTER screenshot" + (", then REFERENCE showing the expected result from the tutorial (theme, size or "
                                    "layout may differ)" if reference_after else "") + ".\n"
            "Judge only from what is visible. Status 'done' requires a concrete visible change you can name in "
            "'evidence'. Use 'wrong_action' if they clearly did something else, 'not_done' if nothing relevant "
            "changed, 'uncertain' if the screenshots cannot settle it."
        )]
        if before:
            parts.append(image_part(_jpeg(before)))
        parts.append(image_part(_jpeg(after)))
        if reference_after:
            parts.append(image_part(_jpeg(reference_after, 1000)))
        data, reply = await self.client.respond_json(
            self.client.settings.models.verify, [user(*parts)], schema=VERDICT_SCHEMA, schema_name="verdict",
            what="verify", open_weights_role="verify", reasoning_effort="low",
        )
        if isinstance(data, list):
            data = data[0] if data else {}
        verdict = Verdict(
            status=str(data.get("status", "uncertain")),
            evidence=str(data.get("evidence", "")),
            problem=str(data.get("problem", "")),
            hint=str(data.get("hint", "")),
            confidence=float(data.get("confidence") or 0.0),
            model=reply.model,
        )
        if verdict.status == "done" and not verdict.passed:
            log.info("verifier said done without evidence or confidence; downgrading to uncertain")
            verdict.status = "uncertain"
        return verdict

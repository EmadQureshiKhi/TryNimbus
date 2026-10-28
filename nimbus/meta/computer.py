"""The Responses API computer tool: an observe-act loop with a pluggable driver.

The model sees a screenshot and returns `computer_call` actions in that screenshot's pixel space;
the driver executes them (on the Mac with Quartz events, or in a Playwright page) and returns the
next screenshot. Safety checks the API attaches are routed to an approval callback, which decides
whether to acknowledge them (without a callback they are declined). The loop stops on: a final message, a declined check, the step budget, a
stop request, or an action the driver refuses.
"""
from __future__ import annotations

import base64
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from nimbus.meta.client import MetaClient, image_part, text_part, user
from nimbus.meta.resilience import MetaAPIError, first_success

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a computer-use agent operating the user's real computer through screenshots.
Coordinates are pixels in the screenshot you were last shown: (0,0) is the top-left. Click the centre
of the target element. Work fast: return every action whose target you can already see in one
computer_call (for example click a text field, type the text, then click the send button), and only
wait for a new screenshot when the next target depends on what the previous action changes (a page
load, an app launch, a menu or dialog opening). Use wait while something loads.
To open an application, press cmd+space, type its name and press return.
Never type passwords or payment details, never send messages, purchase, delete or submit forms unless
the task explicitly says so. Finish with a normal message that starts with "DONE:" followed by a
one-sentence summary when the goal is visibly complete, or "INFEASIBLE:" followed by a short reason
when it cannot be done. Do not claim completion unless the screenshot shows it."""


class Driver(Protocol):
    async def screenshot(self) -> bytes: ...
    async def execute(self, action: dict[str, Any]) -> None: ...


@dataclass
class AgentResult:
    status: str  # done | infeasible | stopped | declined | budget | error
    message: str
    steps: int
    actions: list[dict[str, Any]] = field(default_factory=list)


def _data_url(img: bytes) -> str:
    mime = "image/png" if img[:4] == b"\x89PNG" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(img).decode()}"


def _as_dict(obj: Any) -> dict[str, Any]:
    return obj.model_dump() if hasattr(obj, "model_dump") else dict(obj)


async def run_agent(
    client: MetaClient,
    driver: Driver,
    task: str,
    *,
    max_steps: int = 25,
    approve: Callable[[list[dict[str, Any]]], Awaitable[bool]] | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_action: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    system_prompt: str = SYSTEM_PROMPT,
    models: list[str] | None = None,
    effort: str = "low",
) -> AgentResult:
    models = models or client.settings.models.computer_use
    shot = await driver.screenshot()
    first_input = [user(text_part(task), image_part(shot))]

    async def start(model: str):
        return await client.respond(model, first_input, instructions=system_prompt, tools=[{"type": "computer"}],
                                    reasoning_effort=effort)

    reply = await first_success(models, start, what="computer use")
    model = reply.model
    response = reply.raw
    taken: list[dict[str, Any]] = []
    for step in range(max_steps):
        if should_stop and should_stop():
            return AgentResult("stopped", "Stopped.", step, taken)
        calls = [item for item in (getattr(response, "output", None) or []) if getattr(item, "type", "") == "computer_call"]
        if not calls:
            text = (getattr(response, "output_text", "") or "").strip()
            return AgentResult(_final_status(text), _strip_status(text) or "Done.", step, taken)
        call = calls[0]
        checks = [_as_dict(c) for c in (getattr(call, "pending_safety_checks", None) or [])]
        if checks:
            ok = await approve(checks) if approve else False
            if not ok:
                return AgentResult("declined", "A safety check was declined: " +
                                   "; ".join(c.get("message", "") for c in checks), step, taken)
        actions = [_as_dict(a) for a in (getattr(call, "actions", None) or [])]
        if not actions and getattr(call, "action", None) is not None:
            actions = [_as_dict(call.action)]
        for action in actions:
            if should_stop and should_stop():
                return AgentResult("stopped", "Stopped.", step, taken)
            if on_action:
                await on_action(action)
            try:
                await driver.execute(action)
            except Exception as exc:
                log.warning("driver could not execute %s: %s", action.get("type"), exc)
                break  # return a fresh screenshot and let the model re-plan
            taken.append(action)
        shot = await driver.screenshot()
        try:
            reply = await client.respond(
                model,
                [{"type": "computer_call_output", "call_id": call.call_id, "acknowledged_safety_checks": checks,
                  "output": {"type": "computer_screenshot", "image_url": _data_url(shot)}}],
                previous_response_id=response.id,
                instructions=system_prompt,
                tools=[{"type": "computer"}],
                reasoning_effort=effort,
            )
        except MetaAPIError as exc:
            lost = _continuation_lost(exc)
            if not lost and not _server_trouble(exc):
                return AgentResult("error", f"The model call failed: {exc}", step, taken)
            # Carry on from a fresh request that states the goal, what was already done, and the screen
            # as it is now: the stored conversation cannot take this step, or the model kept failing
            # (5xx after every retry, seen live mid-drawing), in which case the next model goes first.
            log.warning("computer-use step failed (%s); resuming from the current screen", exc)
            order = [model] + [m for m in models if m != model]
            if not lost:
                order = order[1:] + order[:1]
            resume_input = [user(text_part(_resume_prompt(task, taken)), image_part(shot))]

            async def resume(m: str):
                return await client.respond(m, resume_input, instructions=system_prompt, tools=[{"type": "computer"}],
                                            reasoning_effort=effort)

            try:
                reply = await first_success(order, resume, what="computer use")
            except MetaAPIError as exc2:
                return AgentResult("error", f"The model call failed: {exc2}", step, taken)
            model = reply.model
        response = reply.raw
    return AgentResult("budget", f"Stopped after {max_steps} steps without finishing.", max_steps, taken)


def _continuation_lost(exc: MetaAPIError) -> bool:
    text = str(exc).lower()
    return exc.status in (400, 404) and ("previous_response_id" in text or "too large" in text
                                         or "not found" in text or "expired" in text)


def _server_trouble(exc: MetaAPIError) -> bool:
    """A failure that a fresh request (or another model) may not hit: server errors, rate limits, an open
    circuit or a dropped connection, after the client's own retries."""
    return exc.status is None or exc.status in (408, 429) or exc.status >= 500


def _resume_prompt(task: str, taken: list[dict[str, Any]]) -> str:
    done = "; ".join(_short(a) for a in taken[-12:]) or "nothing yet"
    return (f"{task}\n\nYou were already working on this. Actions taken so far: {done}. "
            "The screenshot shows the screen now. Continue from here; do not repeat finished steps.")


def _short(action: dict[str, Any]) -> str:
    kind = action.get("type", "")
    if kind == "type":
        return f"typed {str(action.get('text', ''))[:40]!r}"
    if kind == "keypress":
        return "pressed " + "+".join(action.get("keys", []))
    return kind.replace("_", " ")


_INFEASIBLE_HINTS = ("infeasible", "cannot ", "can't ", "could not", "couldn't", "unable to", "not possible",
                     "no such", "is not visible", "not available")


def _final_status(text: str) -> str:
    """The model's final message decides the outcome; an explanation of failure is not success."""
    head = text.lstrip().upper()
    if head.startswith("DONE"):
        return "done"
    if head.startswith("INFEASIBLE"):
        return "infeasible"
    low = text.lower()
    return "infeasible" if any(h in low for h in _INFEASIBLE_HINTS) else "done"


def _strip_status(text: str) -> str:
    for tag in ("DONE:", "INFEASIBLE:", "DONE", "INFEASIBLE"):
        if text.lstrip().upper().startswith(tag):
            return text.lstrip()[len(tag):].strip()
    return text.strip()

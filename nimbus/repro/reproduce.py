"""Reproduce a bug step plan in a real Chromium and decide, with evidence, whether the bug shows.

Each step goes through a ladder of executors, cheapest and most deterministic first:

1. Direct: `navigate` loads the URL (local URLs from the recording are rebased onto the configured
   app URL), `keypress`, `wait`, `scroll` and untargeted typing go straight to the keyboard/mouse.
2. Locators: accessible-name, label, placeholder and text locators derived from `step.target`.
   A locator must resolve to exactly one visible element; when it matches several (four "Add to
   cart" buttons), the words of the step's description pick the one whose surroundings mention
   them ("Aurora Headphones"), and a tie is never guessed.
3. DOM scoring: every interactive element is scored against the target and description words.
4. Vision: a screenshot goes to `Grounder.locate` with the step's target, description and the
   crop of the control from the recording, and the click lands on the returned pixel.
5. Computer use: `run_agent` with a Playwright driver gets one short task for the step.

A failed step is retried once from the top of the ladder (pages that are still loading are the
usual cause), and the computer-use agent is only used after that. The run records a Playwright
video and a screenshot after every step. The final screenshot, the page text and the frame of
the recording where the bug was visible go to `Verifier.check`, whose verdict (with the evidence
it cites) decides `reproduced`. No verifier means `uncertain`, never `yes`.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit, urlunsplit

from PIL import Image
from playwright.async_api import Browser, BrowserContext, Locator, Page, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from nimbus import media
from nimbus.engine.grounding import Grounder
from nimbus.engine.steps import PlanStep, StepPlan
from nimbus.engine.verify import Verdict, Verifier
from nimbus.meta.client import MetaClient
from nimbus.meta.computer import AgentResult, run_agent
from nimbus.meta.resilience import MetaAPIError

log = logging.getLogger(__name__)

Reproduced = Literal["yes", "no", "uncertain"]
Mode = Literal["bug", "fixed"]
StepCallback = Callable[["StepOutcome", int], None]
AgentRunner = Callable[[Page, PlanStep, str], Awaitable[AgentResult]]

VIEWPORT = (1280, 800)
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"})

BROWSER_AGENT_PROMPT = """You are operating a web page in a browser through screenshots, to reproduce one step of
a bug report. Coordinates are pixels in the screenshot you were last shown, (0,0) top-left. Do only the
single step you are given, then stop with a one-sentence message saying what you did. Never type
passwords or payment details and never submit anything the step does not ask for. If the step is
impossible on this page, stop and say why."""

STOPWORDS = frozenset("""a an the to on in of for and or with at by from into onto this that it its is are be
then click clicks clicking press tap select choose type enter field fields button buttons link links input box
textbox text icon menu item tab checkbox dropdown control labeled labelled named called area section page
screen top bottom left right upper lower side near next under above below corner green blue red white dark
light large small big primary secondary""".split())
ROLE_WORDS = ("button", "link", "field", "input", "text box", "textbox", "box", "icon", "menu item", "tab",
              "checkbox", "dropdown", "control", "option")

CLICK_ROLES = ("button", "link", "tab", "menuitem", "checkbox", "radio", "option", "switch")
TEXT_ROLES = ("textbox", "searchbox", "combobox", "spinbutton")
SELECT_ROLES = ("combobox", "listbox")


# ---------------------------------------------------------------- results


@dataclass
class StepOutcome:
    index: int
    action: str
    target: str
    ok: bool
    method: str  # navigate | locator | dom | vision | agent | keyboard | mouse | wait | skipped | failed
    detail: str = ""
    attempts: int = 1
    screenshot: str | None = None  # absolute path
    duration_s: float = 0.0


@dataclass
class ReproResult:
    """What happened when the plan ran, and whether the bug showed.

    `reproduced` answers "is the bug visible at the end?": in mode "bug" a `yes` confirms the
    report; in mode "fixed" a `no` (with `expected_visible`) is what proves a fix.
    """

    mode: Mode
    reproduced: Reproduced
    evidence: str
    problem: str = ""
    confidence: float = 0.0
    completed: bool = False
    expected_visible: bool | None = None
    verdict_status: str = ""
    verifier_model: str = ""
    steps: list[StepOutcome] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    final_screenshot: str | None = None
    reference_frame: str | None = None
    video: str | None = None
    console_errors: list[str] = field(default_factory=list)
    page_text: str = ""
    url: str = ""
    out_dir: str = ""
    duration_s: float = 0.0

    @property
    def methods(self) -> list[str]:
        return [s.method for s in self.steps]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, path: Path) -> Path:
        path.write_text(json.dumps(self.to_dict(), indent=2))
        return path


class StepFailed(RuntimeError):
    pass


# ---------------------------------------------------------------- pure helpers


def resolve_url(raw: str, app_url: str) -> str:
    """Map a URL seen in the recording onto the app under test.

    A recording made against `localhost:3000` must replay against the configured app URL, so any
    local host (or the app's own host) is rebased onto `app_url`, keeping path, query and hash.
    Relative paths resolve against `app_url`. Only http(s) URLs are ever returned.
    """
    raw = (raw or "").strip().strip("\"'<>")
    if not raw:
        return app_url
    # Models sometimes wrap the URL in prose ("Navigate to http://localhost:5173/ - fresh load").
    embedded = re.search(r"https?://[^\s'\"<>`)]+", raw) or re.search(
        r"\b(?:localhost|\d{1,3}(?:\.\d{1,3}){3})(?::\d+)?(?:/[^\s'\"<>`)]*)?", raw)
    if embedded and embedded.group(0) != raw:
        raw = embedded.group(0).rstrip(".,;")
    elif " " in raw and not raw.startswith(("/", "?", "#")):
        return app_url  # prose with no URL in it: fall back to the app's own address
    if raw.startswith(("/", "?", "#")):
        return urljoin(app_url, raw)
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        host_like = re.match(r"^(localhost|\d{1,3}(\.\d{1,3}){3}|\[[0-9a-fA-F:]+\]|[\w-]+(\.[\w-]+)+)(:\d+)?(/|$)", raw)
        raw = f"http://{raw}" if host_like else urljoin(app_url.rstrip("/") + "/", raw)
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise StepFailed(f"refusing to navigate to a {parts.scheme or 'schemeless'} URL")
    app = urlsplit(app_url)
    if (parts.hostname or "") in LOCAL_HOSTS or parts.hostname == app.hostname:
        return urlunsplit((app.scheme, app.netloc, parts.path or "/", parts.query, parts.fragment))
    return raw


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9$.]+", (text or "").lower()) if w not in STOPWORDS and len(w) > 1]


def split_target(target: str) -> tuple[str, str]:
    """Split a model-written target into (control, context).

    Models often write a path with location notes: "AURORA card > Aurora Headphones > Add to cart
    button [top left tile]". The control is the last segment; earlier segments are context that
    tells apart several controls with the same name. Bracketed notes are dropped.
    """
    cleaned = re.sub(r"\[[^\]]*\]", " ", target or "")
    parts = [p.strip() for p in re.split(r"\s*(?:>|\u203a|\u2192)\s*", cleaned) if p.strip()]
    if not parts:
        return "", ""
    return parts[-1], " ".join(parts[:-1])


def target_phrases(target: str) -> list[str]:
    """Candidate accessible names for a model-written target like "'Apply' button (discount)"."""
    target = (target or "").strip()
    if not target:
        return []
    primary, _context = split_target(target)
    phrases: list[str] = []
    if primary and primary != target:
        phrases.extend(target_phrases(primary))
    quoted = re.findall(r"[\"“'‘]([^\"”'’]{1,80})[\"”'’]", target)
    phrases.extend(quoted)
    base = re.sub(r"\(.*?\)", " ", target)
    base = re.sub(r"[\"“”'‘’]", "", base).strip()
    phrases.append(base)
    stripped = base
    for word in ROLE_WORDS:
        stripped = re.sub(rf"\b(the\s+)?{word}\b", " ", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\s+", " ", stripped).strip(" -:,")
    phrases.append(stripped)
    head = re.split(r"\s+(?:for|on|in|of|next to|under|beside)\s+", stripped, maxsplit=1, flags=re.IGNORECASE)[0]
    phrases.append(head.strip())
    seen: set[str] = set()
    out = []
    for p in phrases:
        key = p.lower()
        if p and key not in seen and len(p) <= 120:
            seen.add(key)
            out.append(p)
    return out


_KEY_NAMES = {
    "cmd": "Meta", "command": "Meta", "meta": "Meta", "super": "Meta", "win": "Meta",
    "ctrl": "Control", "control": "Control", "alt": "Alt", "option": "Alt", "opt": "Alt", "shift": "Shift",
    "return": "Enter", "enter": "Enter", "esc": "Escape", "escape": "Escape", "tab": "Tab", "space": " ",
    "spacebar": " ", "backspace": "Backspace", "delete": "Delete", "del": "Delete", "up": "ArrowUp",
    "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight", "arrowup": "ArrowUp",
    "arrowdown": "ArrowDown", "arrowleft": "ArrowLeft", "arrowright": "ArrowRight", "pageup": "PageUp",
    "pagedown": "PageDown", "home": "Home", "end": "End",
}


def normalize_key(key: str) -> str:
    key = key.strip()
    named = _KEY_NAMES.get(key.lower())
    if named:
        return named
    if re.fullmatch(r"[fF]\d{1,2}", key):
        return key.upper()
    return key if len(key) != 1 else key.lower()


# Chords that a real browser handles itself (a page never sees them), done as the page actions they mean.
BROWSER_KEYS = {"Meta+r": "reload", "Control+r": "reload", "F5": "reload", "Meta+Shift+r": "reload",
                "Control+Shift+r": "reload", "Meta+[": "go_back", "Alt+ArrowLeft": "go_back", "Meta+]": "go_forward",
                "Alt+ArrowRight": "go_forward"}


def key_chords(text: str) -> list[str]:
    """'cmd+s' -> ['Meta+s']; 'Tab Tab Enter' -> ['Tab', 'Tab', 'Enter']."""
    chords = []
    for chunk in re.split(r"[\s,]+", (text or "").strip()):
        if not chunk:
            continue
        if chunk.endswith("+"):  # the plus key itself: "+" or "ctrl++"
            parts = [p for p in chunk[:-1].rstrip("+").split("+") if p] + ["+"]
        else:
            parts = [p for p in chunk.split("+") if p]
        chords.append("+".join(normalize_key(p) for p in parts))
    return chords


def wait_seconds(text: str, default: float = 1.0) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(ms|millisecond|s|sec|second)?", text or "")
    if not m:
        return default
    value = float(m.group(1))
    if (m.group(2) or "").startswith("m"):
        value /= 1000
    return max(0.1, min(10.0, value))


# ---------------------------------------------------------------- computer-use driver


class PlaywrightDriver:
    """`nimbus.meta.computer.Driver` for a Playwright page.

    Actions arrive in the pixel space of the last screenshot. Screenshots are taken at device
    scale 1 so that space equals CSS pixels, but the scale is measured anyway in case a caller
    configured a HiDPI context.
    """

    def __init__(self, page: Page) -> None:
        self.page = page
        self._scale = 1.0

    async def screenshot(self) -> bytes:
        png = await self.page.screenshot(type="png")
        width = Image.open(io.BytesIO(png)).size[0]
        viewport = self.page.viewport_size or {"width": width}
        self._scale = viewport["width"] / width if width else 1.0
        return png

    def _xy(self, action: dict[str, Any]) -> tuple[float, float]:
        return float(action.get("x", 0)) * self._scale, float(action.get("y", 0)) * self._scale

    async def execute(self, action: dict[str, Any]) -> None:
        kind = action.get("type")
        mouse, keyboard = self.page.mouse, self.page.keyboard
        if kind in ("click", "double_click"):
            x, y = self._xy(action)
            button = str(action.get("button") or "left").lower()
            if button == "back":
                await self.page.go_back()
                return
            if button == "forward":
                await self.page.go_forward()
                return
            button = {"wheel": "middle"}.get(button, button)
            if button not in ("left", "right", "middle"):
                raise ValueError(f"unsupported mouse button {button!r}")
            await mouse.click(x, y, button=button, click_count=2 if kind == "double_click" else 1)  # type: ignore[arg-type]
        elif kind == "move":
            await mouse.move(*self._xy(action))
        elif kind == "scroll":
            await mouse.move(*self._xy(action))
            await mouse.wheel(float(action.get("scroll_x") or 0), float(action.get("scroll_y") or 0))
        elif kind == "type":
            await keyboard.type(str(action.get("text", "")), delay=15)
        elif kind == "keypress":
            keys = action.get("keys") or []
            if isinstance(keys, str):
                keys = [keys]
            chord = "+".join(normalize_key(str(k)) for k in keys)
            browser = BROWSER_KEYS.get(chord)
            if browser:  # the browser's own shortcuts never reach a page's key handlers
                await getattr(self.page, browser)()
                return
            await keyboard.press(chord)
        elif kind == "drag":
            path = action.get("path") or []
            if len(path) < 2:
                raise ValueError("drag needs at least two points")
            pts = [(float(p["x"]) * self._scale, float(p["y"]) * self._scale) for p in path]
            await mouse.move(*pts[0])
            await mouse.down()
            for pt in pts[1:]:
                await mouse.move(*pt, steps=8)
            await mouse.up()
        elif kind == "wait":
            await asyncio.sleep(min(10.0, float(action.get("ms", 1000)) / 1000))
        elif kind == "screenshot":
            return
        else:
            raise ValueError(f"unsupported action {kind!r}")


async def _decline_safety_checks(checks: list[dict[str, Any]]) -> bool:
    log.warning("declining computer-use safety checks during reproduction: %s", checks)
    return False


def local_app_safety_policy(page: Page, app_url: str) -> Callable[[list[dict[str, Any]]], Awaitable[bool]]:
    """Safety checks during reproduction: the agent drives an isolated headless browser, so a check is
    approved only while that page is on the app under test on this machine. Any other site, or a
    check flagged as a credential or payment action, is declined."""
    app_host = urlsplit(app_url).hostname or ""

    async def approve(checks: list[dict[str, Any]]) -> bool:
        host = urlsplit(page.url).hostname or ""
        sensitive = any(word in f"{c.get('code', '')} {c.get('message', '')}".lower()
                        for c in checks for word in ("password", "credential", "payment", "card number", "login"))
        ok = host in LOCAL_HOSTS and host == (app_host or host) and not sensitive
        log.info("%s safety checks on %s: %s", "approving" if ok else "declining", page.url, checks)
        return ok

    return approve


def default_agent_runner(client: MetaClient, *, max_steps: int = 8,
                         should_stop: Callable[[], bool] | None = None, app_url: str | None = None) -> AgentRunner:
    async def runner(page: Page, step: PlanStep, task: str) -> AgentResult:
        approve = local_app_safety_policy(page, app_url) if app_url else _decline_safety_checks
        return await run_agent(client, PlaywrightDriver(page), task, max_steps=max_steps,
                               approve=approve, should_stop=should_stop,
                               system_prompt=BROWSER_AGENT_PROMPT)
    return runner


# ---------------------------------------------------------------- the executor

_CANDIDATES_JS = """() => {
  const sel = 'button, a[href], input:not([type=hidden]), select, textarea, summary, [contenteditable=""], '
    + '[contenteditable=true], [role=button], [role=link], [role=tab], [role=menuitem], [role=checkbox], '
    + '[role=radio], [role=option], [role=switch], [role=combobox], [role=textbox], [role=searchbox], [onclick]';
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  const labelOf = (el) => {
    const parts = [];
    if (el.getAttribute('aria-label')) parts.push(el.getAttribute('aria-label'));
    const lb = el.getAttribute('aria-labelledby');
    if (lb) lb.split(/\\s+/).forEach((id) => { const n = document.getElementById(id); if (n) parts.push(n.innerText); });
    if (el.labels) Array.from(el.labels).forEach((l) => parts.push(l.innerText));
    parts.push(el.innerText || '', el.value || '', el.getAttribute('placeholder') || '', el.getAttribute('title') || '',
               el.getAttribute('alt') || '', el.getAttribute('name') || '');
    return parts.join(' ').replace(/\\s+/g, ' ').trim().slice(0, 200);
  };
  const ctxOf = (el) => {
    const c = el.closest('li, article, tr, form, fieldset, section, [role=listitem], [role=row], [class*=card]');
    return c ? c.innerText.replace(/\\s+/g, ' ').trim().slice(0, 300) : '';
  };
  document.querySelectorAll('[data-nimbus-idx]').forEach((el) => el.removeAttribute('data-nimbus-idx'));
  const out = [];
  document.querySelectorAll(sel).forEach((el) => {
    if (!visible(el) || el.disabled) return;
    el.setAttribute('data-nimbus-idx', String(out.length));
    out.push({ idx: out.length, tag: el.tagName.toLowerCase(), name: labelOf(el), context: ctxOf(el) });
  });
  return out;
}"""

_CONTEXT_JS = """(el) => {
  const c = el.closest('li, article, tr, form, fieldset, section, [role=listitem], [role=row], [class*=card]');
  const name = (el.getAttribute('aria-label') || '') + ' ' + (el.innerText || '');
  return (name + ' ' + (c ? c.innerText : '')).replace(/\\s+/g, ' ').trim().slice(0, 400);
}"""


class StepExecutor:
    """Runs plan steps on one page. Separate from `Reproducer` so tests can drive it directly."""

    def __init__(self, page: Page, *, app_url: str, plan_dir: Path | None = None, grounder: Grounder | None = None,
                 agent_runner: AgentRunner | None = None, step_timeout: float = 8.0, settle_ms: int = 350) -> None:
        self.page = page
        self.app_url = app_url
        self.plan_dir = plan_dir
        self.grounder = grounder
        self.agent_runner = agent_runner
        self.step_timeout_ms = int(step_timeout * 1000)
        self.settle_ms = settle_ms

    # -- public

    async def run_step(self, step: PlanStep, *, plan: StepPlan | None = None) -> StepOutcome:
        started = time.monotonic()
        last_error = ""
        for attempt in (1, 2):
            try:
                method, detail = await self._dispatch(step)
                await self._settle()
                return StepOutcome(step.index, step.action, step.target, True, method, detail, attempt,
                                   duration_s=time.monotonic() - started)
            except (StepFailed, PlaywrightError, MetaAPIError, ValueError) as exc:
                last_error = _short(exc)
                log.info("step %d attempt %d failed: %s", step.index, attempt, last_error)
                if attempt == 1:
                    await self._settle(extra_ms=800)
        if self.agent_runner is not None and step.action not in ("navigate", "wait", "observe"):
            try:
                result = await self.agent_runner(self.page, step, self._agent_task(step, plan))
                await self._settle()
                if result.status == "done":
                    return StepOutcome(step.index, step.action, step.target, True, "agent",
                                       f"computer use: {result.message}", 3, duration_s=time.monotonic() - started)
                last_error += f"; computer use {result.status}: {result.message}"
            except (MetaAPIError, PlaywrightError) as exc:
                last_error += f"; computer use failed: {_short(exc)}"
        return StepOutcome(step.index, step.action, step.target, False, "failed", last_error, 2,
                           duration_s=time.monotonic() - started)

    async def open_start(self, plan: StepPlan) -> str:
        url = resolve_url(plan.url, self.app_url) if plan.url else self.app_url
        await self._goto(url)
        return url

    # -- dispatch

    async def _dispatch(self, step: PlanStep) -> tuple[str, str]:
        action = step.action
        if action == "type" and _is_address_bar(step):
            action = "navigate"  # typing into the browser's address bar is navigation, not page input
        if action == "navigate" or (action == "open_app" and _looks_like_url(step.text)):
            url = resolve_url(step.text, self.app_url)
            await self._goto(url)
            return "navigate", url
        if action == "open_app":
            return "skipped", "the browser is already open"
        if action == "wait":
            seconds = wait_seconds(step.text)
            await asyncio.sleep(seconds)
            return "wait", f"waited {seconds:.1f}s"
        if action == "observe":
            return "skipped", "observation only"
        if action == "keypress":
            chords = key_chords(step.text or step.target)
            if not chords:
                raise StepFailed("no keys to press")
            for chord in chords:
                await self.page.keyboard.press(chord)
            return "keyboard", " ".join(chords)
        if action == "scroll" and not step.target:
            dy = -600 if re.search(r"\bup\b", f"{step.text} {step.instruction}", re.I) else 600
            await self.page.mouse.wheel(0, dy)
            return "mouse", f"scrolled {dy:+d}px"
        if action == "type" and not step.target:
            await self.page.keyboard.type(step.text, delay=20)
            return "keyboard", f"typed {step.text!r} into the focused element"

        locator, how = await self._find(step)
        if locator is not None:
            # Described before acting: a control may hide or remove itself when used (a Settle up button
            # replaced by "All settled up"), and a role locator then matches nothing afterwards.
            detail = await _describe(locator)
            await self._act_on_locator(step, locator)
            return how, detail
        return await self._vision(step)

    async def _goto(self, url: str) -> None:
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=max(15000, self.step_timeout_ms))
        except PlaywrightTimeout as exc:
            raise StepFailed(f"{url} did not load: {_short(exc)}") from exc

    async def _settle(self, extra_ms: int = 0) -> None:
        try:
            await self.page.wait_for_load_state("networkidle", timeout=3000)
        except PlaywrightError:
            pass
        await self.page.wait_for_timeout(self.settle_ms + extra_ms)

    # -- locating

    def _roles(self, action: str) -> tuple[str, ...]:
        if action == "type":
            return TEXT_ROLES
        if action == "select":
            return SELECT_ROLES
        return CLICK_ROLES

    async def _find(self, step: PlanStep) -> tuple[Locator | None, str]:
        hints = " ".join([step.target, step.target_description, step.instruction])
        for phrase in target_phrases(step.target):
            for locator in self._locators(step.action, phrase):
                chosen = await self._unique(locator, hints)
                if chosen is not None:
                    return chosen, "locator"
        chosen = await self._dom_match(step)
        if chosen is not None:
            return chosen, "dom"
        return None, ""

    def _locators(self, action: str, phrase: str) -> list[Locator]:
        page = self.page
        out: list[Locator] = []
        for exact in (True, False):
            for role in self._roles(action):
                out.append(page.get_by_role(role, name=phrase, exact=exact))  # type: ignore[arg-type]
        if action in ("type", "select"):
            out += [page.get_by_label(phrase, exact=True), page.get_by_placeholder(phrase, exact=True),
                    page.get_by_label(phrase), page.get_by_placeholder(phrase)]
        else:
            out += [page.get_by_text(phrase, exact=True), page.get_by_label(phrase, exact=True),
                    page.get_by_title(phrase, exact=True)]
        return out

    async def _unique(self, locator: Locator, hints: str) -> Locator | None:
        """The single visible element `locator` means, using `hints` to break ties; else None."""
        try:
            count = await locator.count()
        except PlaywrightError:
            return None
        if count == 0:
            return None
        visible = []
        for i in range(min(count, 25)):
            item = locator.nth(i)
            try:
                if await item.is_visible():
                    visible.append(item)
            except PlaywrightError:
                continue
        if len(visible) == 1:
            return visible[0]
        if len(visible) < 2:
            return None
        hint_words = set(words(hints))
        scored = []
        for item in visible:
            try:
                ctx = await item.evaluate(_CONTEXT_JS)
            except PlaywrightError:
                ctx = ""
            scored.append((len(hint_words & set(words(ctx))), item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        if scored[0][0] > scored[1][0]:
            return scored[0][1]
        log.info("locator matched %d elements and the hints do not single one out", len(visible))
        return None

    async def _dom_match(self, step: PlanStep) -> Locator | None:
        """Score every interactive element by word overlap with the target and description."""
        primary, context = split_target(step.target)
        target_words = set(words(primary)) or set(words(step.target))
        if not target_words:
            return None
        hint_words = set(words(f"{context} {step.target_description} {step.instruction} {step.text}")) - target_words
        try:
            candidates = await self.page.evaluate(_CANDIDATES_JS)
        except PlaywrightError as exc:
            log.debug("candidate scan failed: %s", exc)
            return None
        roles = {"type": {"input", "textarea"}, "select": {"select"}}.get(step.action)
        scored = []
        for c in candidates:
            if roles and c["tag"] not in roles and not (step.action == "type" and c["tag"] == "div"):
                continue
            name_words = set(words(c["name"]))
            name_hits = len(target_words & name_words)
            if name_hits == 0:
                continue
            coverage = name_hits / len(target_words)
            # Context words count whether they sit in the control's own name ("Add Aurora Headphones to
            # cart") or around it (the product card), which is what separates identical buttons.
            score = 2 * coverage + 0.35 * len(hint_words & (name_words | set(words(c["context"]))))
            scored.append((score, coverage, c["idx"]))
        if not scored:
            return None
        scored.sort(reverse=True)
        best = scored[0]
        if best[1] < 0.5 or (len(scored) > 1 and best[0] - scored[1][0] < 0.2):
            return None
        return self.page.locator(f'[data-nimbus-idx="{best[2]}"]')

    async def _act_on_locator(self, step: PlanStep, locator: Locator) -> None:
        timeout = self.step_timeout_ms
        action = step.action
        if action == "click":
            await locator.click(timeout=timeout)
        elif action == "double_click":
            await locator.dblclick(timeout=timeout)
        elif action == "right_click":
            await locator.click(button="right", timeout=timeout)
        elif action == "type":
            await locator.click(timeout=timeout)
            await locator.fill("", timeout=timeout)
            await locator.press_sequentially(step.text, delay=20, timeout=timeout)
        elif action == "select":
            await self._select(locator, step.text)
        elif action == "scroll":
            await locator.scroll_into_view_if_needed(timeout=timeout)
        elif action == "drag":
            raise StepFailed("drag needs a destination the locator ladder cannot know")
        else:
            raise StepFailed(f"action {action!r} does not take a target")

    async def _select(self, locator: Locator, value: str) -> None:
        tag = await locator.evaluate("(el) => el.tagName.toLowerCase()")
        if tag == "select":
            for kwargs in ({"label": value}, {"value": value}):
                try:
                    await locator.select_option(timeout=self.step_timeout_ms, **kwargs)
                    return
                except PlaywrightError:
                    continue
            raise StepFailed(f"no option {value!r}")
        await locator.click(timeout=self.step_timeout_ms)  # custom dropdown: open it, pick the option
        option = await self._unique(self.page.get_by_role("option", name=value), value) or \
            await self._unique(self.page.get_by_text(value, exact=True), value)
        if option is None:
            raise StepFailed(f"no option {value!r} in the opened dropdown")
        await option.click(timeout=self.step_timeout_ms)

    # -- vision

    def _reference(self, step: PlanStep) -> bytes | None:
        crop = step.evidence.target_crop
        if not crop or not self.plan_dir:
            return None
        path = self.plan_dir / crop
        try:
            return path.read_bytes()
        except OSError:
            log.debug("reference crop %s missing", path)
            return None

    async def _vision(self, step: PlanStep) -> tuple[str, str]:
        if self.grounder is None:
            raise StepFailed(f"no element matches {step.target!r} and vision is not available")
        if step.action not in ("click", "double_click", "right_click", "type", "select", "scroll"):
            raise StepFailed(f"vision cannot perform {step.action!r}")
        shot = await self.page.screenshot(type="png")
        width, height = Image.open(io.BytesIO(shot)).size
        hit = await self.grounder.locate(shot, step.target or step.instruction, description=step.target_description,
                                         reference=self._reference(step), context="A web page in a browser.")
        if hit is None:
            raise StepFailed(f"vision could not find {step.target!r} on the page")
        viewport = self.page.viewport_size or {"width": width, "height": height}
        x, y = hit.x * viewport["width"] / width, hit.y * viewport["height"] / height
        if not (0 <= x <= viewport["width"] and 0 <= y <= viewport["height"]):
            raise StepFailed(f"vision returned a point outside the page: {x:.0f},{y:.0f}")
        mouse = self.page.mouse
        if step.action in ("click", "double_click", "right_click"):
            await self._click_must_change(step, shot, x, y)
            return "vision", f"clicked ({x:.0f}, {y:.0f}) on {step.target!r} (confidence {hit.confidence:.2f})"
        if step.action == "double_click":
            await mouse.dblclick(x, y)
        elif step.action == "right_click":
            await mouse.click(x, y, button="right")
        elif step.action == "scroll":
            await mouse.move(x, y)
            await mouse.wheel(0, 600)
        else:
            await mouse.click(x, y)
            if step.action == "type":
                await self.page.keyboard.press("ControlOrMeta+a")
                await self.page.keyboard.press("Backspace")
                await self.page.keyboard.type(step.text, delay=20)
            elif step.action == "select" and step.text:
                await self.page.keyboard.type(step.text, delay=20)
                await self.page.keyboard.press("Enter")
        return "vision", f"clicked ({x:.0f}, {y:.0f}) on {step.target!r} (confidence {hit.confidence:.2f})"

    async def _click_must_change(self, step: PlanStep, before: bytes, x: float, y: float) -> None:
        """A vision click that changes nothing on the page most likely missed; say so, so the retry and
        computer-use fallbacks run instead of the step being counted as done."""
        mouse = self.page.mouse
        if step.action == "double_click":
            await mouse.dblclick(x, y)
        elif step.action == "right_click":
            await mouse.click(x, y, button="right")
        else:
            await mouse.click(x, y)
        await self.page.wait_for_timeout(350)
        after = await self.page.screenshot(type="png")
        if _same_pixels(before, after):
            raise StepFailed(f"clicking ({x:.0f}, {y:.0f}) for {step.target!r} changed nothing on the page")

    def _agent_task(self, step: PlanStep, plan: StepPlan | None) -> str:
        lines = [f"Do this one step on the web page: {step.instruction or step.action + ' ' + step.target}."]
        if step.target:
            lines.append(f"Control: {step.target}. {step.target_description}".strip())
        if step.text:
            lines.append(f"Text or keys: {step.text}")
        if step.expected_after:
            lines.append(f"Afterwards this should be visible: {step.expected_after}")
        if plan and plan.title:
            lines.append(f"(This is part of reproducing the bug report: {plan.title}.)")
        return "\n".join(lines)


def _is_address_bar(step: PlanStep) -> bool:
    text = f"{step.target} {step.target_description}".lower()
    return (any(k in text for k in ("address bar", "url bar", "location bar", "omnibox", "search or type"))
            or (_looks_like_url(step.text) and not step.target))


def _same_pixels(a: bytes, b: bytes) -> bool:
    from PIL import ImageChops

    ia, ib = Image.open(io.BytesIO(a)).convert("RGB"), Image.open(io.BytesIO(b)).convert("RGB")
    if ia.size != ib.size:
        return False
    return ImageChops.difference(ia, ib).getbbox() is None


def _looks_like_url(text: str) -> bool:
    return bool(re.match(r"^(https?://|localhost|\d{1,3}(\.\d{1,3}){3}|/)", (text or "").strip()))


async def _describe(locator: Locator) -> str:
    try:
        desc = await locator.evaluate(
            "(el) => el.tagName.toLowerCase() + ' \"' + (el.getAttribute('aria-label') || el.innerText || "
            "el.getAttribute('placeholder') || el.value || '').trim().slice(0, 60) + '\"'",
            timeout=2000)  # a label for the log is never worth waiting out Playwright's 30 s default
        return str(desc)
    except PlaywrightError:
        return "element"


def _short(exc: BaseException) -> str:
    return str(exc).strip().splitlines()[0][:300] if str(exc).strip() else type(exc).__name__


# ---------------------------------------------------------------- the reproducer

_DEFAULT = object()


class Reproducer:
    """Run a bug plan in a fresh headless Chromium context and judge the end state.

    `client` may be None (tests, offline use): then vision, computer use and verification are off
    unless fakes are passed in, and the verdict is `uncertain`.
    """

    def __init__(self, client: MetaClient | None, *, app_url: str, grounder: Any = _DEFAULT, verifier: Any = _DEFAULT,
                 agent_runner: Any = _DEFAULT, headless: bool = True, viewport: tuple[int, int] = VIEWPORT,
                 step_timeout: float = 8.0, max_agent_steps: int = 8) -> None:
        self.client = client
        self.app_url = app_url
        self.grounder: Grounder | None = (Grounder(client) if client else None) if grounder is _DEFAULT else grounder
        self.verifier: Verifier | None = (Verifier(client) if client else None) if verifier is _DEFAULT else verifier
        self._agent_runner = agent_runner
        self.max_agent_steps = max_agent_steps
        self.headless = headless
        self.viewport = viewport
        self.step_timeout = step_timeout

    def _runner(self, should_stop: Callable[[], bool] | None) -> AgentRunner | None:
        if self._agent_runner is not _DEFAULT:
            return self._agent_runner
        if self.client is None:
            return None
        return default_agent_runner(self.client, max_steps=self.max_agent_steps, should_stop=should_stop,
                                    app_url=self.app_url)

    async def reproduce(self, plan: StepPlan, out_dir: Path, *, plan_dir: Path | None = None, mode: Mode = "bug",
                        compare_to: ReproResult | None = None, should_stop: Callable[[], bool] | None = None,
                        on_step: StepCallback | None = None) -> ReproResult:
        started = time.monotonic()
        out_dir.mkdir(parents=True, exist_ok=True)
        shots_dir = out_dir / "steps"
        shots_dir.mkdir(exist_ok=True)
        video_dir = out_dir / "video-tmp"
        result = ReproResult(mode=mode, reproduced="uncertain", evidence="", out_dir=str(out_dir))

        async with async_playwright() as pw:
            browser: Browser = await pw.chromium.launch(headless=self.headless)
            context: BrowserContext | None = None
            video = None
            try:
                context = await browser.new_context(
                    viewport={"width": self.viewport[0], "height": self.viewport[1]}, device_scale_factor=1,
                    record_video_dir=str(video_dir),
                    record_video_size={"width": self.viewport[0], "height": self.viewport[1]},
                )
                page = await context.new_page()
                video = page.video
                page.on("console", lambda m: result.console_errors.append(f"console: {m.text}")
                        if m.type == "error" else None)
                page.on("pageerror", lambda e: result.console_errors.append(f"page error: {e}"))
                executor = StepExecutor(page, app_url=self.app_url, plan_dir=plan_dir, grounder=self.grounder,
                                        agent_runner=self._runner(should_stop), step_timeout=self.step_timeout)
                await self._run_steps(plan, executor, page, result, shots_dir, should_stop, on_step)
                final = out_dir / "final.png"
                await page.screenshot(path=str(final))
                result.final_screenshot = str(final)
                result.url = page.url
                try:
                    result.page_text = (await page.inner_text("body"))[:6000]
                except PlaywrightError:
                    result.page_text = ""
            finally:
                if context is not None:
                    await context.close()  # flushes the video
                await browser.close()
            if video is not None:
                try:
                    src = Path(await video.path())
                    dest = out_dir / "reproduction.webm"
                    src.replace(dest)
                    result.video = str(dest)
                except (PlaywrightError, OSError) as exc:
                    log.warning("could not keep the reproduction video: %s", exc)
            _rmdir_quiet(video_dir)

        await self._judge(plan, result, mode, compare_to, out_dir)
        result.duration_s = round(time.monotonic() - started, 2)
        result.save(out_dir / "result.json")
        log.info("reproduction (%s): %s - %s", mode, result.reproduced, result.evidence[:160])
        return result

    async def _run_steps(self, plan: StepPlan, executor: StepExecutor, page: Page, result: ReproResult,
                         shots_dir: Path, should_stop: Callable[[], bool] | None,
                         on_step: StepCallback | None) -> None:
        steps = sorted(plan.steps, key=lambda s: s.index)
        if not steps or steps[0].action != "navigate":
            try:
                await executor.open_start(plan)
            except (StepFailed, PlaywrightError) as exc:
                result.problem = f"the app did not load: {_short(exc)}"
                return
        for step in steps:
            if should_stop and should_stop():
                result.problem = "stopped before every step ran"
                return
            outcome = await executor.run_step(step, plan=plan)
            shot = shots_dir / f"step{step.index:02d}.png"
            try:
                await page.screenshot(path=str(shot))
                outcome.screenshot = str(shot)
                result.screenshots.append(str(shot))
            except PlaywrightError as exc:
                log.warning("screenshot after step %d failed: %s", step.index, exc)
            result.steps.append(outcome)
            if on_step:
                on_step(outcome, len(steps))
            if not outcome.ok:
                result.problem = f"step {step.index + 1} ({step.action} {step.target!r}) failed: {outcome.detail}"
                return
        result.completed = True

    async def _judge(self, plan: StepPlan, result: ReproResult, mode: Mode, compare_to: ReproResult | None,
                     out_dir: Path) -> None:
        if not result.completed:
            result.reproduced = "no" if mode == "bug" else "uncertain"
            result.evidence = f"The steps could not be completed: {result.problem}"
            return
        if self.verifier is None:
            result.reproduced = "uncertain"
            result.evidence = "Steps completed, but no verifier is configured to judge the final screen."
            return
        after = Path(result.final_screenshot or "").read_bytes()
        steps_text = "; ".join(f"{s.index + 1}. {s.instruction or (s.action + ' ' + s.target)}" for s in plan.steps)
        page_text = re.sub(r"\s+", " ", result.page_text)[:1500]
        errors = "; ".join(result.console_errors[:5])
        if mode == "bug":
            reference = await self._reference_frame(plan, out_dir)
            if reference:
                result.reference_frame = str(out_dir / "reference_bug.png")
            verdict = await self.verifier.check(
                instruction=f"Reproduce the reported bug '{plan.title}' by following: {steps_text}",
                expected=f"The bug is visible: {plan.actual}",
                before=None, after=after, reference_after=reference,
                context=(f"Correct behaviour would be: {plan.expected}. "
                         + ("The REFERENCE is the frame of the bug recording where the bug shows. " if reference else "")
                         + "Answer 'done' only if the AFTER screenshot shows the buggy behaviour, and quote the exact "
                         f"on-screen values that prove it. Visible page text: {page_text}"
                         + (f" Browser errors: {errors}" if errors else "")),
            )
            result.reproduced = _bug_status(verdict)
        else:
            before = Path(compare_to.final_screenshot).read_bytes() if compare_to and compare_to.final_screenshot else None
            verdict = await self.verifier.check(
                instruction="Re-run the bug reproduction after a code fix and check that the bug is gone.",
                expected=f"The correct behaviour is visible: {plan.expected}. The bug ({plan.actual}) does not appear.",
                before=before, after=after,
                context=(("BEFORE is the same steps on the code before the fix, where the bug showed. " if before else "")
                         + "Answer 'done' only if the AFTER screenshot shows the correct behaviour, quoting the exact "
                         "on-screen values. Use 'not_done' if the bug is still visible. "
                         f"Visible page text: {page_text}" + (f" Browser errors: {errors}" if errors else "")),
            )
            result.expected_visible = verdict.passed
            result.reproduced = "no" if verdict.passed else ("yes" if verdict.status in ("not_done", "wrong_action")
                                                             else "uncertain")
        result.verdict_status = verdict.status
        result.evidence = verdict.evidence or verdict.problem
        result.problem = result.problem or verdict.problem
        result.confidence = verdict.confidence
        result.verifier_model = verdict.model

    async def _reference_frame(self, plan: StepPlan, out_dir: Path) -> bytes | None:
        if plan.bug_time is None or not plan.source_video or not Path(plan.source_video).exists():
            return None
        try:
            frame = await asyncio.to_thread(media.frame_at, Path(plan.source_video), plan.bug_time)
        except (media.MediaError, OSError) as exc:
            log.warning("could not read the bug frame at %.1fs: %s", plan.bug_time, exc)
            return None
        (out_dir / "reference_bug.png").write_bytes(frame)
        return frame


def _bug_status(verdict: Verdict) -> Reproduced:
    if verdict.passed:
        return "yes"
    if verdict.status in ("not_done", "wrong_action"):
        return "no"
    return "uncertain"


def _rmdir_quiet(path: Path) -> None:
    try:
        for child in path.iterdir():
            child.unlink()
        path.rmdir()
    except OSError:
        pass

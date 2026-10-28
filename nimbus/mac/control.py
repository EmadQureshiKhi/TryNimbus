"""Real mouse and keyboard on macOS (Quartz events), plus the frontmost-app context.

Used by the computer-use driver when the person lets Nimbus act ("open DaVinci", "do this step
for me"). Posting events needs Accessibility permission; `accessibility_trusted()` reports it so
the UI can ask before anything is attempted.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

import Quartz
from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication, NSWorkspace

log = logging.getLogger(__name__)

KEYCODES: dict[str, int] = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12,
    "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23,
    "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34,
    "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46,
    ".": 47, "`": 50,
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51, "escape": 53, "esc": 53,
    "forwarddelete": 117, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "left": 123, "arrowleft": 123, "right": 124, "arrowright": 124, "down": 125, "arrowdown": 125,
    "up": 126, "arrowup": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101,
    "f10": 109, "f11": 103, "f12": 111,
}
MODIFIERS: dict[str, int] = {
    "cmd": Quartz.kCGEventFlagMaskCommand, "command": Quartz.kCGEventFlagMaskCommand,
    "meta": Quartz.kCGEventFlagMaskCommand, "super": Quartz.kCGEventFlagMaskCommand,
    "ctrl": Quartz.kCGEventFlagMaskControl, "control": Quartz.kCGEventFlagMaskControl,
    "alt": Quartz.kCGEventFlagMaskAlternate, "option": Quartz.kCGEventFlagMaskAlternate,
    "opt": Quartz.kCGEventFlagMaskAlternate,
    "shift": Quartz.kCGEventFlagMaskShift,
    "fn": Quartz.kCGEventFlagMaskSecondaryFn,
}


def accessibility_trusted(prompt: bool = False) -> bool:
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt

        return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: prompt}))
    except Exception:
        return False


def _post(event) -> None:
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def move(x: float, y: float) -> None:
    _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, (x, y), Quartz.kCGMouseButtonLeft))


def click(x: float, y: float, button: str = "left", count: int = 1) -> None:
    if button == "right":
        down, up, btn = Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp, Quartz.kCGMouseButtonRight
    elif button in ("middle", "wheel"):
        down, up, btn = Quartz.kCGEventOtherMouseDown, Quartz.kCGEventOtherMouseUp, Quartz.kCGMouseButtonCenter
    else:
        down, up, btn = Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp, Quartz.kCGMouseButtonLeft
    move(x, y)
    time.sleep(0.03)
    for n in range(1, count + 1):
        for kind in (down, up):
            ev = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), btn)
            Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, n)
            _post(ev)
        time.sleep(0.04)


def drag(path: list[tuple[float, float]]) -> None:
    if len(path) < 2:
        return
    x0, y0 = path[0]
    move(x0, y0)
    _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, (x0, y0), Quartz.kCGMouseButtonLeft))
    for x, y in path[1:]:
        time.sleep(0.02)
        _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDragged, (x, y), Quartz.kCGMouseButtonLeft))
    x1, y1 = path[-1]
    _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, (x1, y1), Quartz.kCGMouseButtonLeft))


def scroll(x: float, y: float, dx: int, dy: int) -> None:
    move(x, y)
    # The API's positive scroll_y means content moves up (scroll down); Quartz wheel1 is the inverse.
    _post(Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitPixel, 2, int(-dy), int(-dx)))


def keypress(keys: list[str]) -> None:
    flags = 0
    main: list[str] = []
    for key in keys:
        k = key.strip().lower()
        if k in MODIFIERS:
            flags |= MODIFIERS[k]
        else:
            main.append(k)
    if not main:
        raise ValueError(f"no non-modifier key in {keys}")
    for k in main:
        code = KEYCODES.get(k)
        if code is None:
            if len(k) == 1:
                type_text(k)
                continue
            raise ValueError(f"unknown key {k!r}")
        for down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(None, code, down)
            Quartz.CGEventSetFlags(ev, flags)
            _post(ev)
            time.sleep(0.01)


def type_text(text: str) -> None:
    for i in range(0, len(text), 16):  # CGEventKeyboardSetUnicodeString handles short runs reliably
        chunk = text[i : i + 16]
        for down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
            Quartz.CGEventKeyboardSetUnicodeString(ev, len(chunk), chunk)
            _post(ev)
        time.sleep(0.015)


@dataclass
class AppContext:
    name: str
    bundle_id: str
    window_title: str
    pid: int


def _app_behind_us() -> int:
    """pid of the app whose window sits frontmost behind Nimbus's own windows (-1 if none)."""
    try:
        info = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID)
        for w in info or []:  # front to back
            pid = int(w.get("kCGWindowOwnerPID", -1))
            if pid != os.getpid() and int(w.get("kCGWindowLayer", 1)) == 0:
                return pid
    except Exception:
        pass
    return -1


def frontmost_app() -> AppContext:
    """The app the person is working in. When Nimbus's own panel is in front (a typed question, an
    Allow click), that is the app right behind it, not Nimbus."""
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is not None and int(app.processIdentifier()) == os.getpid():
        behind = _app_behind_us()
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(behind) if behind > 0 else None
    name = str(app.localizedName() or "") if app else ""
    bundle = str(app.bundleIdentifier() or "") if app else ""
    pid = int(app.processIdentifier()) if app else -1
    title = ""
    try:
        info = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID)
        for w in info or []:
            if int(w.get("kCGWindowOwnerPID", -1)) == pid and int(w.get("kCGWindowLayer", 1)) == 0:
                title = str(w.get("kCGWindowName") or "")
                break
    except Exception:
        pass
    return AppContext(name, bundle, title, pid)


def activate_app(pid: int) -> bool:
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid) if pid > 0 else None
    return bool(app and app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps))


def give_focus_back(pid: int, timeout: float = 0.8) -> None:
    """If Nimbus holds keyboard focus (its panel just asked for permission), hand it back to `pid` and
    wait until macOS has switched, so the next keystroke lands in the person's app, not in Nimbus."""
    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    if pid <= 0 or front is None or int(front.processIdentifier()) != os.getpid():
        return
    activate_app(pid)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is not None and int(front.processIdentifier()) == pid:
            time.sleep(0.08)  # let the window become key
            return
        time.sleep(0.03)
    log.warning("could not return focus to pid %s", pid)


AGENT_MAX_SIDE = 1600


def compact(cap):
    """The agent's view of a capture: a JPEG at the display's point size (Retina pixels add bytes, not
    detail the agent needs). Meta stores every step for `previous_response_id`, and a Retina PNG of a
    busy page (search results with thumbnails) is over its per-input limit. Coordinates stay correct
    because the returned Capture describes the smaller image."""
    import io
    from dataclasses import replace

    from PIL import Image

    img = Image.open(io.BytesIO(cap.png)).convert("RGB")
    w = min(int(round(cap.display.width)), AGENT_MAX_SIDE) if cap.display.width >= cap.display.height else \
        int(round(cap.display.width * min(1.0, AGENT_MAX_SIDE / cap.display.height)))
    h = max(1, round(w * img.height / img.width))
    if (w, h) != img.size:
        img = img.resize((w, h), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=82)
    return replace(cap, png=out.getvalue(), pixel_width=w, pixel_height=h)


class MacDriver:
    """`nimbus.meta.computer.Driver` for the real Mac: screenshots in pixels, events in points."""

    def __init__(self, capturer, display=None, before_action=None, target_pid: int = -1) -> None:
        from nimbus.mac.screen import display_at, mouse_position

        self.capturer = capturer
        self.target_pid = target_pid  # the app the task is for; keystrokes never go to Nimbus itself
        self.display = display or display_at(*mouse_position())
        self.before_action = before_action  # async callback(global_x, global_y, action) for the buddy
        self._last = None

    async def screenshot(self) -> bytes:
        self._last = await asyncio.to_thread(lambda: compact(self.capturer.capture(self.display)))
        return self._last.png

    def _to_global(self, x: float, y: float) -> tuple[float, float]:
        if self._last is None:
            raise RuntimeError("no screenshot to resolve coordinates against")
        return self._last.pixel_to_global(x, y)

    async def execute(self, action: dict) -> None:
        kind = action.get("type")
        gx = gy = None
        if "x" in action and "y" in action:
            gx, gy = self._to_global(float(action["x"]), float(action["y"]))
        if self.before_action:
            await self.before_action(gx, gy, action)
        if kind in ("keypress", "type"):
            await asyncio.to_thread(give_focus_back, self.target_pid)
        if kind == "click":
            await asyncio.to_thread(click, gx, gy, action.get("button", "left"), 1)
        elif kind == "double_click":
            await asyncio.to_thread(click, gx, gy, "left", 2)
        elif kind == "move":
            await asyncio.to_thread(move, gx, gy)
        elif kind == "drag":
            pts = [self._to_global(float(p["x"]), float(p["y"])) for p in action.get("path", [])]
            await asyncio.to_thread(drag, pts)
        elif kind == "scroll":
            await asyncio.to_thread(scroll, gx, gy, int(action.get("scroll_x", 0)), int(action.get("scroll_y", 0)))
        elif kind == "keypress":
            await asyncio.to_thread(keypress, list(action.get("keys", [])))
        elif kind == "type":
            await asyncio.to_thread(type_text, str(action.get("text", "")))
        elif kind == "wait":
            await asyncio.sleep(1.0)
        elif kind == "screenshot":
            pass
        else:
            raise ValueError(f"unsupported action {kind!r}")
        await asyncio.sleep(0.22 if kind in ("click", "double_click", "keypress") else 0.1)

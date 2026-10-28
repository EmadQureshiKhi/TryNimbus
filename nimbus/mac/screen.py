"""Displays, the mouse, and screen capture that leaves Nimbus out of its own screenshots.

Coordinates: everything Nimbus stores is in Quartz global display space, in points, origin at the
top-left of the main display, y growing downward. Qt uses the same space for window geometry, so
an overlay can draw at a captured point without conversion. Screenshots are in pixels (2x on
Retina); `Capture` maps between the two.

Capture never hides the overlay. It asks the window server for an image of every on-screen
window except this process's, so the buddy stays visible to screen recorders (the demo video
needs it) while the model never sees Nimbus's own drawings. If that call fails, the fallback
briefly fades Nimbus's windows out and grabs the whole display.
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

import Quartz
from AppKit import NSBitmapImageRep, NSPNGFileType, NSScreen

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Display:
    index: int
    display_id: int
    x: float  # global points, top-left origin
    y: float
    width: float
    height: float
    scale: float
    is_main: bool

    def contains(self, gx: float, gy: float) -> bool:
        return self.x <= gx < self.x + self.width and self.y <= gy < self.y + self.height


@dataclass
class Capture:
    display: Display
    png: bytes
    pixel_width: int
    pixel_height: int
    taken_at: float

    def pixel_to_global(self, px: float, py: float) -> tuple[float, float]:
        sx = self.display.width / self.pixel_width
        sy = self.display.height / self.pixel_height
        return self.display.x + px * sx, self.display.y + py * sy

    def norm_to_global(self, nx: float, ny: float) -> tuple[float, float]:
        return (self.display.x + max(0.0, min(1000.0, nx)) * self.display.width / 1000,
                self.display.y + max(0.0, min(1000.0, ny)) * self.display.height / 1000)

    def global_to_pixel(self, gx: float, gy: float) -> tuple[float, float]:
        return ((gx - self.display.x) * self.pixel_width / self.display.width,
                (gy - self.display.y) * self.pixel_height / self.display.height)


def displays() -> list[Display]:
    err, ids, count = Quartz.CGGetActiveDisplayList(16, None, None)
    if err:
        raise RuntimeError(f"CGGetActiveDisplayList failed: {err}")
    scales = {}
    for screen in NSScreen.screens():
        num = screen.deviceDescription().get("NSScreenNumber")
        if num is not None:
            scales[int(num)] = float(screen.backingScaleFactor())
    main = Quartz.CGMainDisplayID()
    result = []
    for i, did in enumerate(ids[:count]):
        b = Quartz.CGDisplayBounds(did)
        pixel_w = Quartz.CGDisplayPixelsWide(did)
        scale = scales.get(int(did)) or (pixel_w / b.size.width if b.size.width else 1.0)
        result.append(Display(i, int(did), b.origin.x, b.origin.y, b.size.width, b.size.height, scale, did == main))
    result.sort(key=lambda d: (not d.is_main, d.x, d.y))
    return [Display(i, d.display_id, d.x, d.y, d.width, d.height, d.scale, d.is_main) for i, d in enumerate(result)]


def mouse_position() -> tuple[float, float]:
    loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return float(loc.x), float(loc.y)


def display_at(gx: float, gy: float, all_displays: list[Display] | None = None) -> Display:
    ds = all_displays or displays()
    for d in ds:
        if d.contains(gx, gy):
            return d
    return min(ds, key=lambda d: abs(d.x + d.width / 2 - gx) + abs(d.y + d.height / 2 - gy))


def _png(cg_image) -> bytes:
    rep = NSBitmapImageRep.alloc().initWithCGImage_(cg_image)
    data = rep.representationUsingType_properties_(NSPNGFileType, {})
    return bytes(data)


def _foreign_window_ids() -> list[int]:
    pid = os.getpid()
    info = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
    return [int(w["kCGWindowNumber"]) for w in info if int(w.get("kCGWindowOwnerPID", -1)) != pid]


class ScreenCapturer:
    """Grabs a display without Nimbus's own windows in it."""

    def __init__(self, hide_own_windows: Callable[[bool], None] | None = None) -> None:
        self._hide = hide_own_windows

    def capture(self, display: Display | None = None) -> Capture:
        display = display or display_at(*mouse_position())
        bounds = Quartz.CGRectMake(display.x, display.y, display.width, display.height)
        image = None
        try:
            ids = _foreign_window_ids()
            image = Quartz.CGWindowListCreateImageFromArray(bounds, ids, Quartz.kCGWindowImageDefault)
        except Exception as exc:
            log.warning("window-list capture failed (%s); falling back to hide-and-grab", exc)
        if image is None or Quartz.CGImageGetWidth(image) == 0:
            image = self._hide_and_grab(display)
        if image is None:
            raise PermissionError(
                "macOS refused the screenshot. Give your terminal (or Nimbus) Screen Recording permission in "
                "System Settings > Privacy & Security > Screen Recording, then restart Nimbus."
            )
        w, h = Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image)
        return Capture(display, _png(image), int(w), int(h), time.time())

    def _hide_and_grab(self, display: Display):
        if self._hide:
            self._hide(True)
            time.sleep(0.06)  # let the window server composite a frame without us
        try:
            return Quartz.CGDisplayCreateImage(display.display_id)
        finally:
            if self._hide:
                self._hide(False)

    def capture_all(self) -> list[Capture]:
        return [self.capture(d) for d in displays()]


def has_screen_recording_permission() -> bool:
    try:
        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except AttributeError:
        return True


def request_screen_recording_permission() -> bool:
    try:
        return bool(Quartz.CGRequestScreenCaptureAccess())
    except AttributeError:
        return True

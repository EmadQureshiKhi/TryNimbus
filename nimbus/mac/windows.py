"""NSWindow tweaks Qt cannot express: float above every app and Space, ignore clicks, no Dock icon."""
from __future__ import annotations

import logging

import objc
from AppKit import (
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorIgnoresCycle,
    NSWindowCollectionBehaviorStationary,
)

log = logging.getLogger(__name__)

# Above normal windows, the Dock and menus, below the screensaver; same band other cursor
# companions use so the buddy is never hidden behind a menu it is pointing into.
OVERLAY_LEVEL = 1000 - 1   # NSScreenSaverWindowLevel - 1
PANEL_LEVEL = 101          # NSPopUpMenuWindowLevel


def _native() -> bool:
    """Only the Cocoa platform plugin backs a QWidget with an NSView; anything else (offscreen
    rendering, tests) hands back an id that must never be dereferenced as an Objective-C object."""
    from PyQt6.QtGui import QGuiApplication

    return QGuiApplication.platformName() == "cocoa"


def nswindow(widget):
    if not _native():
        raise RuntimeError("not running on the Cocoa platform")
    view = objc.objc_object(c_void_p=int(widget.winId()))
    return view.window()


def make_overlay(widget, *, click_through: bool = True, level: int = OVERLAY_LEVEL) -> None:
    if not _native():
        return
    try:
        win = nswindow(widget)
        win.setLevel_(level)
        win.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorIgnoresCycle
        )
        win.setIgnoresMouseEvents_(click_through)
        win.setHasShadow_(False)
        win.setOpaque_(False)
        win.setHidesOnDeactivate_(False)
    except Exception as exc:  # never fatal: Qt's own flags still give a usable window
        log.warning("could not configure NSWindow: %s", exc)


def set_alpha(widget, alpha: float) -> None:
    if not _native():
        widget.setWindowOpacity(alpha)
        return
    try:
        nswindow(widget).setAlphaValue_(alpha)
    except Exception:
        pass


def become_accessory_app() -> None:
    """No Dock icon and no app menu: Nimbus lives on the screen edge, not in the Dock."""
    try:
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    except Exception as exc:
        log.warning("could not switch to accessory activation policy: %s", exc)


def activate() -> None:
    try:
        NSApp().activateIgnoringOtherApps_(True)
    except Exception:
        pass

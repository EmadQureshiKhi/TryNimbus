"""The buddy's drawings must never overlap each other, cover what they explain, or leave the screen.
Painted offscreen with Qt, checked with `layout_problems`."""
from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPointF, QRectF  # noqa: E402
from PyQt6.QtGui import QImage, QPainter  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from nimbus.ui.overlay import Mark, Overlay, layout_problems  # noqa: E402

W, H = 1440.0, 900.0


@pytest.fixture(scope="module")
def qt():
    return QApplication.instance() or QApplication([])


def paint(overlay: Overlay) -> dict:
    img = QImage(int(W), int(H), QImage.Format.Format_ARGB32_Premultiplied)
    p = QPainter(img)
    overlay.paint(p, QRectF(0, 0, W, H))
    p.end()
    return overlay.drawn


def scene(qt, marks: list[Mark], buddy: tuple[float, float], caption: str) -> dict:
    ov = Overlay()
    ov.windows = []  # no real windows needed to lay out and paint
    for m in marks:
        m.born = time.monotonic() - 2
        ov.add_mark(m)
    ov.follow = False
    ov.buddy = QPointF(*buddy)
    ov.set_caption(caption)
    return paint(ov)


LONG = "Set each part to zero and you get x equals two or x equals three, and that is the whole answer."

CASES = {
    "worksheet under an equation": (
        [Mark("box", 330, 680, box=(160, 640, 760, 720), label="Problem 2"),
         Mark("note", 480, 690, note="find a, b: a+b = -5, ab = 6", step=2),
         Mark("note", 600, 690, note="(x-2)(x-3) = 0", step=3),
         Mark("note", 700, 690, note="x = 2 or x = 3", step=4)], (700, 690), LONG),
    "notes crowded at the bottom": (
        [Mark("note", 300 + i * 90, 860, note=f"line {i}: a longer explanation of this term", step=i + 1)
         for i in range(5)], (660, 860), LONG),
    "ring in the top right corner": (
        [Mark("ring", 1420, 14, label="Share", step=1)], (1420, 14), LONG),
    "ring in the bottom left corner": (
        [Mark("ring", 18, 884, label="Settings")], (18, 884), LONG),
    "three labels close together": (
        [Mark("ring", 600, 400, label="Bold", step=1), Mark("ring", 640, 400, label="Italic", step=2),
         Mark("ring", 680, 400, label="Underline", step=3)], (680, 400), "These three format your text."),
    "box filling most of the screen": (
        [Mark("box", 720, 450, box=(40, 60, 1400, 820), label="Your code")], (720, 450), LONG),
}


@pytest.mark.parametrize("name", list(CASES))
def test_layout_is_clean(qt, name):
    marks, buddy, caption = CASES[name]
    drawn = scene(qt, marks, buddy, caption)
    assert layout_problems(drawn) == [], name


def test_dismiss_chip_sits_beside_the_answer_never_on_it():
    from PyQt6.QtCore import QRectF

    from nimbus.ui.overlay import CHIP, dismiss_anchor

    area = QRectF(0, 0, 1440, 900)
    drawn = [QRectF(200, 300, 300, 60), QRectF(220, 380, 280, 120), QRectF(180, 280, 44, 44)]
    spot = dismiss_anchor(drawn, area)
    chip = QRectF(spot.x() - CHIP / 2, spot.y() - CHIP / 2, CHIP, CHIP)
    assert not any(chip.intersects(r) for r in drawn)
    assert chip.left() > 500 and abs(chip.top() - 280) < 1  # top-right corner of the whole answer

    right_edge = [QRectF(1200, 100, 236, 80)]  # reaches the screen edge: mirrored to the left
    spot = dismiss_anchor(right_edge, area)
    assert spot.x() + CHIP / 2 < 1200
    assert dismiss_anchor([], area) is None


def test_cancelled_push_to_talk_keeps_no_audio():
    from nimbus.meta.voice import PushToTalkSession

    session = PushToTalkSession(client=None)
    session.feed(b"\x01\x00" * 4000)
    session.cancel()
    session.feed(b"\x01\x00" * 4000)
    assert len(session._pcm) == 0


def test_panel_moves_off_the_control_shadow_points_at(qt):
    from PyQt6.QtCore import QRect

    from nimbus.config import Settings
    from nimbus.ui.panel import Panel, _body

    panel = Panel(Settings())
    panel.place_tab()
    panel.open()
    home = panel._target_rect()
    send = QRect(home.center().x(), home.center().y(), 40, 40)  # a control under the docked panel
    panel.keep_clear(send)
    moved = panel._target_rect()
    assert panel.is_open and moved.x() < home.x() and not _body(moved).intersects(send)

    screen = panel._screen_rect()
    panel.keep_clear(QRect(screen.left(), home.y() + 40, screen.width(), 40))  # covered on both edges
    assert not panel.is_open

    panel.keep_clear(None)
    assert panel._target_rect() == home


def test_caption_never_hides_under_the_panel(qt):
    o = Overlay()
    o.buddy = QPointF(1000, 300)
    panel = QRectF(1028, 10, 404, 690)
    o.own_windows = lambda: [panel]
    rect = o._caption_rect(220, 40, QRectF(0, 0, 1440, 900), o.own_windows())
    assert not rect.intersects(panel)

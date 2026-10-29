"""The on-screen layer: the buddy cursor, its flights, and everything it draws to teach.

One transparent, click-through window per display, floating above every app and Space. A single
controller owns the state (buddy position, marks, caption) in global points and repaints the
windows at 60 fps. The buddy moves like a companion: a bezier flight with a mid-flight scale pulse,
a spring follow behind the real cursor, and a slow idle breath.

Marks are what the buddy leaves on screen while teaching:
- ring: a pulsing circle on a point, with a label pill
- box: a rounded highlight around a control, with a label pill
- note: a card with a numbered step and text, joined to its point by a leader line (math steps,
  explanations); notes never overlap each other
"""
from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from PyQt6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor, QCursor, QFontMetricsF, QGuiApplication, QPainter, QPen, QPolygonF,
    QRadialGradient,
)
from PyQt6.QtWidgets import QWidget

from nimbus.mac import windows as macwin
from nimbus.ui import theme

FOLLOW_OFFSET = (10.0, 9.0)
BUDDY_SCALE = 0.66  # about the size of the system arrow, so it reads as a companion, not a second cursor
FOLLOW_LERP = 0.18
CAPTION_MAX_W = 340.0
NOTE_MAX_W = 300.0


def _qc(rgb: tuple[int, int, int], alpha: int = 255) -> QColor:
    return QColor(rgb[0], rgb[1], rgb[2], max(0, min(255, int(alpha))))


def _tint(rgb, amount):
    return tuple(int(round(c + (255 - c) * amount)) for c in rgb)


def _shade(rgb, amount):
    return tuple(int(round(c * (1 - amount))) for c in rgb)


def _bezier(t, p0, p1, p2):
    u = 1 - t
    return (u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0], u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1])


def _smoothstep(t: float) -> float:
    return t * t * (3 - 2 * t)


def _ease_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


@dataclass
class Mark:
    kind: str  # ring | box | note
    x: float
    y: float
    box: tuple[float, float, float, float] | None = None
    label: str = ""
    note: str = ""
    step: int | None = None
    color: tuple[int, int, int] = theme.ACCENT
    born: float = field(default_factory=time.monotonic)
    ttl: float = 45.0
    below: float | None = None  # note: where the text block under the anchor ends (global y)


STATE_COLORS = {
    "idle": theme.ACCENT,
    "pointing": theme.ACCENT,
    "listening": theme.LISTENING,
    "thinking": theme.THINKING,
    "acting": theme.ACCENT_DEEP,
}


class OverlayWindow(QWidget):
    def __init__(self, controller: "Overlay", screen) -> None:
        super().__init__(None)
        self.controller = controller
        self.screen_ref = screen
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool | Qt.WindowType.WindowTransparentForInput
                            | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setGeometry(screen.geometry())

    def showEvent(self, event) -> None:
        super().showEvent(event)
        macwin.make_overlay(self, click_through=True)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        origin = self.geometry().topLeft()
        p.translate(-origin.x(), -origin.y())  # draw in global points
        self.controller.paint(p, QRectF(self.geometry()))
        p.end()


CHIP = 22.0  # the close chip's diameter


def dismiss_anchor(content: list[QRectF], area: QRectF) -> QPointF | None:
    """Centre for the close chip: just right of the answer's top-right corner, so it can never sit on
    anything the answer drew; mirrored to the left when the answer reaches the screen's right edge."""
    if not content:
        return None
    box = content[0]
    for r in content[1:]:
        box = box.united(r)
    gap = 8 + CHIP / 2
    x = box.right() + gap
    if x + CHIP / 2 > area.right() - 4:
        x = box.left() - gap
    y = max(area.top() + CHIP / 2 + 4, min(box.top() + CHIP / 2, area.bottom() - CHIP / 2 - 4))
    return QPointF(max(area.left() + CHIP / 2 + 4, x), y)


class DismissChip(QWidget):
    """A small × beside what the buddy drew. The overlay itself is click-through, so this is its own
    tiny clickable window; one click clears the whole answer (Show again brings it back)."""

    clicked = pyqtSignal()

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(int(CHIP) + 8, int(CHIP) + 8)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Clear this answer (Esc)")
        self._hover = False
        self.center = QPointF()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        macwin.make_overlay(self, click_through=False, level=macwin.OVERLAY_LEVEL + 1)

    def place(self, center: QPointF) -> None:
        if self.isVisible():  # glide as the answer grows step by step, never jump
            center = QPointF(self.center.x() + (center.x() - self.center.x()) * 0.3,
                             self.center.y() + (center.y() - self.center.y()) * 0.3)
        self.center = center
        self.move(int(center.x() - self.width() / 2), int(center.y() - self.height() / 2))

    def enterEvent(self, e) -> None:
        self._hover = True
        self.update()

    def leaveEvent(self, e) -> None:
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, e) -> None:
        if self.rect().contains(e.position().toPoint()):
            self.clicked.emit()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QPointF(self.width() / 2, self.height() / 2)
        r = CHIP / 2
        p.setPen(QPen(_qc(theme.ACCENT if self._hover else (255, 255, 255), 150 if self._hover else 46), 1.0))
        p.setBrush(_qc((24, 19, 16), 235 if self._hover else 190))
        p.drawEllipse(c, r, r)
        p.setPen(QPen(_qc((255, 255, 255), 235 if self._hover else 175), 1.5, cap=Qt.PenCapStyle.RoundCap))
        a = 3.6
        p.drawLine(QPointF(c.x() - a, c.y() - a), QPointF(c.x() + a, c.y() + a))
        p.drawLine(QPointF(c.x() - a, c.y() + a), QPointF(c.x() + a, c.y() - a))
        p.end()


class Overlay(QObject):
    landed = pyqtSignal()
    dismissed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.windows: list[OverlayWindow] = []
        pos = QCursor.pos()
        self.buddy = QPointF(pos.x() + FOLLOW_OFFSET[0], pos.y() + FOLLOW_OFFSET[1])
        self.buddy_visible = True
        self.follow = True
        self.state = "idle"
        self.state_since = time.monotonic()
        self.marks: list[Mark] = []
        self.caption = ""
        self.caption_since = 0.0
        self.audio_level = 0.0
        self.toast_text = ""
        self.toast_kind = "info"
        self.toast_until = 0.0
        self.progress_text = ""
        self.exclusion: Callable[[float, float], bool] | None = None  # e.g. the cursor is over our panel
        self.own_windows: Callable[[], list[QRectF]] | None = None  # Nimbus's panel: captions stay off it
        self.drawn: dict = {}  # rectangles painted on the last frame, for layout checks
        self._flight: tuple[float, float, tuple, tuple, tuple] | None = None  # start, duration, p0, p1, p2
        self._trail: deque[QPointF] = deque(maxlen=10)
        self._on_land: list[Callable[[], None]] = []
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        self.chip = DismissChip()
        self.chip.clicked.connect(self.dismissed.emit)
        self._content: dict[tuple, list[QRectF]] = {}  # per screen: what the answer drew last frame
        self._build_windows()
        QGuiApplication.instance().screenAdded.connect(lambda _s: self._build_windows())
        QGuiApplication.instance().screenRemoved.connect(lambda _s: self._build_windows())

    # ------------------------------------------------------------ lifecycle

    def _build_windows(self) -> None:
        for w in self.windows:
            w.close()
            w.deleteLater()
        self.windows = [OverlayWindow(self, s) for s in QGuiApplication.screens()]

    def show(self) -> None:
        for w in self.windows:
            w.show()
        self._timer.start(16)

    def hide(self) -> None:
        for w in self.windows:
            w.hide()
        self.chip.hide()
        self._timer.stop()

    def set_alpha(self, alpha: float) -> None:
        for w in self.windows:
            macwin.set_alpha(w, alpha)

    # ------------------------------------------------------------ API (main thread)

    def set_state(self, state: str) -> None:
        if state != self.state:
            self.state, self.state_since = state, time.monotonic()

    def set_buddy_visible(self, visible: bool) -> None:
        self.buddy_visible = visible

    def fly_to(self, gx: float, gy: float, on_land: Callable[[], None] | None = None, *, quick: bool = False) -> None:
        """Fly the buddy's tip to a global point and stay there until `release()`. `quick` is for
        acting: a short hop that keeps pace with the clicks instead of a teaching flight."""
        self.follow = False
        p0 = (self.buddy.x(), self.buddy.y())
        p2 = (float(gx), float(gy))
        dist = math.hypot(p2[0] - p0[0], p2[1] - p0[1])
        if dist < 2:
            self.buddy = QPointF(*p2)
            if on_land:
                on_land()
            self.landed.emit()
            return
        # Control point lifted perpendicular to the path: a natural arc, not a straight slide.
        mx, my = (p0[0] + p2[0]) / 2, (p0[1] + p2[1]) / 2
        lift = min(dist * 0.25, 140)
        nx, ny = -(p2[1] - p0[1]) / dist, (p2[0] - p0[0]) / dist
        if ny > 0:
            nx, ny = -nx, -ny  # always arc upward
        p1 = (mx + nx * lift, my + ny * lift)
        duration = max(0.16, min(dist / 2400.0, 0.38)) if quick else max(0.5, min(dist / 900.0, 1.2))
        self._flight = (time.monotonic(), duration, p0, p1, p2)
        if on_land:
            self._on_land.append(on_land)
        self.set_state("pointing")

    def release(self) -> None:
        """Return to following the real mouse."""
        self._flight = None
        self.follow = True
        if self.state == "pointing":
            self.set_state("idle")

    def add_mark(self, mark: Mark) -> None:
        self.marks.append(mark)

    def clear_marks(self) -> None:
        self.marks.clear()

    def set_caption(self, text: str) -> None:
        self.caption = text.strip()
        self.caption_since = time.monotonic()

    def set_progress(self, text: str) -> None:
        self.progress_text = text

    def set_audio_level(self, level: float) -> None:
        self.audio_level = max(0.0, min(1.0, level))

    def toast(self, text: str, kind: str = "info", seconds: float = 5.0) -> None:
        self.toast_text, self.toast_kind, self.toast_until = text, kind, time.monotonic() + seconds

    def buddy_global(self) -> tuple[float, float]:
        return self.buddy.x(), self.buddy.y()

    # ------------------------------------------------------------ animation

    def _tick(self) -> None:
        now = time.monotonic()
        if self._flight:
            start, duration, p0, p1, p2 = self._flight
            t = min(1.0, (now - start) / duration)
            x, y = _bezier(_smoothstep(t), p0, p1, p2)
            self.buddy = QPointF(x, y)
            self._trail.append(QPointF(x, y))
            if t >= 1.0:
                self._flight = None
                self._trail.clear()
                callbacks, self._on_land = self._on_land, []
                for cb in callbacks:
                    cb()
                self.landed.emit()
        elif self.follow:
            pos = QCursor.pos()
            tx, ty = pos.x() + FOLLOW_OFFSET[0], pos.y() + FOLLOW_OFFSET[1]
            self.buddy = QPointF(self.buddy.x() + (tx - self.buddy.x()) * FOLLOW_LERP,
                                 self.buddy.y() + (ty - self.buddy.y()) * FOLLOW_LERP)
        self.marks = [m for m in self.marks if now - m.born < m.ttl]
        for w in self.windows:
            w.update()
        self._sync_chip()

    def _sync_chip(self) -> None:
        spot = None
        if self.marks:
            for (x, y, w, h), rects in self._content.items():
                spot = dismiss_anchor(rects, QRectF(x, y, w, h))
                if spot is not None:
                    break
        if spot is None:
            self.chip.hide()
            return
        self.chip.place(spot)
        if not self.chip.isVisible():
            self.chip.show()

    @property
    def flying(self) -> bool:
        return self._flight is not None

    def _flight_scale(self) -> float:
        if not self._flight:
            return 1.025 + math.sin((time.monotonic() - self.state_since) * math.tau / 3.0) * 0.025
        start, duration, *_ = self._flight
        t = min(1.0, (time.monotonic() - start) / duration)
        return 1.0 + math.sin(t * math.pi) * 0.3

    # ------------------------------------------------------------ painting

    def paint(self, p: QPainter, area: QRectF) -> None:
        visible = [m for m in self.marks if area.contains(QPointF(m.x, m.y)) or (m.box and area.intersects(QRectF(
            QPointF(m.box[0], m.box[1]), QPointF(m.box[2], m.box[3]))))]
        layout = self._layout_notes([m for m in visible if m.kind == "note" and m.note], area)
        obstacles = [rect for rect, *_ in layout.values()]
        for mark in visible:  # the caption must not cover what is being explained either
            if mark.box:
                obstacles.append(QRectF(QPointF(mark.box[0], mark.box[1]), QPointF(mark.box[2], mark.box[3])))
            else:
                obstacles.append(QRectF(mark.x - 26, mark.y - 26, 52, 52))
        self.drawn = {"area": QRectF(area), "notes": [r for r, *_ in layout.values()], "pills": [], "caption": None,
                      "anchors": [(m.x, m.y) for m in visible],
                      "marks": [QRectF(QPointF(m.box[0], m.box[1]), QPointF(m.box[2], m.box[3])) if m.box
                                else QRectF(m.x - 22, m.y - 22, 44, 44) for m in visible if m.kind != "note"]}
        if len(layout) == 1:  # a single note gets a leader line; several are matched by numbered badges
            only = next(m for m in visible if id(m) in layout)
            self._paint_leader(p, only, layout[id(only)][0])
        for mark in visible:
            self._paint_mark(p, mark, area, layout, obstacles)
        cursor = QCursor.pos()
        hidden_by_ui = self.follow and self.exclusion is not None and self.exclusion(cursor.x(), cursor.y())
        key = (area.x(), area.y(), area.width(), area.height())
        self._content[key] = self.drawn["notes"] + self.drawn["pills"] + self.drawn["marks"]
        if self.buddy_visible and not hidden_by_ui and area.contains(self.buddy):
            self._paint_buddy(p)
            own = self.own_windows() if self.own_windows else []
            self.drawn["caption"] = self._paint_caption(p, area, obstacles + own)
        if self.toast_text and time.monotonic() < self.toast_until and area.contains(self.buddy):
            self._paint_toast(p, area)

    def _accent(self) -> tuple[int, int, int]:
        return STATE_COLORS.get(self.state, theme.ACCENT)

    def _paint_buddy(self, p: QPainter) -> None:
        accent = self._accent()
        px, py = self.buddy.x(), self.buddy.y()
        if self.state == "listening":
            self._paint_waveform(p, px, py, accent)
            return
        for i, pt in enumerate(list(self._trail)[:-1]):
            frac = (i + 1) / max(1, len(self._trail))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_qc(accent, 8 + frac * 30))
            p.drawEllipse(pt, 1.5 + frac * 2.5, 1.5 + frac * 2.5)
        scale = self._flight_scale() * BUDDY_SCALE
        p.save()
        p.translate(px, py)
        p.scale(scale, scale)
        poly = QPolygonF([QPointF(dx, dy) for dx, dy in theme.CURSOR_VERTICES])
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_qc((0, 0, 0), 45))
        p.drawPolygon(poly.translated(1.2, 2.0))  # a tight drop shadow in the arrow's own shape
        fill = QRadialGradient(QPointF(4, 8), 24)
        fill.setColorAt(0.0, _qc(_tint(accent, 0.82), 245))
        fill.setColorAt(0.42, _qc(accent, 235))
        fill.setColorAt(1.0, _qc(_shade(accent, 0.22), 235))
        p.setBrush(fill)
        p.setPen(QPen(_qc((0, 0, 0), 190), 1.4))
        p.drawPolygon(poly)
        p.restore()
        if self.state in ("thinking", "acting"):  # busy until the answer is spoken
            self._paint_spinner(p, px + 22, py + 22, accent)

    def _paint_spinner(self, p: QPainter, cx: float, cy: float, accent) -> None:
        angle = (time.monotonic() - self.state_since) * 360 / 0.9
        r = 6.0
        for i in range(12):
            a = math.radians(angle - i * 9)
            alpha = int(255 * (1 - i / 12) ** 1.7)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_qc(accent, alpha))
            p.drawEllipse(QPointF(cx + r * math.cos(a), cy + r * math.sin(a)), 1.7, 1.7)

    def _paint_waveform(self, p: QPainter, px: float, py: float, accent) -> None:
        profile = (0.4, 0.7, 1.0, 0.7, 0.4)
        phase = time.monotonic() - self.state_since
        level = pow(min(max(self.audio_level - 0.008, 0) * 2.85, 1.0), 0.76)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_qc((0, 0, 0), 150))
        p.drawRoundedRect(QRectF(px - 4, py - 4, 44, 30), 15, 15)
        lift = int(20 + 55 * level)
        col = tuple(min(255, c + lift) for c in accent)
        for i, prof in enumerate(profile):
            h = 3 + level * 10 * prof + (math.sin(phase * 3.6 + i * 0.35) + 1) / 2 * 1.5
            x = px + 4 + i * 6.5
            p.setBrush(_qc(col, 255))
            p.drawRoundedRect(QRectF(x, py + 11 - h, 3.2, h * 2), 1.6, 1.6)

    def _pill(self, p: QPainter, candidates: list[QPointF], text: str, color, area: QRectF,
              avoid: list[QRectF] | None = None, own: QRectF | None = None) -> QRectF:
        """Place a label at the first candidate spot that is on screen and clear of every other label,
        note card and mark (its own mark excepted); stack below the mark if all of them are taken."""
        font = theme.font(12, "semibold")
        fm = QFontMetricsF(font)
        w, h = fm.horizontalAdvance(text) + 18, fm.height() + 8
        blockers = [o for o in (avoid or []) if own is None or o != own]

        def fit(pt: QPointF) -> QRectF:
            x = max(area.left() + 4, min(pt.x(), area.right() - w - 4))
            y = max(area.top() + 4, min(pt.y(), area.bottom() - h - 4))
            return QRectF(x, y, w, h)

        def clear(r: QRectF) -> bool:
            return not any(r.intersects(o.adjusted(-3, -3, 3, 3)) for o in blockers)

        rect = None
        for pt in candidates:
            trial = fit(pt)
            if clear(trial):
                rect = trial
                break
        if rect is None:
            base = fit(candidates[-1])
            for k in range(1, 12):
                trial = base.translated(0, k * (h + 4))
                if trial.bottom() > area.bottom() - 4:
                    trial = base.translated(0, -k * (h + 4))
                if clear(fit(trial.topLeft())):
                    rect = fit(trial.topLeft())
                    break
        rect = rect or fit(candidates[0])
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_qc((0, 0, 0), 60))
        p.drawRoundedRect(rect.translated(0, 2), h / 2, h / 2)
        p.setBrush(_qc(color, 240))
        p.drawRoundedRect(rect, h / 2, h / 2)
        p.setFont(font)
        p.setPen(_qc((18, 12, 8)))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        return rect

    def _badge(self, p: QPainter, cx: float, cy: float, n: int, color) -> None:
        p.setPen(QPen(_qc((0, 0, 0), 200), 1.5))
        p.setBrush(_qc(color))
        p.drawEllipse(QPointF(cx, cy), 11, 11)
        p.setFont(theme.font(11, "bold"))
        p.setPen(_qc((18, 12, 8)))
        p.drawText(QRectF(cx - 11, cy - 11, 22, 22), Qt.AlignmentFlag.AlignCenter, str(n))

    def _paint_mark(self, p: QPainter, m: Mark, area: QRectF, layout: dict, obstacles: list[QRectF]) -> None:
        age = time.monotonic() - m.born
        appear = _ease_out(age / 0.25)
        fade = 1.0 if age < m.ttl - 0.6 else max(0.0, (m.ttl - age) / 0.6)
        alpha = appear * fade
        color = m.color
        if m.box:
            x0, y0, x1, y1 = m.box
            rect = QRectF(QPointF(x0, y0), QPointF(x1, y1)).adjusted(-4, -4, 4, 4)
            p.setBrush(_qc(color, 28 * alpha))
            p.setPen(QPen(_qc(color, 235 * alpha), 2.5))
            p.drawRoundedRect(rect, 7, 7)
            anchor = QPointF(rect.left(), rect.bottom() + 6)
        elif m.kind == "note":
            p.setOpacity(alpha)
            if m.step is not None:  # numbered anchor, matching the "STEP n" card
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_qc(color, 60))
                p.drawEllipse(QPointF(m.x, m.y), 14, 14)
                self._badge(p, m.x, m.y, m.step, color)
            else:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_qc(color, 70))
                p.drawEllipse(QPointF(m.x, m.y), 7, 7)
                p.setBrush(_qc(color))
                p.drawEllipse(QPointF(m.x, m.y), 3.5, 3.5)
            p.setOpacity(1.0)
            anchor = QPointF(m.x, m.y)
        else:
            pulse = 1 + 0.045 * math.sin(age * 2.6)  # a slow, shallow breath: alive, not distracting
            r = 20 * pulse * (0.6 + 0.4 * appear)
            p.setBrush(_qc(color, 22 * alpha))
            p.setPen(QPen(_qc(color, 235 * alpha), 2.5))
            p.drawEllipse(QPointF(m.x, m.y), r, r)
            anchor = QPointF(m.x + 24, m.y - 40)  # preferred: above-right, clear of the buddy and its caption
        if m.step is not None and m.kind != "note":
            bx = (m.box[0] - 4) if m.box else m.x - 20
            by = (m.box[1] - 4) if m.box else m.y - 20
            self._badge(p, bx, by, m.step, color)
        if m.label and m.kind != "note":
            p.setOpacity(alpha)
            if m.box:
                x0, y0, x1, y1 = m.box
                own = QRectF(QPointF(x0, y0), QPointF(x1, y1))
                spots = [anchor, QPointF(x0 - 4, y0 - 38), QPointF(x1 + 10, y0), QPointF(x0 - 4, y1 + 40)]
            else:
                own = QRectF(m.x - 26, m.y - 26, 52, 52)
                spots = [anchor, QPointF(m.x + 24, m.y + 22), QPointF(m.x - 24 - 140, m.y - 40),
                         QPointF(m.x - 24 - 140, m.y + 22), QPointF(m.x + 30, m.y - 12), QPointF(m.x - 10, m.y + 30)]
            text = f"{m.step}.  {m.label}" if m.step is not None else m.label  # ties a moved label to its badge
            pill = self._pill(p, spots, text, color, area, avoid=obstacles, own=own)
            near = QPointF(max(pill.left(), min(m.x, pill.right())), max(pill.top(), min(m.y, pill.bottom())))
            if math.hypot(near.x() - m.x, near.y() - m.y) > 46:  # displaced: a faint connector back to the mark
                p.setPen(QPen(_qc(color, 120), 1.2, Qt.PenStyle.DotLine))
                p.drawLine(QPointF(m.x, m.y), near)
            obstacles.append(pill)
            self.drawn.setdefault("pills", []).append(pill)
            p.setOpacity(1.0)
        if m.kind == "note" and id(m) in layout:
            p.setOpacity(alpha)
            self._paint_note(p, m, *layout[id(m)])
            p.setOpacity(1.0)

    def _note_size(self, m: Mark) -> tuple[float, float, QRectF, str, float]:
        font, title_font = theme.font(13), theme.font(11, "bold")
        text_rect = QFontMetricsF(font).boundingRect(QRectF(0, 0, NOTE_MAX_W - 28, 1000),
                                                     int(Qt.TextFlag.TextWordWrap), m.note)
        title = (f"STEP {m.step}" if m.step is not None else "") + (f"  {m.label.upper()}" if m.label else "")
        th = QFontMetricsF(title_font).height() + 4 if title.strip() else 0
        w = min(NOTE_MAX_W, max(170, text_rect.width() + 30))
        return w, text_rect.height() + th + 22, text_rect, title.strip(), th

    def _layout_notes(self, notes: list[Mark], area: QRectF) -> dict:
        """Lay notes out like working on a worksheet: a tidy column under the thing they explain, never
        on top of it. If the column does not fit below, it moves to the side of the anchors."""
        if not notes:
            return {}
        notes = sorted(notes, key=lambda m: (m.step if m.step is not None else 0, m.born))
        sizes = {id(m): self._note_size(m) for m in notes}
        xs, ys = [m.x for m in notes], [m.y for m in notes]
        boxes = [m for m in self.marks if m.box]
        content_bottom = max([max(ys)] + [b.box[3] for b in boxes if b.box[1] <= max(ys) <= b.box[3] + 40]
                             + [m.below - 22 for m in notes if m.below is not None])
        col_w = max(w for w, *_ in sizes.values())
        total_h = sum(h for _, h, *_ in sizes.values()) + 10 * (len(notes) - 1)
        left = max(area.left() + 16, min(min(xs) - 24, area.right() - col_w - 16))
        top = content_bottom + 34
        if top + total_h > area.bottom() - 12:  # no room below: a column beside the anchors instead
            left = max(xs) + 70
            if left + col_w > area.right() - 16:
                left = max(area.left() + 16, min(xs) - col_w - 70)
            # Starts level with the first note, like working down a worksheet: starting at the highest
            # anchor put a column over the question text when only the last note pointed up there.
            top = max(area.top() + 30, min(notes[0].y - 20, area.bottom() - total_h - 12))
        layout = {}
        for m in notes:
            w, h, text_rect, title, th = sizes[id(m)]
            layout[id(m)] = (QRectF(left, top, w, h), text_rect, title, th)
            top += h + 10
        return layout

    @staticmethod
    def _leader_end(m: Mark, rect: QRectF) -> QPointF:
        if rect.top() > m.y:  # card below the anchor: enter through the top edge, under the anchor if possible
            return QPointF(max(rect.left() + 18, min(m.x, rect.right() - 18)), rect.top())
        return QPointF(rect.left() if rect.left() > m.x else rect.right(), rect.top() + 18)

    def _paint_leader(self, p: QPainter, m: Mark, rect: QRectF) -> None:
        p.setPen(QPen(_qc(m.color, 170), 1.5, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(m.x, m.y), self._leader_end(m, rect))

    def _paint_note(self, p: QPainter, m: Mark, rect: QRectF, text_rect: QRectF, title: str, th: float) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_qc((0, 0, 0), 70))
        p.drawRoundedRect(rect.translated(0, 3), 12, 12)
        p.setBrush(_qc((20, 17, 15), 245))
        p.setPen(QPen(_qc(m.color, 200), 1.3))
        p.drawRoundedRect(rect, 12, 12)
        p.setBrush(_qc(m.color))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(rect.left(), rect.top() + 10, 3.5, rect.height() - 20), 2, 2)
        y = rect.top() + 11
        if th:
            p.setFont(theme.font(11, "bold"))
            p.setPen(_qc(m.color))
            p.drawText(QRectF(rect.left() + 15, y, rect.width() - 26, th), Qt.AlignmentFlag.AlignLeft, title)
            y += th
        p.setFont(theme.font(13))
        p.setPen(QColor(theme.TEXT_PRIMARY))
        p.drawText(QRectF(rect.left() + 15, y, rect.width() - 26, text_rect.height() + 4),
                   int(Qt.TextFlag.TextWordWrap), m.note)

    def _paint_caption(self, p: QPainter, area: QRectF, obstacles: list[QRectF] | None = None) -> QRectF | None:
        text = self.caption or self.progress_text
        if not text:
            return None
        font = theme.font(13)
        fm = QFontMetricsF(font)
        br = fm.boundingRect(QRectF(0, 0, CAPTION_MAX_W, 600), int(Qt.TextFlag.TextWordWrap), text)
        w, h = br.width() + 26, br.height() + 18
        rect = self._caption_rect(w, h, area, obstacles or [])
        accent = self._accent()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_qc((0, 0, 0), 80))
        p.drawRoundedRect(rect.translated(0, 3), 12, 12)
        p.setBrush(_qc((17, 14, 13), 240))
        p.setPen(QPen(_qc(accent, 150), 1.2))
        p.drawRoundedRect(rect, 12, 12)
        p.setFont(font)
        p.setPen(QColor(theme.TEXT_PRIMARY if self.caption else theme.TEXT_SECONDARY))
        p.drawText(rect.adjusted(13, 9, -13, -9), int(Qt.TextFlag.TextWordWrap), text)
        return rect

    def _caption_rect(self, w: float, h: float, area: QRectF, obstacles: list[QRectF]) -> QRectF:
        """The nearest spot to the buddy where the caption covers nothing it should not: tried on widening
        rings in eight directions, preferring below-right (where people expect it). Boxes that fill most of
        the screen cannot be avoided, so they do not count."""
        big = 0.4 * area.width() * area.height()
        blockers = [o for o in obstacles if o.width() * o.height() < big]
        bx, by = self.buddy.x(), self.buddy.y()
        directions = [(1, 1), (1, -1), (-1, 1), (-1, -1), (1, 0), (-1, 0), (0, 1), (0, -1)]
        for dist in (30, 60, 100, 160, 240, 340, 480):
            for dx, dy in directions:
                x = bx + (dist if dx > 0 else -w - dist if dx < 0 else -w / 2)
                y = by + (dist if dy > 0 else -h - dist if dy < 0 else -h / 2)
                x = max(area.left() + 8, min(x, area.right() - w - 8))
                y = max(area.top() + 8, min(y, area.bottom() - h - 8))
                trial = QRectF(x, y, w, h)
                if not any(trial.intersects(o.adjusted(-6, -6, 6, 6)) for o in blockers):
                    return trial
        return QRectF(max(area.left() + 8, min(bx + 30, area.right() - w - 8)),
                      max(area.top() + 8, min(by + 30, area.bottom() - h - 8)), w, h)

    def _paint_toast(self, p: QPainter, area: QRectF) -> None:
        color = {"error": theme.DANGER, "success": theme.SUCCESS}.get(self.toast_kind, theme.ACCENT)
        font = theme.font(13)
        fm = QFontMetricsF(font)
        br = fm.boundingRect(QRectF(0, 0, 420, 400), int(Qt.TextFlag.TextWordWrap), self.toast_text)
        w, h = br.width() + 30, br.height() + 20
        rect = QRectF(area.center().x() - w / 2, area.top() + 46, w, h)
        p.setPen(QPen(_qc(color, 220), 1.4))
        p.setBrush(_qc((17, 14, 13), 245))
        p.drawRoundedRect(rect, 12, 12)
        p.setFont(font)
        p.setPen(QColor(theme.TEXT_PRIMARY))
        p.drawText(rect.adjusted(15, 10, -15, -10), int(Qt.TextFlag.TextWordWrap), self.toast_text)


def layout_problems(drawn: dict, tolerance: float = 1.0) -> list[str]:
    """What is wrong with a painted frame: overlapping text, text covering a point being explained,
    anything off screen. Empty means the frame is clean."""
    problems: list[str] = []
    area: QRectF | None = drawn.get("area")
    texts = [("note", r) for r in drawn.get("notes", [])] + [("label", r) for r in drawn.get("pills", [])]
    if drawn.get("caption") is not None:
        texts.append(("caption", drawn["caption"]))
    for i, (ka, a) in enumerate(texts):
        if area is not None and not area.adjusted(-tolerance, -tolerance, tolerance, tolerance).contains(a):
            problems.append(f"{ka} off screen at {a.x():.0f},{a.y():.0f}")
        for kb, b in texts[i + 1:]:
            inter = a.intersected(b)
            if inter.width() > tolerance and inter.height() > tolerance:
                problems.append(f"{ka} overlaps {kb} at {inter.x():.0f},{inter.y():.0f}")
        for x, y in drawn.get("anchors", []):
            if ka in ("note", "caption") and a.adjusted(2, 2, -2, -2).contains(QPointF(x, y)):
                problems.append(f"{ka} covers the point it should leave visible at {x:.0f},{y:.0f}")
    cap = drawn.get("caption")
    big = 0.4 * area.width() * area.height() if area is not None else float("inf")
    for m in drawn.get("marks", []):
        if cap is not None and m.width() * m.height() < big:  # a box filling the screen cannot be avoided
            inter = cap.intersected(m)
            if inter.width() > tolerance and inter.height() > tolerance:
                problems.append(f"caption covers a mark at {m.x():.0f},{m.y():.0f}")
    return problems

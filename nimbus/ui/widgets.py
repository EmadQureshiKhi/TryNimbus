"""Custom-painted controls for the panel: animated buttons, switches, segmented tabs, status pill,
progress ring, pipeline timeline, drop zone and surfaces.

Painted rather than styled with QSS so that hover, press and selection can animate, and so that
surfaces get real depth: a vertical gradient, a hairline highlight along the top edge, and a
translucent border.
"""
from __future__ import annotations

import math
import time

from PyQt6.QtCore import (
    QEasingCurve, QPointF, QRectF, QSize, Qt, QTimer, QVariantAnimation, pyqtSignal,
)
from PyQt6.QtGui import QColor, QFontMetricsF, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import QAbstractButton, QSizePolicy, QWidget

from nimbus.ui import theme

ACCENT = QColor(*theme.ACCENT)
ACCENT_LIGHT = QColor(*theme.ACCENT_LIGHT)
ACCENT_DEEP = QColor(*theme.ACCENT_DEEP)
TEXT = QColor(theme.TEXT_PRIMARY)
TEXT2 = QColor(theme.TEXT_SECONDARY)
TEXT3 = QColor(theme.TEXT_MUTED)


def rgba(rgb, a: float) -> QColor:
    c = QColor(*rgb) if isinstance(rgb, tuple) else QColor(rgb)
    c.setAlphaF(max(0.0, min(1.0, a)))
    return c


def mix(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(int(a.red() + (b.red() - a.red()) * t), int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t), int(a.alpha() + (b.alpha() - a.alpha()) * t))


def animator(owner, setter, duration=160, curve=QEasingCurve.Type.OutCubic) -> QVariantAnimation:
    anim = QVariantAnimation(owner)
    anim.setDuration(duration)
    anim.setEasingCurve(curve)
    anim.valueChanged.connect(setter)
    return anim


def paint_surface(p: QPainter, rect: QRectF, radius: float, *, top="#1F1A17", bottom="#141110",
                  border_alpha: float = 0.09, highlight: float = 0.07, border: QColor | None = None) -> None:
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    grad.setColorAt(0, QColor(top))
    grad.setColorAt(1, QColor(bottom))
    p.fillPath(path, grad)
    p.setPen(QPen(border or rgba((255, 255, 255), border_alpha), 1))
    p.drawPath(path)
    if highlight:
        hl = QLinearGradient(rect.topLeft(), QPointF(rect.left(), rect.top() + 18))
        hl.setColorAt(0, rgba((255, 255, 255), highlight))
        hl.setColorAt(1, rgba((255, 255, 255), 0))
        p.setPen(QPen(hl, 1))
        inner = QPainterPath()
        inner.addRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        p.drawPath(inner)


class Surface(QWidget):
    """A card: gradient, border, top highlight. Children lay out inside it normally."""

    def __init__(self, parent=None, *, radius=14, top="#1E1916", bottom="#161210", border: QColor | None = None,
                 highlight=0.06) -> None:
        super().__init__(parent)
        self.radius, self.top, self.bottom, self.border, self.highlight = radius, top, bottom, border, highlight
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_surface(p, QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), self.radius, top=self.top,
                      bottom=self.bottom, border=self.border, highlight=self.highlight)


class NButton(QAbstractButton):
    """Rounded button with animated hover and press. Variants: primary, secondary, ghost, danger."""

    def __init__(self, text: str = "", variant: str = "secondary", parent=None, *, icon: str = "",
                 height: int = 36) -> None:
        super().__init__(parent)
        self.setText(text)
        self.variant, self.icon = variant, icon
        self._hover = 0.0
        self._press = 0.0
        self._anim = animator(self, self._set_hover, 140)
        self._panim = animator(self, self._set_press, 90)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFont(theme.font(13, "semibold"))

    def _set_hover(self, v) -> None:
        self._hover = float(v)
        self.update()

    def _set_press(self, v) -> None:
        self._press = float(v)
        self.update()

    def _to(self, anim, start, end) -> None:
        anim.stop()
        anim.setStartValue(start)
        anim.setEndValue(end)
        anim.start()

    def enterEvent(self, e) -> None:
        self._to(self._anim, self._hover, 1.0)

    def leaveEvent(self, e) -> None:
        self._to(self._anim, self._hover, 0.0)

    def mousePressEvent(self, e) -> None:
        self._to(self._panim, self._press, 1.0)
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        self._to(self._panim, self._press, 0.0)
        super().mouseReleaseEvent(e)

    def sizeHint(self) -> QSize:
        fm = QFontMetricsF(self.font())
        return QSize(int(fm.horizontalAdvance(self.text()) + (44 if self.icon else 28)), self.height())

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        shrink = self._press * 1.2
        r = r.adjusted(shrink, shrink, -shrink, -shrink)
        radius = r.height() / 2.6
        enabled = self.isEnabled()
        checked = self.isCheckable() and self.isChecked()
        h = self._hover if enabled else 0.0
        if self.variant == "primary" or checked:
            base_top = DANGER_TOP if (checked and self.variant == "primary") else ACCENT_LIGHT
            base_bot = DANGER_BOT if (checked and self.variant == "primary") else ACCENT
            if not enabled:
                base_top, base_bot = QColor("#5A3F2B"), QColor("#4A3322")
            if h > 0 and enabled:  # soft glow
                for i in range(4):
                    glow = QPainterPath()
                    g = 2 + i * 2
                    glow.addRoundedRect(r.adjusted(-g, -g, g, g), radius + g, radius + g)
                    p.fillPath(glow, rgba((base_bot.red(), base_bot.green(), base_bot.blue()), 0.06 * h))
            grad = QLinearGradient(r.topLeft(), r.bottomLeft())
            grad.setColorAt(0, mix(base_top, QColor("#FFFFFF"), 0.08 * h))
            grad.setColorAt(1, mix(base_bot, QColor("#FFFFFF"), 0.06 * h))
            path = QPainterPath()
            path.addRoundedRect(r, radius, radius)
            p.fillPath(path, grad)
            p.setPen(QPen(rgba((255, 255, 255), 0.22), 1))
            p.drawLine(QPointF(r.left() + radius, r.top() + 0.8), QPointF(r.right() - radius, r.top() + 0.8))
            fg = QColor("#FFF7F0") if checked else QColor("#1A0E05")
            if not enabled:
                fg = QColor("#2B1D12")
        else:
            if self.variant == "danger":
                fill = mix(QColor(*theme.DANGER_DEEP), QColor("#4A1A29"), h)
                edge = rgba(theme.DANGER, 0.28 + 0.22 * h)
                fg = mix(QColor(*theme.DANGER), QColor("#FFE3EA"), 0.25 + 0.3 * h)
            elif self.variant == "ghost":
                fill = rgba((255, 255, 255), 0.05 * h)
                edge = rgba((255, 255, 255), 0.0)
                fg = mix(TEXT2, TEXT, h)
            else:
                fill = mix(QColor("#221D1A"), QColor("#2C2522"), h)
                edge = rgba((255, 255, 255), 0.08 + 0.06 * h)
                fg = TEXT
            if not enabled:
                fg = TEXT3
            path = QPainterPath()
            path.addRoundedRect(r, radius, radius)
            p.fillPath(path, fill)
            p.setPen(QPen(edge, 1))
            p.drawPath(path)
        p.setPen(fg)
        p.setFont(self.font())
        label = (self.icon + "  " if self.icon else "") + self.text()
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, label)


DANGER_TOP = QColor("#FF7B97")
DANGER_BOT = QColor("#E0456A")


class IconButton(NButton):
    def __init__(self, glyph: str, parent=None, *, size: int = 30, variant: str = "ghost", tooltip: str = "") -> None:
        super().__init__(glyph, variant, parent, height=size)
        self.setFixedSize(size, size)
        self.setToolTip(tooltip)
        self.setFont(theme.font(14, "semibold"))


class Switch(QAbstractButton):
    """An animated iOS-style switch; `toggled(bool)` like a checkbox."""

    def __init__(self, checked: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setFixedSize(40, 24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pos = 1.0 if checked else 0.0
        self._anim = animator(self, self._set_pos, 180)
        self.toggled.connect(self._animate)

    def _set_pos(self, v) -> None:
        self._pos = float(v)
        self.update()

    def _animate(self, on: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        track = mix(QColor("#2E2824"), ACCENT, self._pos)
        path = QPainterPath()
        path.addRoundedRect(r, r.height() / 2, r.height() / 2)
        p.fillPath(path, track)
        p.setPen(QPen(rgba((255, 255, 255), 0.08), 1))
        p.drawPath(path)
        d = r.height() - 4
        x = r.left() + 2 + (r.width() - d - 4) * self._pos
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(rgba((0, 0, 0), 0.25))
        p.drawEllipse(QRectF(x, r.top() + 3, d, d))
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(QRectF(x, r.top() + 2, d, d))


class Segmented(QWidget):
    """Tabs with a selection pill that slides between them."""

    changed = pyqtSignal(int)

    def __init__(self, labels: list[str], parent=None) -> None:
        super().__init__(parent)
        self.labels = labels
        self.index = 0
        self._x = 0.0
        self._hover = -1
        self._anim = animator(self, self._set_x, 240)
        self.setFixedHeight(36)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFont(theme.font(13, "semibold"))

    def _set_x(self, v) -> None:
        self._x = float(v)
        self.update()

    def _seg_w(self) -> float:
        return (self.width() - 6) / max(1, len(self.labels))

    def set_index(self, i: int, animate: bool = True) -> None:
        if i == self.index and animate:
            return
        self.index = i
        self._anim.stop()
        if animate:
            self._anim.setStartValue(self._x)
            self._anim.setEndValue(float(i))
            self._anim.start()
        else:
            self._x = float(i)
            self.update()
        self.changed.emit(i)

    def mouseMoveEvent(self, e) -> None:
        h = int((e.position().x() - 3) // self._seg_w())
        if h != self._hover:
            self._hover = h
            self.update()

    def leaveEvent(self, e) -> None:
        self._hover = -1
        self.update()

    def mouseReleaseEvent(self, e) -> None:
        i = int((e.position().x() - 3) // self._seg_w())
        if 0 <= i < len(self.labels):
            self.set_index(i)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        track = QPainterPath()
        track.addRoundedRect(r, 11, 11)
        p.fillPath(track, QColor("#120F0D"))
        p.setPen(QPen(rgba((255, 255, 255), 0.06), 1))
        p.drawPath(track)
        w = self._seg_w()
        pill = QRectF(3 + self._x * w, 3, w, r.height() - 5)
        pp = QPainterPath()
        pp.addRoundedRect(pill, 9, 9)
        grad = QLinearGradient(pill.topLeft(), pill.bottomLeft())
        grad.setColorAt(0, QColor("#2F2824"))
        grad.setColorAt(1, QColor("#26201C"))
        p.fillPath(pp, grad)
        p.setPen(QPen(rgba((255, 255, 255), 0.10), 1))
        p.drawPath(pp)
        p.setPen(QPen(rgba(theme.ACCENT, 0.9), 2))
        p.drawLine(QPointF(pill.center().x() - 9, pill.bottom() - 1.5), QPointF(pill.center().x() + 9, pill.bottom() - 1.5))
        p.setFont(self.font())
        for i, label in enumerate(self.labels):
            cell = QRectF(3 + i * w, 3, w, r.height() - 6)
            closeness = max(0.0, 1.0 - abs(self._x - i))
            color = mix(TEXT3 if i != self._hover else TEXT2, TEXT, closeness)
            p.setPen(color)
            p.drawText(cell, Qt.AlignmentFlag.AlignCenter, label)


class StatusPill(QWidget):
    """A coloured dot and short text; the dot breathes while busy."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.text = "Ready"
        self.rgb = theme.SUCCESS
        self.busy = False
        self._t0 = time.monotonic()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self.setFont(theme.font(12, "medium", "label"))
        self.setFixedHeight(24)

    def set(self, text: str, rgb, busy: bool = False) -> None:
        self.text, self.rgb, self.busy = text, rgb, busy
        if busy:
            self._timer.start(33)
        else:
            self._timer.stop()
        self.setMinimumWidth(self.sizeHint().width())
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(int(QFontMetricsF(self.font()).horizontalAdvance(self.text) + 32), 24)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        fm = QFontMetricsF(self.font())
        w = min(self.width() - 1.0, fm.horizontalAdvance(self.text) + 32)
        r = QRectF(self.width() - w - 0.5, 0.5, w, 23)
        path = QPainterPath()
        path.addRoundedRect(r, 11.5, 11.5)
        p.fillPath(path, rgba(self.rgb, 0.10))
        p.setPen(QPen(rgba(self.rgb, 0.28), 1))
        p.drawPath(path)
        c = QPointF(r.left() + 12, r.center().y())
        if self.busy:
            phase = (math.sin((time.monotonic() - self._t0) * 5) + 1) / 2
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(rgba(self.rgb, 0.25 * phase))
            p.drawEllipse(c, 4 + 4 * phase, 4 + 4 * phase)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(*self.rgb))
        p.drawEllipse(c, 3.5, 3.5)
        p.setPen(mix(QColor(*self.rgb), TEXT, 0.45))
        p.setFont(self.font())
        p.drawText(r.adjusted(21, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   fm.elidedText(self.text, Qt.TextElideMode.ElideRight, r.width() - 26))


class ProgressRing(QWidget):
    def __init__(self, size: int = 46, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.value = 0.0
        self.label = ""
        self.spin = False
        self._t0 = time.monotonic()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self._anim = animator(self, self._set, 400)

    def _set(self, v) -> None:
        self.value = float(v)
        self.update()

    def set_value(self, v: float, label: str = "") -> None:
        self.label = label
        self._anim.stop()
        self._anim.setStartValue(self.value)
        self._anim.setEndValue(max(0.0, min(1.0, v)))
        self._anim.start()

    def set_spinning(self, on: bool) -> None:
        self.spin = on
        self._timer.start(33) if on else self._timer.stop()
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        p.setPen(QPen(QColor("#2A2420"), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawEllipse(r)
        pen = QPen(ACCENT, 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        if self.spin and self.value <= 0.001:
            start = int(-(time.monotonic() - self._t0) * 360 * 16) % (360 * 16)
            p.drawArc(r, start, 90 * 16)
        else:
            p.drawArc(r, 90 * 16, int(-360 * 16 * self.value))
        if self.label:
            p.setPen(TEXT)
            p.setFont(theme.font(12, "bold", "label"))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, self.label)


class Timeline(QWidget):
    """Vertical pipeline: a node per stage joined by a line; the active node pulses."""

    STATES = ("pending", "active", "done", "failed")

    def __init__(self, stages: list[tuple[str, str]], parent=None) -> None:
        super().__init__(parent)
        self.stages = stages
        self.state = {k: "pending" for k, _ in stages}
        self.detail: dict[str, str] = {}
        self._t0 = time.monotonic()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self._timer.start(40)
        self.row_h = 42  # a title and its detail line, with clear air before the next stage
        self.setMinimumHeight(self.row_h * len(stages) + 4)

    def set_state(self, key: str, state: str, detail: str = "") -> None:
        if key in self.state:
            self.state[key] = state
            if detail:
                self.detail[key] = detail
            self.update()

    def reset(self) -> None:
        self.state = {k: "pending" for k, _ in self.stages}
        self.detail.clear()
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        x = 12.0
        title_font, detail_font = theme.font(13, "semibold"), theme.font(11.5)
        colors = {"pending": QColor("#3A332E"), "active": ACCENT, "done": QColor(*theme.SUCCESS),
                  "failed": QColor(*theme.DANGER)}
        for i, (key, title) in enumerate(self.stages):
            y = 12 + i * self.row_h
            st = self.state[key]
            if i < len(self.stages) - 1:
                nxt = self.state[self.stages[i + 1][0]]
                p.setPen(QPen(colors["done"] if nxt != "pending" and st == "done" else QColor("#2E2824"), 2))
                p.drawLine(QPointF(x, y + 8), QPointF(x, y + self.row_h - 8))
            c = QPointF(x, y)
            if st == "active":
                phase = (math.sin((time.monotonic() - self._t0) * 4) + 1) / 2
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(rgba(theme.ACCENT, 0.18 + 0.2 * phase))
                p.drawEllipse(c, 9 + 3 * phase, 9 + 3 * phase)
            p.setPen(QPen(colors[st], 2))
            p.setBrush(colors[st] if st in ("done", "failed") else QColor("#16120F"))
            p.drawEllipse(c, 6.5, 6.5)
            if st == "done":
                p.setPen(QPen(QColor("#0B1F16"), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawPolyline([QPointF(x - 3, y), QPointF(x - 0.8, y + 2.4), QPointF(x + 3.2, y - 2.4)])
            elif st == "failed":
                p.setPen(QPen(QColor("#2A0E0E"), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(QPointF(x - 2.5, y - 2.5), QPointF(x + 2.5, y + 2.5))
                p.drawLine(QPointF(x + 2.5, y - 2.5), QPointF(x - 2.5, y + 2.5))
            p.setFont(title_font)
            p.setPen(TEXT if st != "pending" else TEXT3)
            p.drawText(QRectF(x + 18, y - 10, self.width() - 40, 18), Qt.AlignmentFlag.AlignVCenter, title)
            detail = self.detail.get(key, "")
            if detail and st != "pending":
                p.setFont(detail_font)
                p.setPen(TEXT3)
                fm = QFontMetricsF(detail_font)
                p.drawText(QRectF(x + 18, y + 6, self.width() - 40, 16), Qt.AlignmentFlag.AlignVCenter,
                           fm.elidedText(detail, Qt.TextElideMode.ElideRight, self.width() - 44))


class DropZone(QWidget):
    """Dashed drop target for video files; click to browse."""

    dropped = pyqtSignal(str)
    clicked = pyqtSignal()
    EXTS = (".mp4", ".mov", ".m4v", ".webm", ".mkv")

    def __init__(self, title: str, subtitle: str, parent=None) -> None:
        super().__init__(parent)
        self.title, self.subtitle = title, subtitle
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(132)
        self._hover = 0.0
        self._anim = animator(self, self._set, 160)
        self.mark = QPixmap(str(theme.ASSETS / "nimbus_mark_256.png"))

    def _set(self, v) -> None:
        self._hover = float(v)
        self.update()

    def _to(self, v: float) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._hover)
        self._anim.setEndValue(v)
        self._anim.start()

    def enterEvent(self, e) -> None:
        self._to(0.6)

    def leaveEvent(self, e) -> None:
        self._to(0.0)

    def mouseReleaseEvent(self, e) -> None:
        self.clicked.emit()

    def dragEnterEvent(self, e) -> None:
        urls = e.mimeData().urls() if e.mimeData().hasUrls() else []
        if any(u.toLocalFile().lower().endswith(self.EXTS) for u in urls):
            e.acceptProposedAction()
            self._to(1.0)

    def dragLeaveEvent(self, e) -> None:
        self._to(0.0)

    def dropEvent(self, e) -> None:
        self._to(0.0)
        for u in e.mimeData().urls():
            path = u.toLocalFile()
            if path.lower().endswith(self.EXTS):
                self.dropped.emit(path)
                return

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(r, 14, 14)
        p.fillPath(path, mix(QColor("#17130F"), QColor("#2A1C12"), self._hover))
        pen = QPen(mix(QColor("#3A322C"), ACCENT, self._hover), 1.4, Qt.PenStyle.DashLine)
        pen.setDashPattern([5, 4])
        p.setPen(pen)
        p.drawPath(path)
        size = 34
        p.drawPixmap(QRectF(r.center().x() - size / 2, r.top() + 22, size, size).toRect(),
                     self.mark.scaled(size * 2, size * 2, Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation))
        p.setPen(TEXT)
        p.setFont(theme.font(14, "semibold", "display"))
        p.drawText(QRectF(r.left(), r.top() + 64, r.width(), 20), Qt.AlignmentFlag.AlignCenter, self.title)
        p.setPen(TEXT3)
        p.setFont(theme.font(11.5, "medium", "label"))
        p.drawText(QRectF(r.left(), r.top() + 86, r.width(), 18), Qt.AlignmentFlag.AlignCenter, self.subtitle)


class Chip(NButton):
    def __init__(self, text: str, parent=None) -> None:
        super().__init__(text, "secondary", parent, height=30)
        self.setFont(theme.font(12, "medium"))
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)


class Section(QWidget):
    """A titled block that folds away: click the header to collapse or expand with an animation.
    `key` lets the owner remember which sections the person closed."""

    toggled = pyqtSignal(str, bool)  # key, collapsed

    def __init__(self, title: str, content: QWidget, *, key: str = "", collapsed: bool = False,
                 trailing: QWidget | None = None, parent=None) -> None:
        from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout

        super().__init__(parent)
        self.key = key or title.lower()
        self.content = content
        self.collapsed = collapsed
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.header = _SectionHeader(title)
        self.header.clicked.connect(self.toggle)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.header, 1)
        if trailing is not None:
            row.addWidget(trailing)
        lay.addLayout(row)
        lay.addWidget(content)
        self._anim = QPropertyAnimationCompat(content)
        self.header.set_angle(0.0 if collapsed else 90.0)
        if collapsed:
            content.setMaximumHeight(0)
            content.hide()

    def toggle(self) -> None:
        self.set_collapsed(not self.collapsed)
        self.toggled.emit(self.key, self.collapsed)

    def set_collapsed(self, collapsed: bool, animate: bool = True) -> None:
        if collapsed == self.collapsed:
            return
        self.collapsed = collapsed
        self.header.animate_to(0.0 if collapsed else 90.0)
        full = max(self.content.sizeHint().height(), self.content.minimumSizeHint().height())
        if not collapsed:
            self.content.show()
        self._anim.run(self.content.height() if collapsed else 0, 0 if collapsed else full,
                       done=(lambda: self.content.hide()) if collapsed else (lambda: self.content.setMaximumHeight(16777215)),
                       animate=animate)


class _SectionHeader(QAbstractButton):
    def __init__(self, title: str) -> None:
        super().__init__()
        self.title = title
        self.angle = 90.0
        self._hover = 0.0
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(22)
        self._anim = animator(self, self._set_angle, 180)

    def _set_angle(self, v) -> None:
        self.angle = float(v)
        self.update()

    def set_angle(self, a: float) -> None:
        self.angle = a
        self.update()

    def animate_to(self, a: float) -> None:
        self._anim.stop()
        self._anim.setStartValue(self.angle)
        self._anim.setEndValue(a)
        self._anim.start()

    def enterEvent(self, e) -> None:
        self._hover = 1.0
        self.update()

    def leaveEvent(self, e) -> None:
        self._hover = 0.0
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = mix(TEXT3, TEXT2, self._hover)
        p.save()
        p.translate(6, self.height() / 2)
        p.rotate(self.angle)
        p.setPen(QPen(color, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.drawPolyline([QPointF(-2, -3.5), QPointF(2, 0), QPointF(-2, 3.5)])
        p.restore()
        p.setPen(color)
        p.setFont(theme.font(11.5, "bold", "label"))
        p.drawText(QRectF(18, 0, self.width() - 18, self.height()), Qt.AlignmentFlag.AlignVCenter,
                   self.title.upper())


class QPropertyAnimationCompat:
    """Animates a widget's maximumHeight (what a fold needs) with a completion callback."""

    def __init__(self, widget: QWidget) -> None:
        self.widget = widget
        self.anim = animator(widget, lambda v: widget.setMaximumHeight(int(v)), 200)
        self._done = None
        self.anim.finished.connect(self._finish)

    def run(self, start: int, end: int, *, done=None, animate: bool = True) -> None:
        self._done = done
        self.anim.stop()
        if not animate:
            self.widget.setMaximumHeight(end)
            self._finish()
            return
        self.anim.setStartValue(start)
        self.anim.setEndValue(end)
        self.anim.start()

    def _finish(self) -> None:
        if self._done:
            done, self._done = self._done, None
            done()


class MicButton(QAbstractButton):
    """Press and hold to talk; a ring around it follows the voice level."""

    held = pyqtSignal(bool)

    def __init__(self, size: int = 36, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Hold to talk")
        self.level = 0.0
        self.active = False
        self._hover = 0.0
        self._t0 = time.monotonic()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)

    def set_level(self, level: float) -> None:
        self.level = max(0.0, min(1.0, level))

    def enterEvent(self, e) -> None:
        self._hover = 1.0
        self.update()

    def leaveEvent(self, e) -> None:
        self._hover = 0.0
        self.update()

    def mousePressEvent(self, e) -> None:
        self.active = True
        self._timer.start(33)
        self.held.emit(True)
        self.update()

    def mouseReleaseEvent(self, e) -> None:
        if self.active:
            self.active = False
            self._timer.stop()
            self.level = 0.0
            self.held.emit(False)
            self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(3, 3, -3, -3)
        c = r.center()
        if self.active:
            pulse = 0.5 + 0.5 * math.sin((time.monotonic() - self._t0) * 6)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(rgba(theme.LISTENING, 0.16 + 0.22 * self.level))
            rad = r.width() / 2 + 2 + 3 * self.level + 1.5 * pulse
            p.drawEllipse(c, rad, rad)
        fill = QColor(*theme.LISTENING) if self.active else mix(QColor("#241E1B"), QColor("#2F2724"), self._hover)
        p.setPen(QPen(rgba((255, 255, 255), 0.10), 1))
        p.setBrush(fill)
        p.drawEllipse(r)
        color = QColor("#0E1F17") if self.active else mix(TEXT2, TEXT, self._hover)
        p.setPen(QPen(color, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        body = QRectF(c.x() - 3.2, c.y() - 7.5, 6.4, 10)
        p.drawRoundedRect(body, 3.2, 3.2)
        p.drawArc(QRectF(c.x() - 6, c.y() - 3.5, 12, 9), 200 * 16, 140 * 16)
        p.drawLine(QPointF(c.x(), c.y() + 5.5), QPointF(c.x(), c.y() + 8))

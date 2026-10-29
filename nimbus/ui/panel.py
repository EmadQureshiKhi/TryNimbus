"""The edge panel: a small tab on the right edge of the screen that opens into Nimbus's sidebar.

Collapsed, it is a slim glass tab with the Nimbus mark and a status ring; hover slides it out,
drag moves it along the edge, click opens the sidebar. Open, the sidebar slides and fades in with a
soft drop shadow, and holds four pages (Ask, Shadow, Repro, Settings) behind a sliding segmented
control. Everything the person does is emitted as a signal; the app controller owns behaviour, so
this file only draws and reports.
"""
from __future__ import annotations

import math
import time
from pathlib import Path

from PyQt6.QtCore import (
    QEasingCurve, QParallelAnimationGroup, QPoint, QPointF, QPropertyAnimation, QRect, QRectF, QSize, Qt,
    QTimer, pyqtSignal,
)
from PyQt6.QtGui import (
    QColor, QCursor, QKeySequence, QLinearGradient, QMouseEvent, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QLineEdit,
    QScrollArea, QStackedWidget, QVBoxLayout, QWidget,
)

from nimbus.mac import windows as macwin
from nimbus.ui import theme
from nimbus.ui.widgets import (
    Chip, DropZone, IconButton, MicButton, NButton, ProgressRing, Section, Segmented, StatusPill, Surface, Switch,
    Timeline, mix, rgba,
)

TAB_W, TAB_H = 34, 112
PANEL_W, PANEL_H = 404, 690
SHADOW_PAGE = 1  # Ask, Shadow, Repro, Settings
SHADOW = 26  # transparent margin around the card for its drop shadow
ACCENT = theme.hex_rgb(theme.ACCENT)

QSS = f"""
QLabel {{ color: {theme.TEXT_PRIMARY}; font-size: 13px; background: transparent; }}
QLabel#h1 {{ font-family: "{theme.DISPLAY_FAMILY}"; font-size: 19px; font-weight: 600; }}
QLabel#h2 {{ font-family: "{theme.DISPLAY_FAMILY}"; font-size: 16px; font-weight: 600; }}
QLabel#muted {{ color: {theme.TEXT_SECONDARY}; font-size: 12.5px; }}
QLabel#faint {{ color: {theme.TEXT_MUTED}; font-size: 12px; }}
QLabel#section {{ color: {theme.TEXT_MUTED}; font-family: "{theme.LABEL_FAMILY}"; font-size: 11.5px;
  font-weight: 700; letter-spacing: 1px; }}
QLabel#eyebrow {{ color: {theme.TEXT_MUTED}; font-family: "{theme.LABEL_FAMILY}"; font-size: 12px; letter-spacing: 0.5px; }}
QLabel#mono {{ color: {theme.TEXT_SECONDARY}; font-family: "{theme.MONO_FAMILY}"; font-size: 11.5px; }}
QLineEdit {{ background: transparent; color: {theme.TEXT_PRIMARY}; border: none; font-size: 13.5px;
  selection-background-color: {ACCENT}; padding: 0 2px; }}
QLineEdit#field {{ background: #120F0D; border: 1px solid #2E2723; border-radius: 10px; padding: 8px 10px; }}
QLineEdit#field:focus {{ border-color: {ACCENT}; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 6px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: rgba(255,255,255,0.12); border-radius: 3px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ height: 0; background: none; }}
QToolTip {{ background: #1E1916; color: {theme.TEXT_PRIMARY}; border: 1px solid #3A322C; padding: 4px 8px; }}
"""


def _label(text: str = "", name: str | None = None, wrap: bool = True, rich: bool = False) -> QLabel:
    lab = QLabel(text)
    if name:
        lab.setObjectName(name)
    lab.setWordWrap(wrap)
    if rich:
        lab.setTextFormat(Qt.TextFormat.RichText)
    lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lab


def _vbox(widget: QWidget, margins=(0, 0, 0, 0), spacing=10) -> QVBoxLayout:
    lay = QVBoxLayout(widget)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    return lay


def _hbox(spacing=8) -> QHBoxLayout:
    lay = QHBoxLayout()
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(spacing)
    return lay


def _scroll(inner: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setFrameShape(QScrollArea.Shape.NoFrame)
    area.viewport().setAutoFillBackground(False)
    inner.setAutoFillBackground(False)
    area.setWidget(inner)
    return area


# ---------------------------------------------------------------- the collapsed tab


class EdgeTab(QWidget):
    """Slim glass tab on the screen edge. Hover slides it out; drag moves it; click opens."""

    clicked = pyqtSignal()
    moved = pyqtSignal(int)

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(TAB_W + 14, TAB_H + 24)  # room for the painted shadow
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Nimbus: click to open, drag to move")
        self.mark = QPixmap(str(theme.ASSETS / "nimbus_mark_256.png"))
        self.status_rgb = theme.MUTED
        self.busy = False
        self._press: QPoint | None = None
        self._dragged = False
        self._hover = 0.0
        self._t0 = time.monotonic()
        self._anim = QPropertyAnimation(self, b"pos")
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self.rest_x = 0

    def showEvent(self, e) -> None:
        super().showEvent(e)
        macwin.make_overlay(self, click_through=False, level=macwin.PANEL_LEVEL)

    def set_status(self, rgb, busy: bool = False) -> None:
        self.status_rgb, self.busy = rgb, busy
        self._timer.start(33) if busy else self._timer.stop()
        self.update()

    def _slide(self, dx: int) -> None:
        self._anim.stop()
        self._anim.setStartValue(self.pos())
        self._anim.setEndValue(QPoint(self.rest_x - dx, self.y()))
        self._anim.start()

    def enterEvent(self, e) -> None:
        self._hover = 1.0
        self._slide(6)
        self.update()

    def leaveEvent(self, e) -> None:
        self._hover = 0.0
        self._slide(0)
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = QRectF(12, 12, TAB_W + 14, TAB_H)  # runs off the right edge
        for i in range(6):  # soft painted shadow
            g = 6 - i
            sh = QPainterPath()
            sh.addRoundedRect(body.adjusted(-g, -g + 3, 0, g + 3), 16 + g, 16 + g)
            p.fillPath(sh, rgba((0, 0, 0), 0.035))
        path = QPainterPath()
        path.addRoundedRect(body, 16, 16)
        grad = QLinearGradient(body.topLeft(), body.bottomLeft())
        grad.setColorAt(0, QColor("#2A2320") if self._hover else QColor("#221C19"))
        grad.setColorAt(1, QColor("#161210"))
        p.fillPath(path, grad)
        p.setPen(QPen(rgba((255, 255, 255), 0.12), 1))
        p.drawPath(path)
        cx = body.left() + TAB_W / 2
        size = 22
        center = QPointF(cx, body.top() + 24)
        ring = QRectF(center.x() - 15, center.y() - 15, 30, 30)
        if self.busy:
            start = int(-(time.monotonic() - self._t0) * 300 * 16) % (360 * 16)
            p.setPen(QPen(QColor(*self.status_rgb), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(ring, start, 110 * 16)
        glow = QRadialGradient(center, 18)
        glow.setColorAt(0, rgba(theme.ACCENT, 0.22 if self._hover else 0.12))
        glow.setColorAt(1, rgba(theme.ACCENT, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawEllipse(center, 18, 18)
        p.drawPixmap(QRectF(cx - size / 2, center.y() - size / 2, size, size).toRect(),
                     self.mark.scaled(size * 2, size * 2, Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation))
        p.setBrush(rgba((255, 255, 255), 0.22 + 0.2 * self._hover))
        for i in range(3):
            p.drawEllipse(QPointF(cx, body.top() + 54 + i * 7), 1.5, 1.5)
        dot = QPointF(cx, body.bottom() - 18)
        if self.busy:
            phase = (math.sin((time.monotonic() - self._t0) * 5) + 1) / 2
            p.setBrush(rgba(self.status_rgb, 0.3 * phase))
            p.drawEllipse(dot, 4 + 4 * phase, 4 + 4 * phase)
        p.setBrush(QColor(*self.status_rgb))
        p.drawEllipse(dot, 3.6, 3.6)

    def mousePressEvent(self, e: QMouseEvent) -> None:
        self._press = e.globalPosition().toPoint()
        self._dragged = False

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        if self._press is None:
            return
        if abs(e.globalPosition().toPoint().y() - self._press.y()) > 3:
            self._dragged = True
        if self._dragged:
            self.moved.emit(e.globalPosition().toPoint().y())

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:
        if not self._dragged:
            self.clicked.emit()
        self._press = None


# ---------------------------------------------------------------- pieces of the sidebar


class MessageCard(Surface):
    """One exchange. Click the question to fold the answer; hover for Show again and Copy. New answers
    type themselves in."""

    replay = pyqtSignal(int)

    def __init__(self, question: str, answer_html: str, stamp: str, index: int | None, plain: str) -> None:
        super().__init__(radius=12, top="#1C1714", bottom="#171310", highlight=0.04)
        self.index, self.plain, self.html = index, plain, answer_html
        lay = _vbox(self, (14, 10, 14, 11), 6)
        top = _hbox()
        self.q = QLabel(question)
        self.q.setObjectName("faint")
        self.q.setWordWrap(True)
        self.q.setCursor(Qt.CursorShape.PointingHandCursor)
        self.q.setToolTip("Click to fold")
        self.q.mousePressEvent = lambda _e: self._fold()
        top.addWidget(self.q, 1)
        # The hover buttons and the timestamp share one fixed-size slot, so hovering swaps them in
        # place: nothing in the row reflows under the pointer while it is moving onto a button.
        self.corner = QStackedWidget()
        self.stamp = _label(stamp, "eyebrow", wrap=False)
        self.stamp.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        self.corner.addWidget(self.stamp)
        self.actions = QWidget()
        al = _hbox(4)
        self.actions.setLayout(al)
        al.addStretch(1)
        if index is not None:
            again = NButton("Show again", "ghost", height=24)
            again.setFont(theme.font(11.5, "medium", "label"))
            again.setFixedWidth(84)
            again.setToolTip("Fly the buddy through this answer again")
            again.clicked.connect(lambda: self.replay.emit(self.index))
            al.addWidget(again)
        self.copy_btn = NButton("Copy", "ghost", height=24)
        self.copy_btn.setFont(theme.font(11.5, "medium", "label"))
        self.copy_btn.setFixedWidth(58)
        self.copy_btn.clicked.connect(self._copy)
        al.addWidget(self.copy_btn)
        self.corner.addWidget(self.actions)
        self.corner.setFixedSize((84 + 4 if index is not None else 0) + 58, 24)
        top.addWidget(self.corner, 0, Qt.AlignmentFlag.AlignTop)
        self._hover_check = QTimer(self)
        self._hover_check.setSingleShot(True)
        self._hover_check.setInterval(120)
        self._hover_check.timeout.connect(self._sync_hover)
        lay.addLayout(top)
        self.answer = _label("", None, rich=True)
        self.answer.setStyleSheet("font-size: 13.5px;")
        self.answer.setOpenExternalLinks(True)
        lay.addWidget(self.answer)
        self._shown = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._type)
        self._timer.start(16)

    def _type(self) -> None:
        """Reveal the plain text a few characters per frame, then swap in the formatted answer."""
        self._shown += max(2, len(self.plain) // 60)
        if self._shown >= len(self.plain):
            self._timer.stop()
            self.answer.setText(self.html)
        else:
            self.answer.setText(_esc(self.plain[: self._shown]) + f"<span style='color:{ACCENT}'>\u258d</span>")

    def finish_typing(self) -> None:
        self._shown = len(self.plain)
        self._type()

    def _fold(self) -> None:
        self.answer.setVisible(not self.answer.isVisible())
        self.q.setToolTip("Click to unfold" if not self.answer.isVisible() else "Click to fold")

    def _copy(self) -> None:
        QApplication.clipboard().setText(self.plain)
        self.copy_btn.setText("Copied")
        QTimer.singleShot(1400, lambda: self.copy_btn.setText("Copy"))

    def _set_hover(self, on: bool) -> None:
        self.corner.setCurrentIndex(1 if on else 0)

    def _sync_hover(self) -> None:
        self._set_hover(self.rect().contains(self.mapFromGlobal(QCursor.pos())))

    def enterEvent(self, e) -> None:
        self._hover_check.stop()
        self._set_hover(True)

    def leaveEvent(self, e) -> None:
        # Confirm against the real pointer position: a transient leave while crossing into a child must
        # not hide the buttons the pointer is heading for.
        self._hover_check.start()


class StepCard(Surface):
    """The current Shadow step: progress ring, instruction, the control as it looked in the video."""

    def __init__(self) -> None:
        super().__init__(radius=14, top="#221B17", bottom="#181310", border=rgba(theme.ACCENT, 0.35))
        lay = _vbox(self, (14, 14, 14, 14), 10)
        top = _hbox(12)
        self.ring = ProgressRing(46)
        top.addWidget(self.ring, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(3)
        self.num = _label("", "eyebrow", wrap=False)
        self.instruction = _label("")
        self.instruction.setStyleSheet(f"font-family: '{theme.DISPLAY_FAMILY}'; font-size: 15px; font-weight: 600;")
        col.addWidget(self.num)
        col.addWidget(self.instruction)
        top.addLayout(col, 1)
        lay.addLayout(top)
        self.thumb = QLabel()
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.thumb.setStyleSheet("background: #0E0B09; border: 1px solid #2E2723; border-radius: 8px; padding: 4px;")
        lay.addWidget(self.thumb, 0, Qt.AlignmentFlag.AlignLeft)
        self.expected = _label("", "muted")
        lay.addWidget(self.expected)

    def show_step(self, index: int, total: int, instruction: str, expected: str, t: float | None,
                  crop: Path | None) -> None:
        stamp = f"  \u00b7  {int(t // 60)}:{int(t % 60):02d}" if t is not None else ""
        self.num.setText(f"STEP {index + 1} OF {total}{stamp}")
        self.ring.set_value(index / max(1, total), f"{index + 1}/{total}")
        self.instruction.setText(instruction)
        self.expected.setText(f"\u2192 {expected}" if expected else "")
        self.expected.setVisible(bool(expected))
        if crop and crop.exists():
            pm = QPixmap(str(crop))
            pm.setDevicePixelRatio(2.0)  # the crop comes from a Retina-resolution frame
            scaled = pm.scaled(QSize(600, 128), Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
            scaled.setDevicePixelRatio(2.0)
            self.thumb.setPixmap(scaled)
            self.thumb.setFixedSize(int(scaled.width() / 2) + 10, int(scaled.height() / 2) + 10)  # hug the image
            self.thumb.setToolTip("How this control looked in the video")
            self.thumb.show()
        else:
            self.thumb.hide()


class SettingRow(QWidget):
    def __init__(self, title: str, subtitle: str, control: QWidget) -> None:
        super().__init__()
        lay = _hbox(12)
        col = QVBoxLayout()
        col.setSpacing(1)
        t = _label(title, None)
        t.setStyleSheet("font-size: 13px; font-weight: 600;")
        col.addWidget(t)
        if subtitle:
            col.addWidget(_label(subtitle, "faint"))
        lay.addLayout(col, 1)
        lay.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setLayout(lay)


class GuideRow(NButton):
    def __init__(self, title: str, subtitle: str) -> None:
        super().__init__("", "ghost", height=48)
        self.title, self.subtitle = title, subtitle

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(r, 10, 10)
        p.fillPath(path, mix(QColor("#17130F"), QColor("#241D19"), self._hover))
        p.setPen(QPen(rgba((255, 255, 255), 0.05 + 0.05 * self._hover), 1))
        p.drawPath(path)
        p.setPen(QColor(*theme.ACCENT))
        p.setFont(theme.font(12, "bold"))
        p.drawText(QRectF(r.left() + 12, r.top(), 20, r.height()), Qt.AlignmentFlag.AlignVCenter, "▶")
        p.setPen(QColor(theme.TEXT_PRIMARY))
        p.setFont(theme.font(13.5, "semibold", "display"))
        p.drawText(QRectF(r.left() + 34, r.top() + 7, r.width() - 46, 18), Qt.AlignmentFlag.AlignVCenter, self.title)
        p.setPen(QColor(theme.TEXT_MUTED))
        p.setFont(theme.font(11.5, "medium", "label"))
        p.drawText(QRectF(r.left() + 34, r.top() + 25, r.width() - 46, 16), Qt.AlignmentFlag.AlignVCenter,
                   self.subtitle)


class PageStack(QStackedWidget):
    """Cross-fades between pages."""

    def set_page(self, i: int) -> None:
        if i == self.currentIndex():
            return
        self.setCurrentIndex(i)
        page = self.currentWidget()
        effect = QGraphicsOpacityEffect(page)
        page.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", page)
        anim.setDuration(180)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(lambda: page.setGraphicsEffect(None))
        anim.start()


# ---------------------------------------------------------------- the sidebar


class Panel(QWidget):
    # Ask
    ask_submitted = pyqtSignal(str, bool)  # text, act (let Nimbus operate)
    # Shadow
    shadow_open = pyqtSignal(str)
    shadow_resume = pyqtSignal(str)
    shadow_action = pyqtSignal(str)  # show | check | skip | do | stop | back
    # Repro
    repro_record = pyqtSignal(bool)
    repro_video = pyqtSignal(str)
    repro_cancel = pyqtSignal()
    repro_open_report = pyqtSignal()
    # Settings
    setting_changed = pyqtSignal(str, object)
    api_key_entered = pyqtSignal(str)
    open_permissions = pyqtSignal(str)
    quit_requested = pyqtSignal()
    confirmed = pyqtSignal(bool)
    mic_held = pyqtSignal(bool)
    replay_requested = pyqtSignal(int)

    STAGES = [("analyze", "Understand the recording"), ("launch", "Start the app"),
              ("reproduce", "Reproduce the bug"), ("fix", "Fix with Muse Code"),
              ("verify", "Prove the fix"), ("report", "Report and pull request")]

    def __init__(self, settings) -> None:
        super().__init__(None)
        self.settings = settings
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(QSS)
        self.tab = EdgeTab()
        self.tab.clicked.connect(self.open)
        self.tab.moved.connect(self._move_tab)
        self._anim: QParallelAnimationGroup | None = None
        self.is_open = False
        self._avoid: QRect | None = None  # a control the panel must not cover (Shadow's current target)
        self._build()

    # ------------------------------------------------------------ layout

    def _build(self) -> None:
        outer = _vbox(self, (SHADOW, SHADOW - 8, SHADOW, SHADOW + 6), 0)
        self.card = Surface(radius=20, top="#1E1916", bottom="#120F0D", highlight=0.08)
        shadow = QGraphicsDropShadowEffect(self.card)
        shadow.setBlurRadius(46)
        shadow.setOffset(0, 16)
        shadow.setColor(QColor(0, 0, 0, 170))
        self.card.setGraphicsEffect(shadow)
        outer.addWidget(self.card)
        lay = _vbox(self.card, (18, 16, 18, 16), 14)

        head = _hbox(10)
        logo = QLabel()
        logo.setPixmap(QPixmap(str(theme.ASSETS / "nimbus_mark_256.png")).scaled(
            30, 30, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        head.addWidget(logo)
        head.addWidget(_label("Nimbus", "h1", wrap=False))
        head.addStretch(1)
        self.status_pill = StatusPill()
        head.addWidget(self.status_pill, 0, Qt.AlignmentFlag.AlignVCenter)
        close = IconButton("✕", tooltip="Collapse to the edge (Esc)")
        close.clicked.connect(self.collapse)
        head.addWidget(close, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addLayout(head)

        self.tabs = Segmented(["Ask", "Shadow", "Repro", "Settings"])
        lay.addWidget(self.tabs)
        self.pages = PageStack()
        for builder in (self._ask_page, self._shadow_page, self._repro_page, self._settings_page):
            self.pages.addWidget(builder())
        self.tabs.changed.connect(self.pages.set_page)
        lay.addWidget(self.pages, 1)
        for i in range(4):  # Cmd+1..4 switch pages
            QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self, activated=lambda i=i: self.show_page(i))

    def show_page(self, i: int) -> None:
        self.tabs.set_index(i)
        self.pages.set_page(i)

    def _ask_page(self) -> QWidget:
        page = QWidget()
        lay = _vbox(page, spacing=10)
        self.conversation = QWidget()
        self.convo_lay = _vbox(self.conversation, spacing=8)
        self.empty_ask = QWidget()
        el = _vbox(self.empty_ask, (6, 22, 6, 6), 8)
        mark = QLabel()
        mark.setPixmap(QPixmap(str(theme.ASSETS / "nimbus_mark_256.png")).scaled(
            54, 54, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        el.addWidget(mark)
        t = _label("Ask about your screen", "h2")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        el.addWidget(t)
        hint = _label(f"HOLD {_pretty_hotkey(self.settings.hotkey).upper()} OR THE MIC", "eyebrow")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        el.addWidget(hint)
        el.addSpacing(8)
        for text in ("Where's the share button?", "Explain this error", "Solve this for me"):
            chip = Chip(text)
            chip.clicked.connect(lambda _=False, q=text: self._fill(q))
            el.addWidget(chip)
        el.addStretch(1)
        self.convo_lay.addWidget(self.empty_ask)
        self.convo_lay.addStretch(1)
        self.convo_scroll = _scroll(self.conversation)
        lay.addWidget(self.convo_scroll, 1)

        self.confirm_bar = Surface(radius=14, top="#2A1C12", bottom="#1E140D", border=rgba(theme.ACCENT, 0.55))
        cb = _vbox(self.confirm_bar, (14, 12, 14, 12), 10)
        cb.addWidget(_label("ALLOW NIMBUS TO ACT?", "section"))
        self.confirm_text = _label("")
        cb.addWidget(self.confirm_text)
        cr = _hbox()
        allow = NButton("Allow", "primary")
        allow.clicked.connect(lambda: self._answer_confirm(True))
        deny = NButton("Cancel")
        deny.clicked.connect(lambda: self._answer_confirm(False))
        cr.addWidget(allow)
        cr.addWidget(deny)
        cb.addLayout(cr)
        self.confirm_bar.hide()
        lay.addWidget(self.confirm_bar)

        act = _hbox(10)
        self.act_toggle = Switch(False)
        act.addWidget(self.act_toggle)
        act.addWidget(_label("Let Nimbus act for me", "muted", wrap=False), 1)
        lay.addLayout(act)

        bar = Surface(radius=14, top="#141110", bottom="#110E0C", border=rgba((255, 255, 255), 0.09))
        bar.setFixedHeight(50)
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(7, 7, 7, 7)
        bl.setSpacing(8)
        self.mic = MicButton(36)
        self.mic.held.connect(self.mic_held.emit)
        bl.addWidget(self.mic)
        self.ask_input = QLineEdit()
        self.ask_input.setPlaceholderText("Ask anything\u2026")
        self.ask_input.returnPressed.connect(self._submit_ask)
        bl.addWidget(self.ask_input, 1)
        send = IconButton("↑", size=36, variant="primary", tooltip="Ask (Return)")
        send.setFont(theme.font(16, "bold"))
        send.clicked.connect(self._submit_ask)
        bl.addWidget(send)
        lay.addWidget(bar)
        return page

    def _shadow_page(self) -> QWidget:
        page = QWidget()
        lay = _vbox(page, spacing=12)
        self.drop = DropZone("Drop a tutorial video", "OR CLICK  \u00b7  MP4  MOV  WEBM")
        self.drop.clicked.connect(self._pick_tutorial)
        self.drop.dropped.connect(self.shadow_open.emit)
        lay.addWidget(self.drop)

        self.loading = Surface(radius=14)
        ll = QHBoxLayout(self.loading)
        ll.setContentsMargins(14, 12, 14, 12)
        ll.setSpacing(12)
        self.load_ring = ProgressRing(42)
        ll.addWidget(self.load_ring)
        lcol = QVBoxLayout()
        lcol.setSpacing(2)
        lcol.addWidget(_label("Learning the tutorial", "h2"))
        self.shadow_status = _label("", "muted")
        lcol.addWidget(self.shadow_status)
        ll.addLayout(lcol, 1)
        self.loading.hide()
        lay.addWidget(self.loading)

        self.session = QWidget()
        sl = _vbox(self.session, spacing=10)
        self.shadow_title = _label("", "h2")
        sl.addWidget(self.shadow_title)
        self.shadow_meta = _label("", "faint")
        sl.addWidget(self.shadow_meta)
        self.step_card = StepCard()
        sl.addWidget(self.step_card)
        self.shadow_verdict = _label("", "muted")
        sl.addWidget(self.shadow_verdict)
        r1, r2 = _hbox(), _hbox()
        for text, action, variant, row in (("Show me", "show", "secondary", r1), ("I did it", "check", "primary", r1),
                                           ("Do it for me", "do", "secondary", r1), ("← Back", "back", "ghost", r2),
                                           ("Skip →", "skip", "ghost", r2), ("Stop", "stop", "danger", r2)):
            b = NButton(text, variant, height=34 if row is r1 else 30)
            b.clicked.connect(lambda _=False, a=action: self.shadow_action.emit(a))
            row.addWidget(b)
        sl.addLayout(r1)
        sl.addLayout(r2)
        self.session.hide()
        lay.addWidget(self.session)

        holder = QWidget()
        self.recent_box = _vbox(holder, spacing=6)
        self.recent_box.addStretch(1)
        scroll = _scroll(holder)
        scroll.setMinimumHeight(120)
        self.recent_section = self._section("Recent guides", scroll, "recent")
        lay.addWidget(self.recent_section, 1)
        self.recent_label = self.recent_section  # hidden while there are no guides
        return page

    def _repro_page(self) -> QWidget:
        page = QWidget()
        lay = _vbox(page, spacing=12)
        row = _hbox()
        self.record_btn = NButton("●  Record bug", "primary", height=40)
        self.record_btn.setCheckable(True)
        self.record_btn.toggled.connect(self._toggle_record)
        row.addWidget(self.record_btn, 3)
        pick = NButton("Import\u2026", height=40)
        pick.setToolTip("Use an existing screen recording")
        pick.clicked.connect(self._pick_bug_video)
        row.addWidget(pick, 2)
        lay.addLayout(row)
        lay.addWidget(_label("NARRATE WHAT'S WRONG WHILE YOU RECORD", "eyebrow"))
        project = Surface(radius=12)
        pl = _vbox(project, (14, 10, 14, 10), 4)
        self.repo_label = _label(_short_path(self.settings.repro.repo_path), "mono")
        pl.addWidget(self.repo_label)
        self.url_label = _label(self.settings.repro.app_url, "mono")
        pl.addWidget(self.url_label)
        lay.addWidget(self._section("Project", project, "project"))
        self.timeline = Timeline(self.STAGES)
        lay.addWidget(self._section("Pipeline", self.timeline, "pipeline"))
        self.repro_message = _label("", "muted")
        lay.addWidget(self.repro_message)
        lay.addStretch(1)
        row2 = _hbox()
        self.report_btn = NButton("Open report")
        self.report_btn.setEnabled(False)
        self.report_btn.clicked.connect(self.repro_open_report.emit)
        cancel = NButton("Cancel", "danger")
        cancel.clicked.connect(self.repro_cancel.emit)
        row2.addWidget(self.report_btn, 2)
        row2.addWidget(cancel, 1)
        lay.addLayout(row2)
        return page

    def _settings_page(self) -> QWidget:
        inner = QWidget()
        lay = _vbox(inner, (0, 0, 4, 0), 14)

        key_card = Surface(radius=14)
        kl = _vbox(key_card, (14, 12, 14, 14), 8)
        self.key_status = _label("", "muted")
        kl.addWidget(self.key_status)
        kr = _hbox()
        self.key_input = QLineEdit()
        self.key_input.setObjectName("field")
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_input.setPlaceholderText("Paste a new key")
        kr.addWidget(self.key_input, 1)
        save = NButton("Save", height=36)
        save.setFixedWidth(70)
        save.clicked.connect(self._save_key)
        kr.addWidget(save)
        kl.addLayout(kr)
        lay.addWidget(self._section("Meta Model API", key_card, "api"))

        beh = Surface(radius=14)
        bl = _vbox(beh, (14, 12, 14, 12), 12)
        s = self.settings
        for key, title, sub, value in (
                ("speak_answers", "Speak answers", "macOS voice", s.speak_answers),
                ("buddy_enabled", "Show the buddy", "Follows your cursor", s.buddy_enabled),
                ("refine_pointing", "Precise pointing", "A second, zoomed look", s.refine_pointing),
                ("confirm_before_acting", "Ask before acting", "Before using the mouse", s.confirm_before_acting),
                ("open_weights", "Open-weights checks", "Muse Spark Open", s.open_weights.enabled)):
            sw = Switch(bool(value))
            sw.toggled.connect(lambda v, k=key: self.setting_changed.emit(k, v))
            bl.addWidget(SettingRow(title, sub, sw))
        lay.addWidget(self._section("Behaviour", beh, "behaviour"))

        perms = Surface(radius=14)
        pl = _vbox(perms, (14, 12, 14, 12), 10)
        self.perm_labels: dict[str, QLabel] = {}
        for perm, title in (("screen", "Screen Recording"), ("accessibility", "Accessibility"),
                            ("microphone", "Microphone")):
            lab = _label(title, None, wrap=False)
            self.perm_labels[perm] = lab
            btn = NButton("Open", height=30)
            btn.setFixedWidth(64)
            btn.clicked.connect(lambda _=False, p=perm: self.open_permissions.emit(p))
            r = _hbox()
            r.addWidget(lab, 1)
            r.addWidget(btn)
            pl.addLayout(r)
        lay.addWidget(self._section("Permissions", perms, "permissions"))
        lay.addStretch(1)
        quit_btn = NButton("Quit Nimbus", "danger", height=34)
        quit_btn.clicked.connect(self.quit_requested.emit)
        lay.addWidget(quit_btn)
        return _scroll(inner)

    def _section(self, title: str, content: QWidget, key: str) -> Section:
        section = Section(title, content, key=key, collapsed=key in self.settings.collapsed_sections)
        section.toggled.connect(self._section_toggled)
        return section

    def _section_toggled(self, key: str, collapsed: bool) -> None:
        keys = set(self.settings.collapsed_sections)
        (keys.add if collapsed else keys.discard)(key)
        self.settings.collapsed_sections = sorted(keys)
        self.setting_changed.emit("collapsed_sections", self.settings.collapsed_sections)

    # ------------------------------------------------------------ open / close

    def _screen_rect(self) -> QRect:
        return QApplication.primaryScreen().availableGeometry()

    def place_tab(self) -> None:
        geo = self._screen_rect()
        y = int(geo.top() + (geo.height() - TAB_H) * self.settings.panel_y_fraction) - 12
        self.tab.rest_x = geo.right() - TAB_W - 11
        self.tab.move(self.tab.rest_x, y)
        self.tab.show()

    def _move_tab(self, global_y: int) -> None:
        geo = self._screen_rect()
        y = max(geo.top() - 4, min(global_y - self.tab.height() // 2, geo.bottom() - self.tab.height() + 4))
        self.tab.move(self.tab.x(), y)
        self.settings.panel_y_fraction = (y + 12 - geo.top()) / max(1, geo.height() - TAB_H)
        self.setting_changed.emit("panel_y_fraction", self.settings.panel_y_fraction)

    def _target_rect(self) -> QRect:
        geo = self._screen_rect()
        w, h = PANEL_W + 2 * SHADOW, min(PANEL_H, geo.height() - 16) + 2 * SHADOW
        center = self.tab.geometry().center().y()
        top = max(geo.top() - SHADOW + 10, min(center - h // 2, geo.bottom() - h + SHADOW - 6))
        home = QRect(geo.right() - w + SHADOW - 8, top, w, h)
        if self._avoid is None or not _body(home).intersects(self._avoid):
            return home
        return QRect(geo.left() - SHADOW + 8, top, w, h)  # the other edge

    def keep_clear(self, rect: QRect | None) -> None:
        """Never cover `rect`, the control the person has to use: slide to the other screen edge, and
        tuck away only when that edge covers it too. `None` lets the panel go home."""
        self._avoid = rect
        if not self.is_open:
            return
        end = self._target_rect()
        if rect is not None and _body(end).intersects(rect):
            self.collapse()
        elif end != self.geometry():
            self._run(self.geometry(), end, self.windowOpacity(), 1.0)

    def open(self) -> None:
        if self.is_open:
            return
        self.is_open = True
        end = self._target_rect()
        start = end.translated(28, 0)
        self.setGeometry(start)
        self.setWindowOpacity(0.0)
        self.show()
        macwin.make_overlay(self, click_through=False, level=macwin.PANEL_LEVEL)
        self.raise_()
        self.activateWindow()
        macwin.activate()
        self.tab.hide()
        self._run(start, end, 0.0, 1.0)
        self.ask_input.setFocus()

    def collapse(self) -> None:
        if not self.is_open:
            return
        self.is_open = False
        group = self._run(self.geometry(), self.geometry().translated(24, 0), 1.0, 0.0, duration=170)
        group.finished.connect(self._after_collapse)

    def _after_collapse(self) -> None:
        if not self.is_open:
            self.hide()
            self.place_tab()

    def toggle(self) -> None:
        self.collapse() if self.is_open else self.open()

    def _run(self, start: QRect, end: QRect, o0: float, o1: float, duration: int = 260) -> QParallelAnimationGroup:
        group = QParallelAnimationGroup(self)
        geo = QPropertyAnimation(self, b"geometry")
        geo.setDuration(duration)
        geo.setEasingCurve(QEasingCurve.Type.OutCubic)
        geo.setStartValue(start)
        geo.setEndValue(end)
        fade = QPropertyAnimation(self, b"windowOpacity")
        fade.setDuration(duration)
        fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        fade.setStartValue(o0)
        fade.setEndValue(o1)
        group.addAnimation(geo)
        group.addAnimation(fade)
        group.start()
        self._anim = group
        return group

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.collapse()
        else:
            super().keyPressEvent(e)

    # ------------------------------------------------------------ actions

    def _fill(self, text: str) -> None:
        self.ask_input.setText(text)
        self.ask_input.setFocus()

    def _submit_ask(self) -> None:
        text = self.ask_input.text().strip()
        if text:
            self.ask_input.clear()
            self.ask_submitted.emit(text, self.act_toggle.isChecked())

    def ask_confirmation(self, text: str) -> None:
        self.open()
        self.show_page(0)
        self.confirm_text.setText(text)
        self.confirm_bar.show()

    def _answer_confirm(self, ok: bool) -> None:
        self.confirm_bar.hide()
        self.confirmed.emit(ok)

    def _pick_tutorial(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose a tutorial video", str(Path.home() / "Movies"),
                                              "Videos (*.mp4 *.mov *.m4v *.webm *.mkv)")
        if path:
            self.shadow_open.emit(path)

    def _pick_bug_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose a bug recording", str(Path.home() / "Movies"),
                                              "Videos (*.mp4 *.mov *.m4v *.webm)")
        if path:
            self.repro_video.emit(path)

    def _toggle_record(self, on: bool) -> None:
        self.record_btn.setText("■  Stop recording" if on else "●  Record bug")
        self.repro_record.emit(on)

    def _save_key(self) -> None:
        key = self.key_input.text().strip()
        if key:
            self.key_input.clear()
            self.api_key_entered.emit(key)

    # ------------------------------------------------------------ updates from the app

    def set_status(self, text: str, rgb=theme.MUTED, busy: bool | None = None) -> None:
        if busy is None:
            busy = rgb in (theme.THINKING, theme.LISTENING, theme.ACCENT)
        self.status_pill.set(text, rgb, busy)
        self.tab.set_status(rgb, busy)

    def show_answer(self, question: str, answer_html: str, index: int | None = None, plain: str = "") -> None:
        self.empty_ask.hide()
        card = MessageCard(question, answer_html, time.strftime("%H:%M"), index, plain or _strip_tags(answer_html))
        card.replay.connect(self.replay_requested.emit)
        self.convo_lay.insertWidget(self.convo_lay.count() - 1, card)
        QTimer.singleShot(30, lambda: self.convo_scroll.verticalScrollBar().setValue(
            self.convo_scroll.verticalScrollBar().maximum()))

    def set_key_status(self, ok: bool, detail: str = "") -> None:
        self.key_status.setText(("\u2713  Connected" if ok else "No key yet")
                                + (f"  ·  {detail}" if detail else ""))
        self.key_status.setStyleSheet(f"color: {theme.hex_rgb(theme.SUCCESS if ok else theme.THINKING)};")

    def set_permission(self, perm: str, ok: bool) -> None:
        names = {"screen": "Screen Recording", "accessibility": "Accessibility", "microphone": "Microphone"}
        lab = self.perm_labels.get(perm)
        if lab:
            mark = "✓" if ok else "!"
            lab.setText(f"{mark}   {names[perm]}")
            lab.setStyleSheet(f"color: {theme.hex_rgb(theme.SUCCESS if ok else theme.THINKING)}; font-weight: 600;")

    def shadow_loading(self, message: str, fraction: float) -> None:
        self.show_page(1)
        self.drop.hide()
        self.loading.show()
        self.load_ring.set_spinning(fraction < 0.05)
        self.load_ring.set_value(fraction, f"{int(fraction * 100)}%")
        self.shadow_status.setText(message)

    def shadow_ready(self, title: str, summary: str) -> None:
        self.loading.hide()
        self.drop.hide()
        self.shadow_title.setText(title)
        self.shadow_meta.setText(summary)
        self.session.show()
        self.show_page(SHADOW_PAGE)

    def shadow_step(self, index: int, total: int, instruction: str, expected: str, t: float | None,
                    crop: Path | None) -> None:
        self.session.show()
        self.show_page(SHADOW_PAGE)  # opening the panel mid-tutorial lands on the step and its buttons
        self.step_card.show_step(index, total, instruction, expected, t, crop)
        self.shadow_verdict.setText("")

    def shadow_verdict_text(self, text: str, ok: bool | None) -> None:
        color = theme.TEXT_SECONDARY if ok is None else theme.hex_rgb(theme.SUCCESS if ok else theme.THINKING)
        self.shadow_verdict.setStyleSheet(f"color: {color}; font-size: 12.5px;")
        self.shadow_verdict.setText(text)

    def shadow_finished(self, text: str) -> None:
        self.session.hide()
        self.loading.hide()
        self.drop.show()
        self.shadow_verdict.setText("")
        self.shadow_status.setText(text)

    def set_recent_guides(self, guides: list[tuple[str, str]]) -> None:
        while self.recent_box.count():
            item = self.recent_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for plan_dir, title in guides[:12]:
            name, _, meta = title.partition("  ·  ")
            row = GuideRow(name, meta or "Tutorial")
            row.clicked.connect(lambda _=False, d=plan_dir: self.shadow_resume.emit(d))
            self.recent_box.addWidget(row)
        self.recent_box.addStretch(1)
        self.recent_label.setVisible(bool(guides))

    def repro_stage(self, stage: str, message: str, fraction: float, state: str = "active") -> None:
        self.timeline.set_state(stage, state, message)
        # Live detail sits under the active stage; the line below the timeline is for the outcome only.
        if message and (state == "failed" or fraction >= 1.0):
            self.repro_message.setText(message)

    def repro_reset(self) -> None:
        self.show_page(2)  # a new run shows itself once; progress never pulls the person off another page
        self.timeline.reset()
        self.repro_message.setText("")
        self.report_btn.setEnabled(False)


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _strip_tags(html_text: str) -> str:
    import html as _html
    import re as _re

    return _html.unescape(_re.sub(r"<li>", "\n\u2022 ", _re.sub(r"<(?!li)[^>]+>", "", html_text))).strip()


def _short_path(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path.startswith(home) else path


def _pretty_hotkey(hotkey: str) -> str:
    names = {"<ctrl>": "⌃", "<alt>": "⌥", "<cmd>": "⌘", "<shift>": "⇧", "<space>": " Space"}
    return "".join(names.get(part, part.upper()) for part in hotkey.split("+"))


def _body(rect: QRect) -> QRect:
    """The visible card of a panel geometry, without its drop-shadow margin."""
    return rect.adjusted(SHADOW, SHADOW, -SHADOW, -SHADOW)

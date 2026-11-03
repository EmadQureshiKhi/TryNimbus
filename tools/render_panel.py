"""Render every panel state to PNG without showing a window (safe to run while using the Mac).

    .venv/bin/python tools/render_panel.py <out_dir>
"""
from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPixmap, QRadialGradient
from PyQt6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # runnable as tools/<name>.py

from nimbus import config  # noqa: E402
from nimbus.ui import theme  # noqa: E402
from nimbus.ui.panel import PANEL_H, PANEL_W, SHADOW, EdgeTab, Panel  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def backdrop(w: int, h: int) -> QPixmap:
    pm = QPixmap(w, h)
    p = QPainter(pm)
    g = QLinearGradient(0, 0, w, h)
    g.setColorAt(0, QColor("#3B4A6B"))
    g.setColorAt(1, QColor("#C9B7A6"))
    p.fillRect(0, 0, w, h, g)
    r = QRadialGradient(w * 0.2, h * 0.8, w * 0.7)
    r.setColorAt(0, QColor(255, 140, 80, 90))
    r.setColorAt(1, QColor(255, 140, 80, 0))
    p.fillRect(0, 0, w, h, r)
    p.end()
    return pm


def shot(widget, path: Path, pad: int = 30, dpr: float = 2.0) -> None:
    """Paint `widget` at Retina scale on the backdrop, whatever screen (or none) Qt is on."""
    widget.ensurePolished()
    img = QPixmap(widget.size() * dpr)
    img.setDevicePixelRatio(dpr)
    img.fill(Qt.GlobalColor.transparent)
    widget.render(img)
    bg = backdrop(int((img.width() / dpr + 2 * pad) * dpr), int((img.height() / dpr + 2 * pad) * dpr))
    bg.setDevicePixelRatio(dpr)
    p = QPainter(bg)
    p.drawPixmap(pad, pad, img)
    p.end()
    bg.save(str(path))


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "artifacts" / "ui")
    out.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv)
    settings = config.Settings()
    panel = Panel(settings)
    panel.resize(PANEL_W + 2 * SHADOW, PANEL_H + 2 * SHADOW)
    panel.set_status("Ready", theme.SUCCESS, busy=False)
    panel.set_key_status(True)
    for perm, ok in (("screen", True), ("accessibility", False), ("microphone", True)):
        panel.set_permission(perm, ok)

    def render(name: str) -> None:
        from PyQt6.QtTest import QTest

        QTest.qWait(450)  # let tab slides, fades and rings settle
        shot(panel, out / f"{name}.png")

    panel.show_page(0); render("1_ask_empty")
    panel.set_status("Thinking", theme.THINKING)
    panel.show_answer("How do I start a new chat?",
                      "Click <b>New chat</b> at the top of the sidebar. I'm pointing at it now."
                      "<ol style='margin:4px 0 0 -18px'><li>Click New chat (top left)</li>"
                      "<li>Type your message in the box at the bottom</li></ol>", 0)
    panel.show_answer("Solve problem 1", "Subtract 3 from both sides, then divide by 2: <b>x = 4</b>.", 1)
    from nimbus.ui.panel import MessageCard
    cards = panel.conversation.findChildren(MessageCard)
    for card in cards:
        card.finish_typing()
    cards[0].enterEvent(None)  # show the hover actions on the first answer
    panel.ask_confirmation("Nimbus wants to control your Mac to: open Cutline and switch to the Color page.\n"
                           "Press Esc at any time to stop.")
    panel.set_status("Ready", theme.SUCCESS)
    render("2_ask_conversation")
    panel.confirm_bar.hide()

    panel.show_page(1)
    panel.set_recent_guides([("/tmp/a", "Switch models in Chatbook  ·  5 steps"),
                             ("/tmp/b", "Color grade a clip  ·  9 steps")])
    render("3_shadow_empty")
    panel.shadow_loading("Watching the video with Muse Spark", 0.42)
    render("4_shadow_loading")
    panel.shadow_ready("Switch models in Chatbook", "5 steps · Chatbook (web)")
    crop = next((ROOT / "artifacts" / "evals" / "shadow_plan").glob("step01_target.png"), None)
    panel.shadow_step(1, 5, "Open the model picker next to the send button.",
                      "A menu lists Muse Spark 1.3, Muse Spark 1.2 and Muse Wonder.", 8.4, crop)
    panel.shadow_verdict_text("✓ The model menu is open with three options.", True)
    render("5_shadow_step")

    panel.show_page(2)
    panel.repro_stage("analyze", "4 steps found in a 25 s recording", 0.15, "done")
    panel.repro_stage("launch", "App ready at 127.0.0.1:5173", 0.25, "done")
    panel.repro_stage("reproduce", "Total shows $79.00, expected $158.00", 0.45, "done")
    panel.repro_stage("fix", "Muse Code is writing a regression test", 0.6, "active")
    render("6_repro_running")

    panel.show_page(3)
    render("7_settings")
    for sec in panel.findChildren(__import__("nimbus.ui.widgets", fromlist=["Section"]).Section):
        if sec.key in ("behaviour", "permissions"):
            sec.set_collapsed(True, animate=False)
    render("8_settings_folded")

    tab = EdgeTab()
    tab.set_status(theme.THINKING, busy=True)
    app.processEvents()
    shot(tab, out / "0_tab.png", pad=20)
    print("rendered to", out)


if __name__ == "__main__":
    main()

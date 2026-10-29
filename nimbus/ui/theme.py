"""Design tokens: dark surfaces, Nimbus orange accent."""
from __future__ import annotations

from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "assets"

ACCENT = (255, 122, 26)
ACCENT_LIGHT = (255, 161, 85)
ACCENT_DEEP = (217, 96, 10)
# Tuned to the warm-dark surfaces and the orange brand rather than stock UI greens and reds.
LISTENING = (110, 220, 168)   # mint, the microphone is live
THINKING = (255, 196, 92)     # honey amber
SUCCESS = (122, 222, 162)     # mint sage
DANGER = (255, 105, 135)      # raspberry rose (clearly apart from the orange accent)
DANGER_DEEP = (58, 20, 32)    # wine, behind destructive buttons
MUTED = (138, 132, 128)

BG_BASE = "#0B0B0D"
BG_ELEVATED = "#141417"
BG_RAISED = "#191920"
BG_HOVER = "#1F1F26"
BG_ACTIVE = "#26262E"
PANEL_TOP = "#1C1714"
PANEL_BOTTOM = "#141110"
BORDER = "#26262B"
BORDER_STRONG = "#33333A"
TEXT_PRIMARY = "#F5F5F7"
TEXT_SECONDARY = "#A1A1AA"
TEXT_MUTED = "#8A8A94"


def hex_rgb(rgb: tuple[int, int, int]) -> str:
    return "#%02X%02X%02X" % rgb


_FAMILY: str | None = None


# Type roles: display for titles, body for reading, label for small caps and numbers, mono for paths.
DISPLAY_FAMILY = "Avenir Next"
LABEL_FAMILY = "DIN Alternate"
MONO_FAMILY = "Menlo"


def font(size: float, weight: str = "regular", role: str = "body"):
    """A font for a role; body is the macOS system font (SF), resolved once from Qt's font database.
    Roles fall back to the system font if a family is missing."""
    global _FAMILY
    from PyQt6.QtGui import QFont, QFontDatabase

    if _FAMILY is None:
        _FAMILY = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()
    family = {"display": DISPLAY_FAMILY, "label": LABEL_FAMILY, "mono": MONO_FAMILY}.get(role, _FAMILY)
    if family != _FAMILY and family not in QFontDatabase.families():
        family = _FAMILY
    f = QFont(family)
    f.setPointSizeF(size)
    f.setWeight({"regular": QFont.Weight.Normal, "medium": QFont.Weight.Medium,
                 "semibold": QFont.Weight.DemiBold, "bold": QFont.Weight.Bold}[weight])
    if role == "label":
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.6)
    return f

# The buddy's pointer silhouette, matching assets/cursor.png (tip at the origin).
CURSOR_VERTICES = [
    (0.0, 0.0), (3.0, 0.0), (23.1, 12.8), (23.7, 13.9), (23.1, 15.9), (13.2, 18.7),
    (8.3, 27.1), (7.1, 27.9), (5.7, 27.7), (4.8, 26.4), (0.0, 1.7),
]

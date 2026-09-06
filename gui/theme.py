"""
redactor_common/gui/theme.py

One shared fix, applied identically across every Redactor app, for:
"can't see what is selected in dark mode" (a real complaint from a
cbzredactor session) -- and, more generally, for the four apps looking
and behaving differently from each other for no reason other than none
of them ever set an explicit style/palette.

None of the four apps touch QApplication's style or palette at all --
each just inherits whatever Qt's native platform style ("windowsvista"/
"windows11" on Windows) renders by default. That native style's
dark-mode approximation of QPalette's Highlight/HighlightedText roles
(the colors a QTableWidget/QListWidget row uses while selected) is
often low-contrast -- background and text too close in value to tell a
selected row apart from an unselected one at a glance. This is the
native style's own weak dark-mode support, identical in all four apps
since none of them touch styling -- hence one shared, uniform fix
instead of four separate per-app patches.

apply_theme() switches to the cross-platform "Fusion" style (immune to
a native style's own per-platform dark-mode quirks -- Fusion always
renders from the QPalette you give it, nothing else) and applies an
explicit palette, chosen (light or dark) by asking Qt what the OS is
actually set to (QStyleHints.colorScheme(), Qt >= 6.5) rather than
guessing. Both palettes' Highlight/HighlightedText pair -- the thing
that was actually broken -- is verified against the WCAG contrast-ratio
formula in this repo's tests/test_theme.py, not eyeballed: every pair
below clears 4.5:1 (the standard normal-text-readability threshold),
and each Highlight color clears 3:1 against its own Base color too (an
unselected row's own background), so a selected row is unmistakably
different from one that isn't, not just "technically readable text on
top of it."
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

_LIGHT: dict[str, tuple[int, int, int]] = {
    "Window": (240, 240, 240),
    "WindowText": (0, 0, 0),
    "Base": (255, 255, 255),
    "AlternateBase": (245, 245, 245),
    "ToolTipBase": (255, 255, 220),
    "ToolTipText": (0, 0, 0),
    "Text": (0, 0, 0),
    "Button": (240, 240, 240),
    "ButtonText": (0, 0, 0),
    "BrightText": (255, 0, 0),
    "Highlight": (43, 108, 196),
    "HighlightedText": (255, 255, 255),
    "PlaceholderText": (120, 120, 120),
}

_DARK: dict[str, tuple[int, int, int]] = {
    "Window": (45, 45, 45),
    "WindowText": (220, 220, 220),
    "Base": (30, 30, 30),
    "AlternateBase": (45, 45, 45),
    "ToolTipBase": (60, 60, 45),
    "ToolTipText": (220, 220, 220),
    "Text": (220, 220, 220),
    "Button": (55, 55, 55),
    "ButtonText": (220, 220, 220),
    "BrightText": (255, 90, 90),
    # Medium-bright blue + near-black text (NOT white-on-blue -- a dark
    # enough blue to read white text well is too close in value to the
    # (30,30,30) Base background to look selected at all; this pairing
    # clears strong contrast against both).
    "Highlight": (90, 160, 240),
    "HighlightedText": (20, 20, 20),
    "PlaceholderText": (150, 150, 150),
}

_ROLE_MAP: dict[str, QPalette.ColorRole] = {
    "Window": QPalette.ColorRole.Window,
    "WindowText": QPalette.ColorRole.WindowText,
    "Base": QPalette.ColorRole.Base,
    "AlternateBase": QPalette.ColorRole.AlternateBase,
    "ToolTipBase": QPalette.ColorRole.ToolTipBase,
    "ToolTipText": QPalette.ColorRole.ToolTipText,
    "Text": QPalette.ColorRole.Text,
    "Button": QPalette.ColorRole.Button,
    "ButtonText": QPalette.ColorRole.ButtonText,
    "BrightText": QPalette.ColorRole.BrightText,
    "Highlight": QPalette.ColorRole.Highlight,
    "HighlightedText": QPalette.ColorRole.HighlightedText,
    "PlaceholderText": QPalette.ColorRole.PlaceholderText,
}


def build_palette(colors: dict[str, tuple[int, int, int]]) -> QPalette:
    """Exposed mainly for tests/test_theme.py's contrast-ratio checks
    (which verify _LIGHT/_DARK directly, not through this), but also
    lets a project build a variant palette starting from the same base
    if it ever needs one."""
    palette = QPalette()
    for name, role in _ROLE_MAP.items():
        palette.setColor(role, QColor(*colors[name]))
    return palette


def is_dark_mode(app: QApplication) -> bool:
    """Best-effort OS dark-mode detection via QStyleHints.colorScheme()
    (Qt >= 6.5). On an older Qt lacking that API, defaults to light --
    the app's look before this fix existed -- rather than guessing from
    something less direct."""
    try:
        return app.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:
        return False


def apply_theme(app: QApplication) -> None:
    """Call once, right after constructing QApplication and before any
    window is shown. Every Redactor app calling this at startup is what
    makes selection (and general light/dark appearance) look and behave
    identically across all of them, rather than each just inheriting
    whatever the native style happens to render."""
    app.setStyle("Fusion")
    app.setPalette(build_palette(_DARK if is_dark_mode(app) else _LIGHT))

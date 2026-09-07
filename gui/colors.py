"""
redactor_common/gui/colors.py

Shared color palette, standardized on the epub tool's scheme. The
other two projects had each independently picked their own row-tint
and selection-highlight colors, so the same visual state (an unsaved
edit, a load/save error, the selected row) looked different depending
on which sibling app happened to be open. One canonical set now.

DIRTY_COLOR/ERROR_COLOR/SAVE_FAILED_COLOR/DRM_COLOR are all light, so
they're paired with the single explicit HIGHLIGHT_TEXT_COLOR (dark) --
this keeps them readable regardless of whether the OS/app is in light
or dark mode. Non-highlighted rows should deliberately NOT set an
explicit background/foreground at all, so they just inherit the
current theme's normal palette instead of fighting it.

DRM_COLOR and SAVE_FAILED_COLOR are epub-specific states with no
equivalent in mp3/video yet -- import only what a given project
actually needs.

TABLE_SELECTION_STYLESHEET: a bright, theme-independent outline for
the current cell (relevant for typing and Tab/Enter navigation), so
it's visible even within a selected row.

2026-09-07 fix: this used to ALSO hardcode the selected row's own
background-color/color ("#2f6fed"/white) here. That was a real
regression once gui/theme.py (apply_theme()) started giving every app
an explicit, WCAG-contrast-verified light/dark QPalette -- a widget-
level Qt stylesheet always wins over the application palette for that
widget, so this stylesheet was silently overriding apply_theme()'s
carefully different light-vs-dark selection colors with one fixed pair
that was never verified for both, on every app that called both (epub,
mp3, video -- cbzredactor never imported this module, so its table
selection was the only one actually running on apply_theme()'s
colors). One of video's own commit-time comments even flagged this
exact risk ("the shared #2f6fed hasn't been re-verified against a dark
theme here") and it was waved through anyway, before apply_theme()
existed to give a real, verified answer. Removed the conflicting rule
-- selection background/text now come from apply_theme()'s palette
everywhere, uniformly, the way "uniform behaviour across the apps" was
supposed to work in the first place.
"""

from __future__ import annotations

from PyQt6.QtGui import QColor

DIRTY_COLOR = QColor("#fff3cd")        # soft amber = unsaved change
ERROR_COLOR = QColor("#f8d7da")        # soft red = failed to load
SAVE_FAILED_COLOR = QColor("#ffddb3")  # soft orange = failed to SAVE (distinct from load/validation problems)
DRM_COLOR = QColor("#dce6fb")          # soft blue = DRM-protected (not broken, just locked)
HIGHLIGHT_TEXT_COLOR = QColor("#000000")

FOCUS_BORDER_COLOR = "#ffb400"

TABLE_SELECTION_STYLESHEET = f"QTableWidget::item:focus {{ border: 2px solid {FOCUS_BORDER_COLOR}; }}"

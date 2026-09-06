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

TABLE_SELECTION_STYLESHEET: strong, theme-independent selection/
current-cell indicators for a QTableWidget -- otherwise the default
look can blend into a project's own custom row colors (dirty/status
highlighting) and make it hard to tell where you clicked. Selection
color takes priority over a row's dirty/status color while selected;
the current cell (relevant for typing and Tab/Enter navigation) gets
its own bright outline so it's visible even within a selected row.
"""

from __future__ import annotations

from PyQt6.QtGui import QColor

DIRTY_COLOR = QColor("#fff3cd")        # soft amber = unsaved change
ERROR_COLOR = QColor("#f8d7da")        # soft red = failed to load
SAVE_FAILED_COLOR = QColor("#ffddb3")  # soft orange = failed to SAVE (distinct from load/validation problems)
DRM_COLOR = QColor("#dce6fb")          # soft blue = DRM-protected (not broken, just locked)
HIGHLIGHT_TEXT_COLOR = QColor("#000000")

SELECTION_BG = "#2f6fed"
SELECTION_FG = "white"
FOCUS_BORDER_COLOR = "#ffb400"

TABLE_SELECTION_STYLESHEET = (
    f"QTableWidget::item:selected {{ background-color: {SELECTION_BG}; color: {SELECTION_FG}; }}"
    f"QTableWidget::item:focus {{ border: 2px solid {FOCUS_BORDER_COLOR}; }}"
)

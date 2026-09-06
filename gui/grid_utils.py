"""
redactor_common/gui/grid_utils.py

One small, common QGridLayout gotcha: a grid built from a fixed set of
rows (checkbox + label + editor, one row per field -- the bulk-edit
tag panels every sibling project has some version of) has zero stretch
on every row by default. When that grid is given more height than its
content actually needs -- a QScrollArea's setWidgetResizable(True)
stretching it to fill the viewport, or the grid's container being
given a stretch factor in an outer layout -- Qt's default is to spread
the leftover space EVENLY into the gap after every single row, rather
than leaving it as blank space below the last one. The visible
symptom: the gaps between every bulk-edit field visibly grow as the
window/panel is resized taller (reported on mp3, but the same latent
shape of bug in epub's identically-structured grid too).
"""

from __future__ import annotations

from PyQt6.QtWidgets import QGridLayout


def absorb_extra_row_space(grid: QGridLayout, content_row_count: int) -> None:
    """Adds a stretch-factor-1 phantom row right after the real content
    (row index == content_row_count, one past the last real row) so any
    leftover vertical space collects there as blank space instead of
    being spread evenly into the gaps between the real rows above it.

    Call once after adding all of a fixed-row-count grid's real content
    -- and again after any full rebuild (e.g. a field-visibility
    change that tears down and re-adds a different number of rows).
    Safe to call more than once even if the row count shrinks on a
    later rebuild -- an earlier, now-higher-than-necessary phantom row
    left with stretch is harmless (it's still just blank space; Qt
    doesn't create a visible extra gap between two stretchy empty rows,
    they simply share the same leftover space)."""
    grid.setRowStretch(content_row_count, 1)

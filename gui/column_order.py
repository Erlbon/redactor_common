"""
redactor_common/gui/column_order.py

Two small helpers shared by every app's Tools > Columns handler, so the
dialog and the table agree on what "the current order" is:

* visual_column_keys(header, keys_by_logical): the column keys in the order
  the user SEES them (header.logicalIndex(visual) order, hidden columns
  included at their position).
* apply_column_order(header, order, logical_of): moves the header sections
  so the visual order matches `order` (a list of keys). Keys the table does
  not have are ignored; columns not named in `order` keep their relative
  order after the named ones. The header's signals are blocked while moving,
  so a persist-on-sectionMoved handler does not run once per step: the
  caller persists the final order once afterwards.
"""

from __future__ import annotations

from PyQt6.QtWidgets import QHeaderView

from redactor_common.core.table_settings import merge_column_order


def visual_column_keys(header: QHeaderView, keys_by_logical: list[str]) -> list[str]:
    """Keys in visual (left to right) order. keys_by_logical[i] is the key
    of logical column i."""
    keys: list[str] = []
    for visual in range(header.count()):
        logical = header.logicalIndex(visual)
        if 0 <= logical < len(keys_by_logical):
            keys.append(keys_by_logical[logical])
    return keys


def apply_column_order(header: QHeaderView, order: list[str], logical_of: dict[str, int]) -> bool:
    """Make the header's visual order follow `order`. Returns True if any
    section actually moved."""
    current = sorted(logical_of, key=lambda k: header.visualIndex(logical_of[k]))
    wanted = merge_column_order(order, current)
    moved = False
    was_blocked = header.blockSignals(True)
    try:
        for position, key in enumerate(wanted):
            visual = header.visualIndex(logical_of[key])
            if visual != position:
                header.moveSection(visual, position)
                moved = True
    finally:
        header.blockSignals(was_blocked)
    if moved:
        header.viewport().update()
    return moved

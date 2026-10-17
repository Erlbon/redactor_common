"""
redactor_common/gui/quality_colour.py

The one painter of the quality colours (core/quality_bands.py) on a table cell, shared by cbzredactor's Size column,
videoredactor's and mp3redactor's Bitrate column:

    apply_band_colour(item, result, failed=False)

The full explanation always becomes the cell's tooltip. The band's solid light tint and the dark text colour are set
only when the band has a colour (not UNKNOWN) and the item did not fail: a failed file's red "this file is broken" tint
matters more and is never overpainted. Call it AFTER the row tint (dirty, error...), so the band colour stays visible
on a modified row.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtGui import QColor

from redactor_common.core.quality_bands import BAND_COLORS, BandResult
from redactor_common.gui.colors import HIGHLIGHT_TEXT_COLOR


def band_qcolor(band) -> Optional[QColor]:
    hex_value = BAND_COLORS.get(band)
    return QColor(hex_value) if hex_value else None


def apply_band_colour(item, result: BandResult, failed: bool = False) -> bool:
    """Paints `item` (a QTableWidgetItem) for `result`; True when a colour was applied."""
    item.setToolTip(result.tooltip)
    colour = band_qcolor(result.band)
    if colour is None or failed:
        return False
    item.setBackground(colour)
    item.setForeground(HIGHLIGHT_TEXT_COLOR)
    return True

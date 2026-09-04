"""
redactor_common/gui/zoom_toolbar.py

The "+ / 100% / -" table-font-zoom toolbar control, lifted from the
epub project (the video project never had this). Adjusts a
QTableWidget's (and its headers') font size, re-fits row heights, and
shows the current size as a percentage of whatever size the table
started at -- not persisted across sessions, purely a this-window
display preference for fitting more (or more readable) content.

Usage:
    from redactor_common.gui.zoom_toolbar import TableZoomController

    self.zoom = TableZoomController(self.table)
    toolbar.addAction(self.zoom.zoom_out_action)
    toolbar.addWidget(self.zoom.label)
    toolbar.addAction(self.zoom.zoom_in_action)
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence
from PyQt6.QtWidgets import QLabel, QTableWidget

from redactor_common.gui.action_factory import make_action

TABLE_ZOOM_MIN_PT = 6
TABLE_ZOOM_MAX_PT = 24


class TableZoomController:
    def __init__(self, table: QTableWidget, parent=None):
        self.table = table
        self._default_pt = table.font().pointSize()

        owner = parent or table
        self.zoom_out_action = make_action(
            owner, "\u2212", self.zoom_out,
            shortcut=QKeySequence.StandardKey.ZoomOut,
            tooltip="Decrease table font size",
        )
        self.zoom_in_action = make_action(
            owner, "+", self.zoom_in,
            shortcut=QKeySequence.StandardKey.ZoomIn,
            tooltip="Increase table font size",
        )

        self.label = QLabel("100%")
        self.label.setMinimumWidth(44)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setToolTip("Table font size, relative to the default (100%). Click to reset.")
        self.label.mousePressEvent = lambda _event: self.zoom_reset()

    def zoom_in(self) -> None:
        self._adjust(1)

    def zoom_out(self) -> None:
        self._adjust(-1)

    def zoom_reset(self) -> None:
        self._set_font_size(self._default_pt)

    def _adjust(self, delta: int) -> None:
        new_size = max(TABLE_ZOOM_MIN_PT, min(TABLE_ZOOM_MAX_PT, self.table.font().pointSize() + delta))
        self._set_font_size(new_size)

    def _set_font_size(self, point_size: int) -> None:
        font = self.table.font()
        if point_size == font.pointSize():
            return
        font.setPointSize(point_size)
        self.table.setFont(font)
        self.table.horizontalHeader().setFont(font)
        self.table.verticalHeader().setFont(font)
        self.table.resizeRowsToContents()
        percent = round(point_size / self._default_pt * 100)
        self.label.setText(f"{percent}%")

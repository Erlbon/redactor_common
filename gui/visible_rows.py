"""
redactor_common/gui/visible_rows.py

VisibleRowsWatcher: tells a table's owner which rows are currently on
screen (plus a small buffer above and below), whenever that set may have
changed -- scrolling, resizing, sorting, rows being added or removed --
debounced so a scroll-wheel flick triggers one check, not one per pixel.

This is the lazy half of epub's lazy cover loading (promoted
2026-09-23): a table rebuild only shows ALREADY-cached icons, and real
decodes are requested only for rows the user can actually see. A user
only ever looks at a couple of dozen rows at once regardless of library
size, so decoding every cover up front was almost entirely wasted work:
measured ~126s -> ~2.6s for a 15,462-book table rebuild. The icon
decoding itself is async_icon_cache.AsyncIconCache; this module only
decides WHEN and FOR WHICH ROWS to ask.

Row-mapping agnostic: the callback receives visual row indices, and the
caller maps them to its own items -- Qt.UserRole data (epub, mp3,
video), or cbz's own list-position order.

Usage:
    self._visible_rows = VisibleRowsWatcher(self.table, self._load_icons_for_rows)

    def _load_icons_for_rows(self, rows: list[int]) -> None:
        for row in rows:
            book = self._book_for_row(row)
            ...  # request that book's icon

    # after repopulating the table:
    self._visible_rows.check_now()
"""

from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import QEvent, QObject, QTimer
from PyQt6.QtWidgets import QTableView

DEFAULT_BUFFER_ROWS = 15
DEFAULT_DEBOUNCE_MS = 150


class VisibleRowsWatcher(QObject):
    def __init__(
        self,
        table: QTableView,
        on_visible_rows: Callable[[list[int]], None],
        buffer_rows: int = DEFAULT_BUFFER_ROWS,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
    ):
        super().__init__(table)
        self._table = table
        self._callback = on_visible_rows
        self._buffer_rows = buffer_rows
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(debounce_ms)
        self._timer.timeout.connect(self.check_now)

        table.verticalScrollBar().valueChanged.connect(self.schedule)
        table.horizontalHeader().sortIndicatorChanged.connect(self.schedule)
        # The viewport, not the table: a splitter drag or a side-panel
        # collapse resizes it without the table itself necessarily
        # getting a resizeEvent of its own.
        table.viewport().installEventFilter(self)
        model = table.model()
        if model is not None:
            model.rowsInserted.connect(self.schedule)
            model.rowsRemoved.connect(self.schedule)
            model.modelReset.connect(self.schedule)
            model.layoutChanged.connect(self.schedule)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self.schedule()
        return False

    def schedule(self, *_args) -> None:
        """Restarts the debounce timer. `*_args` absorbs whatever the
        connected signal passes; the check always reads the current
        viewport fresh."""
        self._timer.start()

    def visible_rows(self) -> list[int]:
        """Visible, non-hidden rows plus `buffer_rows` either side, top
        to bottom. Empty for an empty table."""
        table = self._table
        row_count = table.model().rowCount() if table.model() is not None else 0
        if row_count == 0:
            return []
        viewport_height = table.viewport().height()
        first = table.rowAt(0)
        last = table.rowAt(max(0, viewport_height - 1))
        if first == -1:
            first = 0
        if last == -1:
            last = row_count - 1
        first = max(0, first - self._buffer_rows)
        last = min(row_count - 1, last + self._buffer_rows)
        return [row for row in range(first, last + 1) if not table.isRowHidden(row)]

    def check_now(self) -> None:
        """Runs the callback immediately (e.g. right after a rebuild)."""
        self._timer.stop()
        rows = self.visible_rows()
        if rows:
            self._callback(rows)

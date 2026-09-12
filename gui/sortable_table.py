"""
redactor_common/gui/sortable_table.py

Click-a-header-to-sort support for a QTableWidget, safe to combine with
a row -> item mapping via Qt.UserRole (NOT safe for a project whose
code indexes rows by list position -- see cbzredactor's own deliberate
non-native sort implementation for why: it re-sorts its own list and
rebuilds the table instead, since Qt's native sort physically moves
QTableWidgetItems between rows out from under any "row N is
self.items[N]" assumption). Two small, independent pieces:

- NumericTableWidgetItem: a QTableWidgetItem that compares numerically
  when it can, instead of Qt's default text comparison (which sorts
  "10" before "9" -- that's a string compare, not a numeric one).
  Accepts an optional explicit sort_value for a column whose displayed
  text isn't itself a bare number (e.g. "128.5 LUFS", "44100 Hz") --
  re-parsing the suffix back out of the text on every comparison would
  be wasteful and fragile; pass the real float once when the item is
  built instead. Falls back to Qt's normal text comparison whenever
  neither side has a usable number (blank cells, "TOOL MISSING",
  etc.), so a non-numeric cell never crashes the comparison.

- suspend_sorting(table): a context manager that disables
  setSortingEnabled() for its duration and restores whatever state was
  in effect before. REQUIRED around any block that calls setItem() more
  than once while populating a table -- Qt re-sorts as items land when
  sorting is enabled, so a plain populate loop (set row 0's items, set
  row 1's items, ...) can have row 0 relocated by a live re-sort before
  row 1 is even written, silently scrambling which row ends up holding
  which item. Promoted from epubredactor's own repeated
  "was_sorting = table.isSortingEnabled(); table.setSortingEnabled(False)
  ...; table.setSortingEnabled(was_sorting)" blocks (nine of them,
  hand-written at every one of its own bulk-repopulate call sites).

Usage:
    from redactor_common.gui.sortable_table import NumericTableWidgetItem, suspend_sorting

    self.table.setSortingEnabled(True)  # once, at table setup -- that's
                                         # the whole opt-in; Qt handles
                                         # the header clicks/arrows itself

    def _rebuild_table(self):
        with suspend_sorting(self.table):
            self.table.setRowCount(len(self.files))
            for row, mp3 in enumerate(self.files):
                self._populate_row(row, mp3)

    def _populate_row(self, row, mp3):
        item = NumericTableWidgetItem(mp3.display_bpm())  # auto-parses "128.5"
        ...
        item = NumericTableWidgetItem(mp3.display_loudness(), sort_value=mp3.loudness_lufs)  # "-14.2 LUFS" needs the real float, not text-parsing
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional

from PyQt6.QtWidgets import QTableWidget, QTableWidgetItem


class NumericTableWidgetItem(QTableWidgetItem):
    def __init__(self, text: str, sort_value: Optional[float] = None):
        super().__init__(text)
        if sort_value is not None:
            self._sort_value: Optional[float] = sort_value
        else:
            try:
                self._sort_value = float(text)
            except (ValueError, TypeError):
                self._sort_value = None

    def __lt__(self, other) -> bool:
        other_value = getattr(other, "_sort_value", None)
        if self._sort_value is not None and other_value is not None:
            return self._sort_value < other_value
        return super().__lt__(other)


@contextmanager
def suspend_sorting(table: QTableWidget) -> Iterator[None]:
    was_sorting = table.isSortingEnabled()
    table.setSortingEnabled(False)
    try:
        yield
    finally:
        table.setSortingEnabled(was_sorting)

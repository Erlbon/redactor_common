"""
redactor_common/gui/path_column.py

Qt side of the shared Path column (see core/path_column.py for the rules):

    item = PathTableWidgetItem(path)          # shows the folder, tooltip = full path, sorts naturally
    item.set_path(new_path)                   # after a rename/move
    install_cell_copy(table)                  # once: Ctrl+C copies the current column of the selected rows,
                                              # the Path column as FULL file paths (one per line)
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtGui import QGuiApplication, QKeySequence
from PyQt6.QtWidgets import QAbstractItemView, QTableWidgetItem

from redactor_common.core.path_column import folder_of, natural_sort_key, path_tooltip

# Item data role carrying the full text to copy (the full path for a Path cell).
COPY_TEXT_ROLE = int(Qt.ItemDataRole.UserRole) + 7301
LINE_BREAK = chr(10)


class PathTableWidgetItem(QTableWidgetItem):
    def __init__(self, path=None):
        super().__init__("")
        self.set_path(path)

    def set_path(self, path) -> None:
        self.setText(folder_of(path))
        full = path_tooltip(path)
        self.setToolTip(full)
        self.setData(COPY_TEXT_ROLE, full)

    def __lt__(self, other) -> bool:
        if isinstance(other, QTableWidgetItem):
            return natural_sort_key(self.text()) < natural_sort_key(other.text())
        return super().__lt__(other)


def selected_column_text(table) -> str:
    """The text Ctrl+C copies: the current column's value for every selected row (just the current cell when
    nothing is selected), one per line; a cell's COPY_TEXT_ROLE wins over its displayed text."""
    column = table.currentColumn()
    if column < 0:
        return ""
    rows = sorted({index.row() for index in table.selectedIndexes()})
    if not rows and table.currentRow() >= 0:
        rows = [table.currentRow()]
    lines = []
    for row in rows:
        item = table.item(row, column)
        if item is None:
            lines.append("")
            continue
        full = item.data(COPY_TEXT_ROLE)
        lines.append(full if full else item.text())
    return LINE_BREAK.join(lines)


class _CopyFilter(QObject):
    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.KeyPress and event.matches(QKeySequence.StandardKey.Copy):
            if obj.state() != QAbstractItemView.State.EditingState:
                text = selected_column_text(obj)
                if text:
                    QGuiApplication.clipboard().setText(text)
                return True
        return False


def install_cell_copy(table) -> QObject:
    """Make Ctrl+C on `table` copy the current column of the selected rows (full path for the Path column)."""
    flt = _CopyFilter(table)
    table.installEventFilter(flt)
    table._cell_copy_filter = flt
    return flt

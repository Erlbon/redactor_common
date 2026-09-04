"""
redactor_common/gui/preview_table.py

The "before/after preview with a per-row Apply checkbox" table used by
both Search/Replace and Case Conversion in the epub project -- two
independent, near-identical implementations there (item name / old
value / new value / checkbox columns, resizeColumnsToContents, an
accepted_changes() accessor). Consolidated into one controller both
dialogs configure rather than each reimplementing.

Only rows where the value would actually change are shown -- there's
nothing useful to review or apply for a row that wouldn't change.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QAbstractItemView, QCheckBox, QTableWidget, QTableWidgetItem

ITEM_COL, OLD_COL, NEW_COL, APPLY_COL = range(4)


class PreviewTableController:
    def __init__(self, table: QTableWidget, item_column_label: str = "Item"):
        self.table = table
        table.setColumnCount(4)
        table.setHorizontalHeaderLabels([item_column_label, "Current value", "New value", "Apply"])
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._checkboxes: dict[int, QCheckBox] = {}
        self._new_values: dict[int, str] = {}

    def set_rows(self, rows: list[tuple[int, str, str, str]]) -> None:
        """`rows` is a list of (item_index, display_name, old_value, new_value)
        -- item_index is whatever the caller uses to identify the row
        later (e.g. an index into their own items list), and is what's
        returned by accepted_changes()."""
        self._checkboxes = {}
        self._new_values = {}
        self.table.setRowCount(len(rows))
        for row, (item_index, display_name, old_value, new_value) in enumerate(rows):
            self.table.setItem(row, ITEM_COL, self._readonly_item(display_name))
            self.table.setItem(row, OLD_COL, self._readonly_item(old_value))
            self.table.setItem(row, NEW_COL, self._readonly_item(new_value))

            cb = QCheckBox()
            cb.setChecked(True)
            self._checkboxes[item_index] = cb
            self.table.setCellWidget(row, APPLY_COL, cb)
            self._new_values[item_index] = new_value

        self.table.resizeColumnsToContents()

    def accepted_changes(self) -> dict[int, str]:
        """item_index -> new value, for every row whose checkbox is still
        ticked."""
        return {
            item_index: self._new_values[item_index]
            for item_index, cb in self._checkboxes.items()
            if cb.isChecked()
        }

    @staticmethod
    def _readonly_item(text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item

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

2026-09-10: gained an optional grouping column and a per-row default-
checked state, promoted out of cbzredactor's per-field overwrite-
review dialog (see gui/overwrite_review_dialog.py there) -- that
dialog reviews several FIELDS per FILE at once (a lookup or Parse
Filename touches many fields in one go), unlike Search/Replace and
Case Conversion, which only ever touch one field across many items.
The grouping column (e.g. "File") is what tells those apart visually
when several rows share the same group; `default_checked=False` is
how a field that would overwrite an existing non-blank value starts
unticked (an explicit opt-in per field), while a safe fill starts
ticked, without needing a second call site to hand-roll that logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QAbstractItemView, QCheckBox, QTableWidget, QTableWidgetItem


@dataclass
class PreviewRow:
    """One row for PreviewTableController.set_rows().

    `item_index`: whatever the caller uses to identify this row later
        (an index into their own items list, or any other hashable key)
        -- returned by accepted_changes(), not shown.
    `group`: optional -- shown in a leading column when the controller
        was built with `group_column_label`; ignored (and fine left as
        None) otherwise. Lets several rows sharing one group (e.g. all
        the fields belonging to one file) read as belonging together.
    `default_checked`: whether this row's Apply checkbox starts ticked.
        Defaults to True (the original, and still correct, behavior for
        Search/Replace and Case Conversion, where every previewed
        change is something the user just explicitly asked for) --
        overwrite-review style callers set this False for a row that
        would clobber an existing non-blank value, so accepting it
        requires a deliberate per-field opt-in rather than an
        unnoticed default.
    """

    item_index: object
    display_name: str
    old_value: str
    new_value: str
    group: Optional[str] = None
    default_checked: bool = True


ITEM_COL, OLD_COL, NEW_COL, APPLY_COL = range(4)


class PreviewTableController:
    def __init__(
        self,
        table: QTableWidget,
        item_column_label: str = "Item",
        group_column_label: Optional[str] = None,
    ):
        self.table = table
        self._group_col = 0 if group_column_label is not None else None
        offset = 1 if self._group_col is not None else 0
        self._item_col, self._old_col, self._new_col, self._apply_col = (
            ITEM_COL + offset, OLD_COL + offset, NEW_COL + offset, APPLY_COL + offset,
        )

        headers = ([group_column_label] if group_column_label is not None else [])
        headers += [item_column_label, "Current value", "New value", "Apply"]
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._checkboxes: dict[object, QCheckBox] = {}
        self._new_values: dict[object, str] = {}

    def set_rows(self, rows: list[PreviewRow]) -> None:
        self._checkboxes = {}
        self._new_values = {}
        self.table.setRowCount(len(rows))
        for row, entry in enumerate(rows):
            if self._group_col is not None:
                self.table.setItem(row, self._group_col, self._readonly_item(entry.group or ""))
            self.table.setItem(row, self._item_col, self._readonly_item(entry.display_name))
            self.table.setItem(row, self._old_col, self._readonly_item(entry.old_value))
            self.table.setItem(row, self._new_col, self._readonly_item(entry.new_value))

            cb = QCheckBox()
            cb.setChecked(entry.default_checked)
            self._checkboxes[entry.item_index] = cb
            self.table.setCellWidget(row, self._apply_col, cb)
            self._new_values[entry.item_index] = entry.new_value

        self.table.resizeColumnsToContents()

    def accepted_changes(self) -> dict:
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

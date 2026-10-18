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
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem

from redactor_common.gui.sortable_table import NaturalTableWidgetItem, enable_sorting, suspend_sorting


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


APPLY_COL = 0

# (background, text) for the value you would lose / get / leave alone, light and dark.
_LIGHT = {"old": ("#FCEBEB", "#791F1F"), "new": ("#EAF3DE", "#27500A")}
_DARK = {"old": ("#791F1F", "#F7C1C1"), "new": ("#27500A", "#C0DD97")}


def _tint(table: QTableWidget, kind: str) -> tuple[QBrush, QBrush]:
    dark = table.palette().window().color().lightness() < 128
    bg, fg = (_DARK if dark else _LIGHT)[kind]
    return QBrush(QColor(bg)), QBrush(QColor(fg))


class PreviewTableController:
    """Apply tickbox first (far left, always visible), then the optional group column, the item, the current value and
    the new value. Long values wrap; the current value is tinted red when a real value would be lost and the new value
    green; a blank current value and an unchanged row stay uncoloured."""

    def __init__(
        self,
        table: QTableWidget,
        item_column_label: str = "Item",
        group_column_label: Optional[str] = None,
    ):
        self.table = table
        self._group_col = 1 if group_column_label is not None else None
        offset = 1 if self._group_col is not None else 0
        self._item_col, self._old_col, self._new_col = 1 + offset, 2 + offset, 3 + offset
        self._apply_col = APPLY_COL

        headers = ["Apply"] + ([group_column_label] if group_column_label is not None else [])
        headers += [item_column_label, "Current value", "New value"]
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setWordWrap(True)
        table.verticalHeader().hide()
        table.setTextElideMode(Qt.TextElideMode.ElideNone)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.sectionResized.connect(lambda *_: table.resizeRowsToContents())
        self._new_values: dict[object, str] = {}
        enable_sorting(table)

    def set_rows(self, rows: list[PreviewRow]) -> None:
        self._new_values = {}
        table = self.table
        old_brush, old_text = _tint(table, "old")
        new_brush, new_text = _tint(table, "new")
        with suspend_sorting(table):
            table.setRowCount(len(rows))
            for row, entry in enumerate(rows):
                apply_item = NaturalTableWidgetItem()
                apply_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
                apply_item.setCheckState(Qt.CheckState.Checked if entry.default_checked else Qt.CheckState.Unchecked)
                apply_item.setData(Qt.ItemDataRole.UserRole, entry.item_index)
                table.setItem(row, self._apply_col, apply_item)
                if self._group_col is not None:
                    table.setItem(row, self._group_col, self._readonly_item(entry.group or ""))
                table.setItem(row, self._item_col, self._readonly_item(entry.display_name))
                old_item = self._readonly_item(entry.old_value)
                new_item = self._readonly_item(entry.new_value)
                if entry.old_value.strip() and entry.old_value.strip() != entry.new_value.strip():
                    old_item.setBackground(old_brush)
                    old_item.setForeground(old_text)
                    new_item.setBackground(new_brush)
                    new_item.setForeground(new_text)
                elif not entry.old_value.strip() and entry.new_value.strip():
                    new_item.setBackground(new_brush)
                    new_item.setForeground(new_text)
                table.setItem(row, self._old_col, old_item)
                table.setItem(row, self._new_col, new_item)
                self._new_values[entry.item_index] = entry.new_value

        self._fit_columns()

    def _fit_columns(self) -> None:
        """Apply and the short columns shrink to their text; the current value gets a fixed start width, the new value takes the rest, and both wrap."""
        table = self.table
        table.resizeColumnToContents(self._apply_col)
        table.setColumnWidth(self._apply_col, max(table.columnWidth(self._apply_col), 56))
        for col in (self._group_col, self._item_col):
            if col is not None:
                table.resizeColumnToContents(col)
                table.setColumnWidth(col, min(table.columnWidth(col), 260))
        table.setColumnWidth(self._old_col, 240)
        table.resizeRowsToContents()

    def set_all_checked(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self.table.rowCount()):
            item = self.table.item(row, self._apply_col)
            if item is not None:
                item.setCheckState(state)

    def accepted_changes(self) -> dict:
        """item_index -> new value, for every row whose checkbox is still
        ticked."""
        table = self.table
        out = {}
        for row in range(table.rowCount()):
            item = table.item(row, self._apply_col)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                key = item.data(Qt.ItemDataRole.UserRole)
                out[key] = self._new_values[key]
        return out

    @staticmethod
    def _readonly_item(text: str) -> QTableWidgetItem:
        item = NaturalTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item

"""
redactor_common/gui/column_settings_dialog.py

Tools > Columns dialog, backed by redactor_common.core.table_settings
(field-NAME-based, not index-based -- see that module's docstring for why).
One checkable row per column, listed in the order the columns have in the
table (top = leftmost): the checkbox is "visible", the row can be dragged
or moved with the arrow buttons to change the column's position. Also
reachable via a column header's own right-click context menu in each
project's table; both entry points read and write the same persisted state.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QPushButton, QVBoxLayout,
)

from redactor_common.core.table_settings import is_column_visible
from redactor_common.gui.owned_dialog import OwnedDialog

HINT_TEXT = "Top of the list = leftmost column. Drag rows or use the arrows."
EARLIER_TIP = "Move this column earlier (further left in the table)"
LATER_TIP = "Move this column later (further right in the table)"
UP_GLYPH = "▲"
DOWN_GLYPH = "▼"
_KEY_ROLE = Qt.ItemDataRole.UserRole


class _ReorderList(QListWidget):
    """QListWidget whose drop moves the dragged row itself (insert before the
    row under the pointer, after it when the pointer is in its lower half,
    at the end below the last row). Doing it here, not in Qt's model
    machinery, keeps check states and keys on the item, never loses a row,
    and lets the drag/drop event sequence be driven without a native drag
    (the offscreen platform cannot run one)."""

    def dropEvent(self, event) -> None:
        source_row = self.currentRow()
        if source_row < 0 or not event.mimeData().hasFormat("application/x-qabstractitemmodeldatalist"):
            event.ignore()
            return
        pos = event.position().toPoint()
        index = self.indexAt(pos)
        if index.isValid():
            rect = self.visualItemRect(self.item(index.row()))
            target = index.row() + (1 if pos.y() > rect.center().y() else 0)
        else:
            target = self.count()
        if target > source_row:
            target -= 1
        if target != source_row:
            item = self.takeItem(source_row)
            self.insertItem(target, item)
        self.setCurrentRow(target)
        # IgnoreAction: the move is already done; a Move result would make
        # Qt's startDrag() delete the source row a second time.
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()


class ColumnSettingsDialog(OwnedDialog):
    """
    Usage:
        dialog = ColumnSettingsDialog(
            all_columns=[(field_key, display_label), ...],   # in the table's CURRENT order
            hidden_fields=self._load_hidden_fields(),
            protected_columns=frozenset({"filename"}),
            parent=self,
        )
        if dialog.exec():
            new_hidden = dialog.hidden_fields()
            new_order = dialog.column_order()   # keys, left to right
            ...apply + persist (see gui/column_order.apply_column_order)...
    """

    def __init__(
        self,
        all_columns: list[tuple[str, str]],
        hidden_fields: set[str],
        protected_columns: frozenset[str] = frozenset(),
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Columns")
        self.resize(380, 520)
        self._protected_columns = protected_columns

        layout = QVBoxLayout(self)
        hint = QLabel(HINT_TEXT)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        body = QHBoxLayout()
        self.list_widget = _ReorderList()
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list_widget.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list_widget.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list_widget.setDragEnabled(True)
        self.list_widget.setAcceptDrops(True)
        self.list_widget.setDropIndicatorShown(True)
        self.list_widget.setAccessibleName("Columns")
        for field_key, label in all_columns:
            item = QListWidgetItem(label)
            item.setData(_KEY_ROLE, field_key)
            flags = item.flags() | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsUserCheckable
            visible = is_column_visible(field_key, hidden_fields, protected_columns)
            if field_key in protected_columns:
                flags &= ~Qt.ItemFlag.ItemIsUserCheckable
                item.setToolTip("Always visible")
            item.setFlags(flags)
            item.setCheckState(Qt.CheckState.Checked if visible else Qt.CheckState.Unchecked)
            self.list_widget.addItem(item)
        body.addWidget(self.list_widget, 1)

        side = QVBoxLayout()
        self.up_button = QPushButton(UP_GLYPH)
        self.down_button = QPushButton(DOWN_GLYPH)
        for button, tip in ((self.up_button, EARLIER_TIP), (self.down_button, LATER_TIP)):
            button.setToolTip(tip)
            button.setAccessibleName(tip)
            button.setFixedWidth(40)
            button.setAutoDefault(False)
            side.addWidget(button)
        side.addStretch(1)
        body.addLayout(side)
        layout.addLayout(body, 1)

        self.up_button.clicked.connect(lambda: self.move_selected(-1))
        self.down_button.clicked.connect(lambda: self.move_selected(1))
        self.list_widget.currentRowChanged.connect(lambda _r: self._update_buttons())
        model = self.list_widget.model()
        model.rowsMoved.connect(lambda *_a: self._update_buttons())
        model.rowsInserted.connect(lambda *_a: self._update_buttons())
        model.rowsRemoved.connect(lambda *_a: self._update_buttons())

        if self.list_widget.count():
            self.list_widget.setCurrentRow(0)
        self._update_buttons()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    # -- movement ---------------------------------------------------------

    def move_selected(self, delta: int) -> None:
        """Move the selected row by delta positions (-1 earlier, +1 later)."""
        row = self.list_widget.currentRow()
        target = row + delta
        if row < 0 or not 0 <= target < self.list_widget.count():
            return
        item = self.list_widget.takeItem(row)
        self.list_widget.insertItem(target, item)
        self.list_widget.setCurrentRow(target)

    def _update_buttons(self) -> None:
        row = self.list_widget.currentRow()
        count = self.list_widget.count()
        self.up_button.setEnabled(row > 0)
        self.down_button.setEnabled(0 <= row < count - 1)

    # -- results ----------------------------------------------------------

    def column_order(self) -> list[str]:
        """Field keys in the final (top to bottom = left to right) order."""
        return [
            self.list_widget.item(i).data(_KEY_ROLE) for i in range(self.list_widget.count())
        ]

    def hidden_fields(self) -> set[str]:
        hidden = set()
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            key = item.data(_KEY_ROLE)
            if key in self._protected_columns:
                continue
            if item.checkState() != Qt.CheckState.Checked:
                hidden.add(key)
        return hidden

"""
redactor_common/gui/clear_fields_dialog.py

"Clear fields of N file(s)": the shared dialog behind every app's Clear Fields command. The app passes the ClearPlan
(core/clear_fields.py) and two small callables; the dialog shows

  * one check box per group, with live counts ("Descriptive fields (title, description...): 38 values in 4 files"),
    each group on or off by default as the app says (the riskier ones, like the content type, start unticked);
  * a preview table, File | What will be cleared (the field names and how many), for the groups ticked now, with every
    header draggable (table_columns.make_columns_resizable);
  * a red warning line: "This empties the fields in the file(s) when you Save. You can undo before saving.";
  * Clear / Cancel. Clear is NOT the default button, Cancel is (Enter cancels); Clear is disabled while nothing would
    be cleared.

After exec(): `chosen_groups()` is the list of group keys to clear. Nothing is written here.
"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from redactor_common.core.clear_fields import ClearPlan, describe_fields
from redactor_common.gui.table_columns import make_columns_resizable
from redactor_common.gui.owned_dialog import OwnedDialog

WARNING = "This empties the fields in the file(s) when you Save. You can undo before saving."


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


class ClearFieldsDialog(OwnedDialog):
    def __init__(
        self,
        plan: ClearPlan,
        item_label: Callable[[object], str],
        field_label: Optional[Callable[[str], str]] = None,
        item_noun: str = "file",
        parent=None,
    ):
        super().__init__(parent)
        self.plan = plan
        self.item_label = item_label
        self.field_label = field_label
        self.item_noun = item_noun
        count = len(plan.items)
        self.setWindowTitle(f"Clear fields of {count} {item_noun}(s)")
        self.resize(620, 480)

        layout = QVBoxLayout(self)
        self.heading = QLabel(f"Clear fields of {_plural(count, item_noun)}")
        self.heading.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.heading)

        self.boxes: dict[str, QCheckBox] = {}
        totals = plan.totals()
        for group in plan.groups:
            box = QCheckBox(self._box_text(group.label, totals[group.key].values, totals[group.key].items))
            box.setChecked(group.default_checked)
            box.setToolTip(group.description)
            box.toggled.connect(self._refresh)
            self.boxes[group.key] = box
            layout.addWidget(box)
            if group.description:
                note = QLabel(group.description)
                note.setWordWrap(True)
                note.setStyleSheet("color: gray; margin-left: 22px;")
                layout.addWidget(note)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels([item_noun.capitalize(), "What will be cleared"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)

        self.warning_label = QLabel(WARNING)
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color: #b00020; font-weight: bold;")
        layout.addWidget(self.warning_label)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.clear_button = QPushButton("Clear")
        self.clear_button.setAutoDefault(False)
        self.clear_button.setDefault(False)
        self.clear_button.clicked.connect(self.accept)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setAutoDefault(True)
        self.cancel_button.setDefault(True)
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.clear_button)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)

        self._refresh()

    @staticmethod
    def _box_text(label: str, values: int, items: int) -> str:
        return f"{label}: {_plural(values, 'value')} in {_plural(items, 'file')}"

    def chosen_groups(self) -> list[str]:
        return [key for key, box in self.boxes.items() if box.isChecked()]

    def _refresh(self, *_args) -> None:
        rows = self.plan.selected(self.chosen_groups())
        self.table.setRowCount(len(rows))
        for row, item_plan in enumerate(rows):
            name = QTableWidgetItem(self.item_label(item_plan.item))
            fields = item_plan.fields
            what = QTableWidgetItem(
                f"{describe_fields(fields, self.field_label)}  ({_plural(len(fields), 'value')})")
            what.setToolTip("\n".join(
                f"{self.field_label(f) if self.field_label else f}: {v[:80]}" for f, v in fields.items()))
            self.table.setItem(row, 0, name)
            self.table.setItem(row, 1, what)
        make_columns_resizable(self.table, wide_column=1)
        self.clear_button.setEnabled(bool(rows))

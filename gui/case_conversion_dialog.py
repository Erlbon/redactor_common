"""
redactor_common/gui/case_conversion_dialog.py

Case Conversion (mp3tag's feature of the same name), generalized from
the epub project's version to work on any item type via accessor
callables.
"""

from __future__ import annotations

from typing import Callable, TypeVar

from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QTableWidget, QVBoxLayout,
)

from redactor_common.core.case_conversion import CASE_CONVERSIONS, apply_case_conversion
from redactor_common.gui.preview_table import PreviewTableController

T = TypeVar("T")


class CaseConversionDialog(QDialog):
    def __init__(
        self,
        items: list[T],
        fields: list[tuple[str, str]],  # (field_key, display_label)
        get_value: Callable[[T, str], str],
        get_display_name: Callable[[T], str],
        is_excluded: Callable[[T], bool] | None = None,
        item_noun: str = "item",
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Case Conversion")
        self.resize(760, 480)
        self.items = items
        self._get_value = get_value
        self._get_display_name = get_display_name
        self._is_excluded = is_excluded or (lambda _item: False)

        self._build_ui(fields, item_noun)
        self._refresh_preview()

    def _build_ui(self, fields: list[tuple[str, str]], item_noun: str) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Applies to {len(self.items)} {item_noun}(s)."))

        row = QHBoxLayout()
        row.addWidget(QLabel("Column:"))
        self.field_combo = QComboBox()
        for key, label in fields:
            self.field_combo.addItem(label, key)
        self.field_combo.currentIndexChanged.connect(self._refresh_preview)
        row.addWidget(self.field_combo, 1)

        row.addWidget(QLabel("Convert to:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(list(CASE_CONVERSIONS.keys()))
        self.mode_combo.currentIndexChanged.connect(self._refresh_preview)
        row.addWidget(self.mode_combo)
        layout.addLayout(row)

        self.table = QTableWidget()
        self._preview = PreviewTableController(self.table, item_column_label=item_noun.capitalize())
        layout.addWidget(self.table, 1)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Apply")
        self._ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def result_field_key(self) -> str:
        return self.field_combo.currentData()

    def _refresh_preview(self) -> None:
        field_key = self.result_field_key()
        mode = self.mode_combo.currentText()

        rows: list[tuple[int, str, str, str]] = []
        for i, item in enumerate(self.items):
            if self._is_excluded(item):
                continue
            old_value = self._get_value(item, field_key) or ""
            new_value = apply_case_conversion(old_value, mode)
            if new_value != old_value:
                rows.append((i, self._get_display_name(item), old_value, new_value))

        self._preview.set_rows(rows)

        if not rows:
            self.status_label.setText("No items would be changed.")
            self._ok_button.setEnabled(False)
        else:
            self.status_label.setText(f"{len(rows)} item(s) would be changed.")
            self._ok_button.setEnabled(True)

    def accepted_changes(self) -> dict[int, str]:
        return self._preview.accepted_changes()

"""
redactor_common/gui/auto_numbering_dialog.py

"Assign a sequential number to a chosen field across the selected
items" (Operations > Auto-Numbering), generalized from video's version
to work on any item type via accessor callables -- same pattern as
CaseConversionDialog/SearchReplaceDialog. video was the only project
that had this generically; epub's "Number Series" is a narrower,
single-field (series index only) version of the same idea and is left
as-is, not replaced by this.

Two modes per field (mirrors video's original): a numeric field
(`is_numeric=True` in `fields`) gets the generated number written
directly; a text field gets the number prefixed onto its existing
value with a configurable separator ("Pilot" -> "01 - Pilot"), not a
full overwrite, since a text field usually already has meaningful
content worth keeping.
"""

from __future__ import annotations

from typing import Callable, TypeVar

from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QLineEdit, QSpinBox, QTableWidget, QVBoxLayout,
)

from redactor_common.core.auto_number import apply_auto_number_to_text_field, generate_auto_number
from redactor_common.gui.preview_table import PreviewRow, PreviewTableController

T = TypeVar("T")


class AutoNumberingDialog(QDialog):
    def __init__(
        self,
        items: list[T],
        fields: list[tuple[str, str, bool]],  # (field_key, display_label, is_numeric)
        get_value: Callable[[T, str], str],
        get_display_name: Callable[[T], str],
        item_noun: str = "item",
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Auto-Numbering")
        self.resize(760, 520)
        self.items = items
        self._fields = fields
        self._get_value = get_value
        self._get_display_name = get_display_name

        self._build_ui(item_noun)
        self._refresh_preview()

    def _build_ui(self, item_noun: str) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"Assigns a sequential number to {len(self.items)} {item_noun}(s), "
            f"in their current order."
        ))

        row = QHBoxLayout()
        row.addWidget(QLabel("Field:"))
        self.field_combo = QComboBox()
        for key, label, _is_numeric in self._fields:
            self.field_combo.addItem(label, key)
        self.field_combo.currentIndexChanged.connect(self._on_field_changed)
        row.addWidget(self.field_combo, 1)

        row.addWidget(QLabel("Start at:"))
        self.start_spin = QSpinBox()
        self.start_spin.setRange(-999999, 999999)
        self.start_spin.setValue(1)
        self.start_spin.valueChanged.connect(self._refresh_preview)
        row.addWidget(self.start_spin)

        row.addWidget(QLabel("Increment:"))
        self.increment_spin = QSpinBox()
        self.increment_spin.setRange(-999999, 999999)
        self.increment_spin.setValue(1)
        self.increment_spin.valueChanged.connect(self._refresh_preview)
        row.addWidget(self.increment_spin)

        row.addWidget(QLabel("Zero-pad to:"))
        self.padding_spin = QSpinBox()
        self.padding_spin.setRange(0, 10)
        self.padding_spin.setValue(2)
        self.padding_spin.setToolTip("0 = no padding")
        self.padding_spin.valueChanged.connect(self._refresh_preview)
        row.addWidget(self.padding_spin)
        layout.addLayout(row)

        sep_row = QHBoxLayout()
        self.separator_label = QLabel("Text-field separator:")
        sep_row.addWidget(self.separator_label)
        self.separator_edit = QLineEdit(" - ")
        self.separator_edit.textChanged.connect(self._refresh_preview)
        sep_row.addWidget(self.separator_edit, 1)
        layout.addLayout(sep_row)

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

        self._on_field_changed()  # sets the separator row's initial enabled state

    def result_field_key(self) -> str:
        return self.field_combo.currentData()

    def _current_field_is_numeric(self) -> bool:
        key = self.result_field_key()
        for field_key, _label, is_numeric in self._fields:
            if field_key == key:
                return is_numeric
        return False

    def _on_field_changed(self) -> None:
        # The separator only means anything for a text field (a numeric
        # field's value IS the number, nothing to prefix onto) -- greyed
        # out rather than hidden, so the control layout doesn't jump
        # around as the field selection changes.
        is_numeric = self._current_field_is_numeric()
        self.separator_label.setEnabled(not is_numeric)
        self.separator_edit.setEnabled(not is_numeric)
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        field_key = self.result_field_key()
        is_numeric = self._current_field_is_numeric()
        start = self.start_spin.value()
        increment = self.increment_spin.value()
        padding = self.padding_spin.value()
        separator = self.separator_edit.text()

        rows: list[PreviewRow] = []
        for i, item in enumerate(self.items):
            old_value = self._get_value(item, field_key) or ""
            number_str = generate_auto_number(i, start, increment, padding)
            new_value = number_str if is_numeric else apply_auto_number_to_text_field(
                old_value, number_str, separator
            )
            if new_value != old_value:
                rows.append(PreviewRow(i, self._get_display_name(item), old_value, new_value))

        self._preview.set_rows(rows)

        if not rows:
            self.status_label.setText("No items would be changed.")
            self._ok_button.setEnabled(False)
        else:
            self.status_label.setText(f"{len(rows)} item(s) would be changed.")
            self._ok_button.setEnabled(True)

    def accepted_changes(self) -> dict[int, str]:
        return self._preview.accepted_changes()

"""
redactor_common/gui/column_settings_dialog.py

"Add/Remove Columns..." dialog, backed by
redactor_common.core.table_settings (field-NAME-based, not index-based
-- see that module's docstring for why this matters). Also reachable
via a column header's own right-click context menu in each project's
table; both entry points read and write the exact same persisted
hidden-columns state, so toggling a column here and toggling it via
the header menu do the same thing.
"""

from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QLabel, QVBoxLayout, QWidget

from redactor_common.core.table_settings import is_column_visible


class ColumnSettingsDialog(QDialog):
    """
    Usage:
        dialog = ColumnSettingsDialog(
            all_columns=[(field_key, display_label), ...],
            hidden_fields=self._load_hidden_fields(),
            protected_columns=frozenset({"filename"}),
            parent=self,
        )
        if dialog.exec():
            new_hidden = dialog.hidden_fields()
            ...persist + trigger a table/panel rebuild...
    """

    def __init__(
        self,
        all_columns: list[tuple[str, str]],
        hidden_fields: set[str],
        protected_columns: frozenset[str] = frozenset(),
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Add/Remove Columns")
        self.resize(300, 420)
        self._protected_columns = protected_columns
        self._checkboxes: dict[str, QCheckBox] = {}

        layout = QVBoxLayout(self)
        container = QWidget()
        inner_layout = QVBoxLayout(container)
        for field_key, label in all_columns:
            cb = QCheckBox(label)
            cb.setChecked(is_column_visible(field_key, hidden_fields, protected_columns))
            if field_key in protected_columns:
                cb.setEnabled(False)
                cb.setToolTip("Always visible")
            self._checkboxes[field_key] = cb
            inner_layout.addWidget(cb)
        inner_layout.addStretch(1)
        layout.addWidget(container, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def hidden_fields(self) -> set[str]:
        return {
            field_key for field_key, cb in self._checkboxes.items()
            if not cb.isChecked() and field_key not in self._protected_columns
        }

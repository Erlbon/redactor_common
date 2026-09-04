"""
redactor_common/gui/search_replace_dialog.py

Search & Replace across any field, plus (optionally) an item's own
filename -- generalized from the epub project's version, which was
tied directly to EpubBook/EpubMetadata. This version works on any list
of items via accessor callables the caller supplies, so both the epub
and video (and future) projects can share it as-is.

A filename match means an actual on-disk rename, which callers will
typically want to route differently from an in-memory field edit (e.g.
excluded from an undo stack) -- see FILENAME_FIELD_KEY.
"""

from __future__ import annotations

from typing import Callable, TypeVar

from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout,
    QLabel, QLineEdit, QTableWidget, QVBoxLayout,
)

from redactor_common.core.search_replace import SearchReplaceError, apply_replace
from redactor_common.gui.preview_table import PreviewTableController

FILENAME_FIELD_KEY = "__filename__"

T = TypeVar("T")


class SearchReplaceDialog(QDialog):
    def __init__(
        self,
        items: list[T],
        fields: list[tuple[str, str]],  # (field_key, display_label)
        get_value: Callable[[T, str], str],
        get_display_name: Callable[[T], str],
        include_filename: bool = True,
        is_excluded: Callable[[T], bool] | None = None,
        item_noun: str = "item",
        parent=None,
    ):
        """
        `get_value(item, field_key)` returns the item's current value for
        that field (field_key is FILENAME_FIELD_KEY when the "Filename"
        column is selected, if include_filename is True).
        `get_display_name(item)` is what's shown in the preview table's
        first column (typically the filename).
        `is_excluded(item)` lets the caller skip items that shouldn't be
        offered at all (e.g. one that failed to load) -- defaults to
        including everything.
        """
        super().__init__(parent)
        self.setWindowTitle("Search & Replace")
        self.resize(800, 520)
        self.items = items
        self._get_value = get_value
        self._get_display_name = get_display_name
        self._is_excluded = is_excluded or (lambda _item: False)

        self._build_ui(fields, include_filename, item_noun)
        self._refresh_preview()

    def _build_ui(self, fields: list[tuple[str, str]], include_filename: bool, item_noun: str) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Applies to {len(self.items)} {item_noun}(s)."))

        field_row = QHBoxLayout()
        field_row.addWidget(QLabel("Column:"))
        self.field_combo = QComboBox()
        if include_filename:
            self.field_combo.addItem("Filename", FILENAME_FIELD_KEY)
        for key, label in fields:
            self.field_combo.addItem(label, key)
        self.field_combo.currentIndexChanged.connect(self._refresh_preview)
        field_row.addWidget(self.field_combo, 1)
        layout.addLayout(field_row)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Find:"))
        self.search_edit = QLineEdit()
        self.search_edit.textChanged.connect(self._refresh_preview)
        search_row.addWidget(self.search_edit, 1)
        layout.addLayout(search_row)

        replace_row = QHBoxLayout()
        replace_row.addWidget(QLabel("Replace with:"))
        self.replace_edit = QLineEdit()
        self.replace_edit.textChanged.connect(self._refresh_preview)
        replace_row.addWidget(self.replace_edit, 1)
        layout.addLayout(replace_row)

        options_row = QHBoxLayout()
        self.case_sensitive_cb = QCheckBox("Case sensitive")
        self.case_sensitive_cb.stateChanged.connect(self._refresh_preview)
        options_row.addWidget(self.case_sensitive_cb)

        self.regex_cb = QCheckBox("Use regular expression")
        self.regex_cb.setToolTip("Enables regex patterns and \\1, \\2... backreferences in Replace with")
        self.regex_cb.stateChanged.connect(self._refresh_preview)
        options_row.addWidget(self.regex_cb)
        options_row.addStretch(1)
        layout.addLayout(options_row)

        self.preview_table = QTableWidget()
        self._preview = PreviewTableController(self.preview_table, item_column_label=item_noun.capitalize())
        layout.addWidget(self.preview_table, 1)

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
        search = self.search_edit.text()
        replace = self.replace_edit.text()
        case_sensitive = self.case_sensitive_cb.isChecked()
        use_regex = self.regex_cb.isChecked()

        rows: list[tuple[int, str, str, str]] = []
        error_msg = ""

        if search:
            for i, item in enumerate(self.items):
                if self._is_excluded(item):
                    continue
                old_value = self._get_value(item, field_key) or ""
                try:
                    new_value = apply_replace(old_value, search, replace, use_regex, case_sensitive)
                except SearchReplaceError as exc:
                    error_msg = str(exc)
                    break
                if new_value != old_value:
                    rows.append((i, self._get_display_name(item), old_value, new_value))

        self._preview.set_rows(rows)

        if error_msg:
            self._set_status(error_msg, error=True)
            self._ok_button.setEnabled(False)
        elif not search:
            self._set_status("Enter text to find.")
            self._ok_button.setEnabled(False)
        elif not rows:
            self._set_status(f"No items would be changed.")
            self._ok_button.setEnabled(False)
        else:
            self._set_status(f"{len(rows)} item(s) would be changed.")
            self._ok_button.setEnabled(True)

    def _set_status(self, text: str, error: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            "color: #b91c1c; font-size: 11px;" if error else "color: gray; font-size: 11px;"
        )

    def accepted_changes(self) -> dict[int, str]:
        """item index (into the list passed to the constructor) -> new
        value, for every row whose checkbox is still ticked."""
        return self._preview.accepted_changes()

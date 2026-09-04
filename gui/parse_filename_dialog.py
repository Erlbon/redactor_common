"""
redactor_common/gui/parse_filename_dialog.py

The reverse of Rename/Export by Pattern: extracts metadata FROM a
filename using the same %field% pattern syntax. Shares its pattern
history with Rename/Export -- if you've already described your naming
convention there, it's the natural pattern to parse back with too.
On open, checks every pattern in history against the current batch of
filenames and starts with whichever one actually fits best (epub v46),
rather than just reusing whatever was used last.

Generalized from the epub project's FilenameParseDialog to work on any
item type via accessor callables.
"""

from __future__ import annotations

import os
from typing import Callable, TypeVar

from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout,
    QLabel, QLineEdit, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from redactor_common.core.filename_parser import best_matching_pattern, parse_filename
from redactor_common.gui.pattern_field_panel import PatternFieldPanel

T = TypeVar("T")


class ParseFilenameDialog(QDialog):
    def __init__(
        self,
        items: list[T],
        placeholders: list[tuple[str, str]],  # (field_key, label)
        get_current_path: Callable[[T], str],
        pattern_history: list[str],
        default_pattern: str,
        valid_field_keys: set[str],
        numeric_fields: set[str] = frozenset(),
        isbn_like_fields: set[str] = frozenset(),
        strip_leading_zeros_fields: set[str] = frozenset(),
        title: str = "Parse Filename \u2192 Metadata",
        item_noun: str = "item",
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1060, 560)
        self.items = items
        self._get_current_path = get_current_path
        self._valid_field_keys = valid_field_keys
        self._numeric_fields = numeric_fields
        self._isbn_like_fields = isbn_like_fields
        self._strip_leading_zeros_fields = strip_leading_zeros_fields
        self._stems = [os.path.splitext(os.path.basename(get_current_path(item)))[0] for item in items]
        self._checkboxes: dict[int, QCheckBox] = {}
        self._parsed: dict[int, dict[str, str]] = {}

        self._build_ui(placeholders, pattern_history, default_pattern, item_noun)
        self._refresh_preview()

    def _build_ui(self, placeholders, pattern_history, default_pattern, item_noun) -> None:
        outer = QHBoxLayout(self)

        layout = QVBoxLayout()
        outer.addLayout(layout, 2)
        layout.addWidget(QLabel(
            f"Applies to {len(self.items)} {item_noun}(s). Only fields present in the pattern "
            "are extracted and offered; everything else is left untouched."
        ))

        pattern_row = QHBoxLayout()
        pattern_row.addWidget(QLabel("Pattern:"))
        detected = best_matching_pattern(
            self._stems, pattern_history, self._valid_field_keys,
            self._numeric_fields, self._isbn_like_fields,
        )
        if detected:
            starting_pattern, _count = detected
            self._auto_detected_pattern = starting_pattern
        else:
            starting_pattern = pattern_history[0] if pattern_history else default_pattern
            self._auto_detected_pattern = None
        self.pattern_edit = QLineEdit(starting_pattern)
        self.pattern_edit.textChanged.connect(self._refresh_preview)
        pattern_row.addWidget(self.pattern_edit, 1)

        self._panel = PatternFieldPanel(self.pattern_edit, placeholders, pattern_history, parent=self)
        pattern_row.addWidget(self._panel.recent_button)
        layout.addLayout(pattern_row)
        layout.addWidget(self._panel.recent_list)

        outer.addWidget(self._panel.placeholder_list, 1)

        self.preview_table = QTableWidget()
        self.preview_table.setColumnCount(3)
        self.preview_table.setHorizontalHeaderLabels([item_noun.capitalize(), "Extracted fields", "Apply"])
        self.preview_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
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

    def _refresh_preview(self) -> None:
        pattern = self.pattern_edit.text()
        self._checkboxes = {}
        self._parsed = {}
        rows: list[tuple[int, str, dict[str, str]]] = []

        for i, stem in enumerate(self._stems):
            parsed = parse_filename(
                stem, pattern, self._valid_field_keys, self._numeric_fields,
                self._isbn_like_fields, self._strip_leading_zeros_fields,
            )
            if parsed:
                rows.append((i, stem, parsed))

        self.preview_table.setRowCount(len(rows))
        for row, (item_index, stem, parsed) in enumerate(rows):
            self.preview_table.setItem(row, 0, QTableWidgetItem(stem))
            summary = ", ".join(f"{k}={v}" for k, v in parsed.items() if v)
            self.preview_table.setItem(row, 1, QTableWidgetItem(summary or "(no fields captured)"))
            cb = QCheckBox()
            cb.setChecked(bool(summary))
            cb.setEnabled(bool(summary))
            self._checkboxes[item_index] = cb
            self.preview_table.setCellWidget(row, 2, cb)
            self._parsed[item_index] = parsed

        self.preview_table.resizeColumnsToContents()

        if pattern == getattr(self, "_auto_detected_pattern", None):
            self.status_label.setText(
                f"{len(rows)} of {len(self.items)} filename(s) match. Auto-detected from your pattern history."
            )
        else:
            self.status_label.setText(f"{len(rows)} of {len(self.items)} filename(s) match this pattern.")
        self._ok_button.setEnabled(any(cb.isChecked() for cb in self._checkboxes.values()))

    def accepted_changes(self) -> dict[int, dict[str, str]]:
        """item index -> parsed field dict, for every row whose checkbox
        is still ticked."""
        return {
            item_index: self._parsed[item_index]
            for item_index, cb in self._checkboxes.items()
            if cb.isChecked()
        }

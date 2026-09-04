"""
redactor_common/gui/rename_pattern_dialog.py

The "Tag -> Filename" dialog, mp3tag's Convert feature: build a
filename pattern from metadata placeholders, preview the result for
every item being processed, then either rename the files in place or
export copies with the new names into a chosen folder, leaving the
originals untouched.

Generalized from the epub project's RenameDialog to work on any item
type via accessor callables, so it isn't tied to EpubBook.
"""

from __future__ import annotations

import os
from typing import Callable, TypeVar

from PyQt6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QDialog, QDialogButtonBox,
    QFileDialog, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QRadioButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from redactor_common.core.rename_pattern import render_filename, unique_path
from redactor_common.gui.pattern_field_panel import PatternFieldPanel

T = TypeVar("T")


class RenamePatternDialog(QDialog):
    def __init__(
        self,
        items: list[T],
        placeholders: list[tuple[str, str]],  # (field_key, label)
        get_values: Callable[[T], dict[str, str]],
        get_current_path: Callable[[T], str],
        pattern_history: list[str],
        default_pattern: str,
        title: str = "Rename / Export by Metadata Pattern",
        item_noun: str = "item",
        zero_pad_field: str | None = None,
        zero_pad_label: str = "Zero-pad number to 2 digits (e.g. 02)",
        parent=None,
    ):
        """
        `get_values(item)` returns the item's full placeholder value
        dict (already reflecting any unsaved in-memory edits).
        `get_current_path(item)` returns the item's current file path.
        `zero_pad_field`, if given, is the placeholder key that gets
        zero-padding applied when the checkbox is on (e.g. "series_index"
        or "episode").
        """
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(990, 560)
        self.items = items
        self._get_values = get_values
        self._get_current_path = get_current_path
        self._zero_pad_field = zero_pad_field
        self.output_folder: str | None = None

        self._build_ui(placeholders, pattern_history, default_pattern, item_noun, zero_pad_label)
        self._refresh_preview()

    def _build_ui(
        self, placeholders, pattern_history, default_pattern, item_noun, zero_pad_label
    ) -> None:
        outer = QHBoxLayout(self)

        layout = QVBoxLayout()
        outer.addLayout(layout, 2)
        layout.addWidget(QLabel(f"Applies to {len(self.items)} {item_noun}(s)."))

        pattern_row = QHBoxLayout()
        pattern_row.addWidget(QLabel("Pattern:"))
        starting_pattern = pattern_history[0] if pattern_history else default_pattern
        self.pattern_edit = QLineEdit(starting_pattern)
        self.pattern_edit.textChanged.connect(self._refresh_preview)
        pattern_row.addWidget(self.pattern_edit, 1)

        self._panel = PatternFieldPanel(self.pattern_edit, placeholders, pattern_history, parent=self)
        pattern_row.addWidget(self._panel.recent_button)
        layout.addLayout(pattern_row)
        layout.addWidget(self._panel.recent_list)

        outer.addWidget(self._panel.placeholder_list, 1)

        if self._zero_pad_field:
            self.zero_pad_cb = QCheckBox(zero_pad_label)
            self.zero_pad_cb.stateChanged.connect(self._refresh_preview)
            layout.addWidget(self.zero_pad_cb)
        else:
            self.zero_pad_cb = None

        mode_box = QGroupBox("Action")
        mode_layout = QVBoxLayout(mode_box)
        self.rename_radio = QRadioButton("Rename files in place (in their current folder)")
        self.export_radio = QRadioButton("Export renamed copies to a folder (originals untouched)")
        self.rename_radio.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.rename_radio)
        group.addButton(self.export_radio)
        mode_layout.addWidget(self.rename_radio)

        export_row = QHBoxLayout()
        export_row.addWidget(self.export_radio)
        self.choose_folder_btn = QPushButton("Choose Folder\u2026")
        self.choose_folder_btn.clicked.connect(self._choose_folder)
        self.choose_folder_btn.setEnabled(False)
        export_row.addWidget(self.choose_folder_btn)
        mode_layout.addLayout(export_row)

        self.folder_label = QLabel("(no folder chosen)")
        self.folder_label.setStyleSheet("color: gray; font-size: 11px;")
        mode_layout.addWidget(self.folder_label)

        self.rename_radio.toggled.connect(self._on_mode_toggled)
        self.export_radio.toggled.connect(self._on_mode_toggled)
        layout.addWidget(mode_box)

        self.preview_table = QTableWidget()
        self.preview_table.setColumnCount(2)
        self.preview_table.setHorizontalHeaderLabels(["Current filename", "New filename"])
        self.preview_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.preview_table, 1)

        self.warning_label = QLabel("")
        self.warning_label.setStyleSheet("color: #b45309; font-size: 11px;")
        self.warning_label.setWordWrap(True)
        layout.addWidget(self.warning_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Apply")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)

    def _on_mode_toggled(self) -> None:
        self.choose_folder_btn.setEnabled(self.export_radio.isChecked())
        self._refresh_preview()

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose Export Folder")
        if folder:
            self.output_folder = folder
            self.folder_label.setText(folder)
            self._refresh_preview()

    def _refresh_preview(self) -> None:
        pattern = self.pattern_edit.text()
        zero_pad = bool(self.zero_pad_cb and self.zero_pad_cb.isChecked())

        self._planned: list[tuple[T, str, str]] = []  # (item, old_path, new_stem)
        self.preview_table.setRowCount(len(self.items))
        taken: set[str] = set()

        for row, item in enumerate(self.items):
            values = dict(self._get_values(item))
            if zero_pad and self._zero_pad_field and self._zero_pad_field in values:
                from redactor_common.core.rename_pattern import zero_pad_numeric_value
                values[self._zero_pad_field] = zero_pad_numeric_value(values[self._zero_pad_field])

            old_path = self._get_current_path(item)
            old_name = os.path.basename(old_path)
            new_stem = render_filename(values, pattern)
            ext = os.path.splitext(old_path)[1]

            directory = self.output_folder if self.export_radio.isChecked() else os.path.dirname(old_path)
            new_path = unique_path(directory, new_stem, ext, taken) if directory else os.path.join("", new_stem + ext)
            taken.add(os.path.normcase(os.path.abspath(new_path)) if directory else new_path)
            new_name = os.path.basename(new_path)

            self.preview_table.setItem(row, 0, QTableWidgetItem(old_name))
            self.preview_table.setItem(row, 1, QTableWidgetItem(new_name))
            self._planned.append((item, old_path, new_path))

        if self.export_radio.isChecked() and not self.output_folder:
            self.warning_label.setText("Choose an export folder before applying.")
            self._ok_button.setEnabled(False)
        else:
            self.warning_label.setText("")
            self._ok_button.setEnabled(bool(self.items))

    def _on_accept(self) -> None:
        self.accept()

    def planned_renames(self) -> list[tuple[T, str, str]]:
        """(item, old_path, new_path) for every item, reflecting the
        current pattern/mode/folder. The caller performs the actual
        rename/copy -- this dialog only plans it, since how a rename
        vs. an export-copy is executed is project-specific (e.g.
        whether it interacts with an undo stack)."""
        return self._planned

    def is_export_mode(self) -> bool:
        return self.export_radio.isChecked()

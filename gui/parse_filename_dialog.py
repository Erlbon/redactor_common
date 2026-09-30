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

PATH mode: a pattern containing "/" (or "\\") parses the folder path too
(core/path_parser.py), the mirror of Rename's "Move into folders". The
dialog then shows a Library Root row (remembered through
`on_library_root_changed`, exactly as RenamePatternDialog does) and adds
Confidence and Matched-segments columns. Patterns without a separator
behave exactly as before. accepted_changes() is unchanged either way.
"""

from __future__ import annotations

import os
from typing import Callable, TypeVar

from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFileDialog,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from redactor_common.core.filename_parser import (
    best_matching_pattern, normalize_field_value, parse_filename,
)
from redactor_common.core.path_parser import (
    HIGH_CONFIDENCE, PathParseResult, folder_value_counts, is_path_pattern,
    parse_path_detailed, relative_segments, split_path_pattern,
)
from redactor_common.gui.pattern_field_panel import PatternFieldPanel

T = TypeVar("T")

NOTE_JOINER = chr(10)  # tooltip line break


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
        field_patterns: dict[str, str] | None = None,
        normalizers: dict[str, Callable[[str], str]] | None = None,
        parent=None,
        library_root: str = "",
        on_library_root_changed: Callable[[str], None] | None = None,
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
        # Per-field regex shapes and value clean-up (see
        # core/filename_parser.py) -- e.g. epub's series-index ranges
        # and month names.
        self._parse_kwargs = {"field_patterns": field_patterns, "normalizers": normalizers}
        self._stems = [os.path.splitext(os.path.basename(get_current_path(item)))[0] for item in items]
        self._checkboxes: dict[int, QCheckBox] = {}
        self._parsed: dict[int, dict[str, str]] = {}
        # Path mode (pattern contains a separator): see core/path_parser.py.
        self._paths = [get_current_path(item) for item in items]
        self._library_root = library_root or ""
        self._on_library_root_changed = on_library_root_changed
        self._results: dict[int, PathParseResult] = {}
        self._path_mode = False

        self._item_noun = item_noun
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
        detected = self._detect_pattern(pattern_history)
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

        # Library Root row: only shown in path mode.
        self._root_row = QWidget()
        root_layout = QHBoxLayout(self._root_row)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.addWidget(QLabel("Library root:"))
        self.root_label = QLabel(self._library_root or "(no library root chosen)")
        self.root_label.setStyleSheet("color: gray; font-size: 11px;")
        root_layout.addWidget(self.root_label, 1)
        self.choose_root_btn = QPushButton("Library Root…")
        self.choose_root_btn.clicked.connect(self._choose_root)
        root_layout.addWidget(self.choose_root_btn)
        layout.addWidget(self._root_row)
        self._root_row.setVisible(False)

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

    def _detect_pattern(self, pattern_history: list[str]):
        """(pattern, match_count) of the history pattern that fits this
        batch best, or None. Filename-only histories take the original
        route unchanged; path patterns are counted with parse_path."""
        if not any(is_path_pattern(p) for p in pattern_history):
            return best_matching_pattern(
                self._stems, pattern_history, self._valid_field_keys,
                self._numeric_fields, self._isbn_like_fields,
                field_patterns=self._parse_kwargs["field_patterns"],
            )
        best, best_count = None, 0
        for pattern in pattern_history:
            if is_path_pattern(pattern):
                results = self._parse_paths(pattern, corroborate=False).values()
                count = sum(1 for r in results if r.matched)
            else:
                found = best_matching_pattern(
                    self._stems, [pattern], self._valid_field_keys, self._numeric_fields,
                    self._isbn_like_fields, field_patterns=self._parse_kwargs["field_patterns"],
                )
                count = found[1] if found else 0
            if count > best_count:
                best, best_count = pattern, count
        return (best, best_count) if best is not None else None

    def _parse_paths(self, pattern: str, corroborate: bool = True) -> dict[int, PathParseResult]:
        """Path-mode parse of every item. With `corroborate`, a first pass
        counts how many items share each folder value and a second pass
        lifts the confidence of folder segments that siblings agree on."""
        def run(callback):
            return {
                i: parse_path_detailed(
                    path, pattern, self._library_root, self._valid_field_keys,
                    self._numeric_fields, self._isbn_like_fields,
                    self._strip_leading_zeros_fields, corroborate=callback,
                    **self._parse_kwargs,
                )
                for i, path in enumerate(self._paths)
            }
        first = run(None)
        if not corroborate:
            return first
        counts = folder_value_counts([r for r in first.values() if r.matched])
        return run(lambda f, v: counts.get((f, normalize_field_value(v)), 0))

    def _choose_root(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose Library Root Folder", self._library_root)
        if folder:
            self._library_root = folder
            self.root_label.setText(folder)
            if self._on_library_root_changed is not None:
                self._on_library_root_changed(folder)
            self._refresh_preview()

    def library_root(self) -> str:
        return self._library_root

    def is_path_mode(self) -> bool:
        """True while the pattern contains a folder separator."""
        return self._path_mode

    def parse_results(self) -> dict[int, PathParseResult]:
        """Path mode only: item index -> PathParseResult (confidence,
        matched/missing segments) for every matching row; {} otherwise."""
        return {i: r for i, r in self._results.items() if r.matched}

    def _refresh_preview(self) -> None:
        pattern = self.pattern_edit.text()
        self._path_mode = is_path_pattern(pattern)
        self._root_row.setVisible(self._path_mode)
        if self._path_mode:
            self._refresh_path_preview(pattern)
            return
        self._results = {}
        self.preview_table.setColumnCount(3)
        self.preview_table.setHorizontalHeaderLabels([self._item_noun.capitalize(), "Extracted fields", "Apply"])
        self._checkboxes = {}
        self._parsed = {}
        rows: list[tuple[int, str, dict[str, str]]] = []

        for i, stem in enumerate(self._stems):
            parsed = parse_filename(
                stem, pattern, self._valid_field_keys, self._numeric_fields,
                self._isbn_like_fields, self._strip_leading_zeros_fields,
                **self._parse_kwargs,
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

    def _refresh_path_preview(self, pattern: str) -> None:
        """Path-mode preview: one row per matching item with its relative
        path, parsed fields, confidence and the segments that matched. A
        row starts ticked only at confidence >= 0.5 (the user can tick it)."""
        self._checkboxes = {}
        self._parsed = {}
        self._results = self._parse_paths(pattern)
        count = len(split_path_pattern(pattern))
        rows = [(i, r) for i, r in self._results.items() if r.matched]

        self.preview_table.setColumnCount(5)
        self.preview_table.setHorizontalHeaderLabels(
            [self._item_noun.capitalize(), "Extracted fields", "Confidence", "Matched path segments", "Apply"]
        )
        self.preview_table.setRowCount(len(rows))
        for row, (item_index, result) in enumerate(rows):
            shown = "/".join(relative_segments(self._paths[item_index], self._library_root, count))
            self.preview_table.setItem(row, 0, QTableWidgetItem(shown))
            summary = ", ".join(f"{k}={v}" for k, v in result.values.items() if v)
            self.preview_table.setItem(row, 1, QTableWidgetItem(summary or "(no fields captured)"))
            conf_item = QTableWidgetItem(f"{round(result.confidence * 100)}%")
            if result.notes:
                conf_item.setToolTip(NOTE_JOINER.join(result.notes))
            self.preview_table.setItem(row, 2, conf_item)
            matched = " | ".join(text for _seg, text in result.matched_segments)
            if result.missing_segments:
                matched += f"  (missing: {', '.join(result.missing_segments)})"
            self.preview_table.setItem(row, 3, QTableWidgetItem(matched))
            cb = QCheckBox()
            cb.setChecked(bool(summary) and result.confidence >= 0.5)
            cb.setEnabled(bool(summary))
            self._checkboxes[item_index] = cb
            self.preview_table.setCellWidget(row, 4, cb)
            self._parsed[item_index] = dict(result.values)
        self.preview_table.resizeColumnsToContents()

        high = sum(1 for _i, r in rows if r.confidence >= HIGH_CONFIDENCE)
        text = (
            f"{len(rows)} of {len(self.items)} path(s) match this pattern; "
            f"{high} with high confidence (>= {round(HIGH_CONFIDENCE * 100)}%)."
        )
        if not self._library_root:
            text += " No library root chosen: the last folders of each path are used."
        elif pattern == getattr(self, "_auto_detected_pattern", None):
            text += " Auto-detected from your pattern history."
        self.status_label.setText(text)
        self._ok_button.setEnabled(any(cb.isChecked() for cb in self._checkboxes.values()))

    def accepted_changes(self) -> dict[int, dict[str, str]]:
        """item index -> parsed field dict, for every row whose checkbox
        is still ticked."""
        return {
            item_index: self._parsed[item_index]
            for item_index, cb in self._checkboxes.items()
            if cb.isChecked()
        }

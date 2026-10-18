"""
redactor_common/gui/overwrite_review_dialog.py

Per-file, per-field review of an incoming batch of metadata changes
before they're applied -- the standard confirmation step for any
metadata-writing path that could overwrite existing data (a lookup
apply, Parse Filename, or a local-database import). Replaces the
cruder all-or-nothing "Overwrite All / Keep Existing / Cancel" choice
(or, worse, one checkbox per FILE that silently accepts every field a
lookup found for it, good and bad alike) with a real side-by-side
comparison and a checkbox per (file, field) pair: "there could be
instances where we want some fields, but not all."

Promoted from cbzredactor's `gui/overwrite_review_dialog.py` +
`MainWindow._resolve_overwrite_conflicts()` (2026-09-10, tag
`2026-09-10-02`) -- that project's own code had zero project-specific
dependencies to begin with (duck-types on `.path` and `.metadata`),
so this is that same code, not a reimplementation. `resolve_overwrite_conflicts()`
below is the one entry point a consuming project's MainWindow should
call from every metadata-writing path; it decides on its own whether
there's anything to review at all.

Built on `redactor_common.gui.preview_table.PreviewTableController`,
grouped by File -- the exact same "before/after + per-row Apply
checkbox" shape already used for Search/Replace and Case Conversion,
just grouped since this reviews several fields per file at once
instead of one field across many files.

A field whose existing value is blank (nothing to lose) starts ticked;
a field that would actually overwrite a different, non-blank existing
value starts UNTICKED, requiring a deliberate opt-in. Every field the
incoming change touches is listed, not just the ones that conflict,
once anything in the batch conflicts at all -- full visibility into
what's about to happen to a file, not a partial view.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QSplitter, QStackedWidget,
    QTableWidget, QVBoxLayout, QWidget,
)

from redactor_common.gui.preview_table import PreviewRow, PreviewTableController
from redactor_common.gui.sortable_table import NaturalTableWidgetItem, enable_sorting, suspend_sorting


def _is_overwrite(row: PreviewRow) -> bool:
    old, new = row.old_value.strip(), row.new_value.strip()
    return bool(old) and old != new


def _summary(rows: list[PreviewRow]) -> str:
    overwrites = sum(1 for r in rows if _is_overwrite(r))
    fills = len(rows) - overwrites
    parts = []
    if overwrites:
        parts.append(f"{overwrites} overwrite{'s' if overwrites != 1 else ''}")
    if fills:
        parts.append(f"{fills} fill{'s' if fills != 1 else ''}")
    return ", ".join(parts)


class OverwriteReviewDialog(QDialog):
    """One file: just its fields. Several files: a file list on the left (how many overwrites and fills each has,
    files with overwrites tinted amber) and the fields of the selected file on the right, so the dialog never turns
    into one enormous list. Ticks are kept per file while you move between files."""

    def __init__(self, rows: list[PreviewRow], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Review Changes")
        self.resize(1000, 560)

        layout = QVBoxLayout(self)
        info = QLabel(
            "Review every field this would change before applying. A field "
            "that would overwrite an existing value (red, current value) starts unticked -- tick "
            "it to accept the new value (green), or leave it to keep what's already "
            "there. A field that's currently blank starts ticked, since "
            "there's nothing to lose."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        groups: dict[str, list[PreviewRow]] = {}
        for row in rows:
            groups.setdefault(row.group or "", []).append(row)

        self._stack = QStackedWidget()
        self._controllers: list[PreviewTableController] = []
        self._group_rows = list(groups.values())
        for group_rows in self._group_rows:
            table = QTableWidget()
            controller = PreviewTableController(table, item_column_label="Field")
            controller.set_rows(group_rows)
            self._controllers.append(controller)
            self._stack.addWidget(table)

        self._file_list: Optional[QTableWidget] = None
        if len(groups) > 1:
            self._build_file_list(groups)
            split = QSplitter(Qt.Orientation.Horizontal)
            split.addWidget(self._file_list)
            split.addWidget(self._stack)
            split.setStretchFactor(1, 1)
            split.setSizes([330, 670])
            layout.addWidget(split, 1)

            bulk = QHBoxLayout()
            for text, handler in (
                ("Tick All in File", lambda: self._current().set_all_checked(True)),
                ("Untick All in File", lambda: self._current().set_all_checked(False)),
                ("Tick All Files", lambda: self._all_files(True)),
                ("Untick All Files", lambda: self._all_files(False)),
                ("Next File with Overwrite", self._next_overwrite),
            ):
                button = QPushButton(text)
                button.clicked.connect(lambda _checked=False, h=handler: h())
                bulk.addWidget(button)
            bulk.addStretch(1)
            layout.addLayout(bulk)
            self._file_list.selectRow(0)
        else:
            layout.addWidget(self._stack, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Apply")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _build_file_list(self, groups: dict[str, list[PreviewRow]]) -> None:
        table = QTableWidget(len(groups), 2)
        self._file_list = table
        table.setHorizontalHeaderLabels(["File", "Changes"])
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setWordWrap(True)
        table.verticalHeader().hide()
        table.horizontalHeader().setStretchLastSection(True)
        dark = table.palette().window().color().lightness() < 128
        amber = QBrush(QColor("#633806" if dark else "#FAEEDA"))
        amber_text = QBrush(QColor("#FAC775" if dark else "#633806"))
        enable_sorting(table)
        with suspend_sorting(table):
            for index, (name, group_rows) in enumerate(groups.items()):
                name_item = NaturalTableWidgetItem(name)
                name_item.setData(Qt.ItemDataRole.UserRole, index)
                summary_item = NaturalTableWidgetItem(_summary(group_rows))
                if any(_is_overwrite(r) for r in group_rows):
                    for item in (name_item, summary_item):
                        item.setBackground(amber)
                        item.setForeground(amber_text)
                table.setItem(index, 0, name_item)
                table.setItem(index, 1, summary_item)
        table.setColumnWidth(0, 200)
        table.resizeRowsToContents()
        table.currentCellChanged.connect(lambda row, *_: self._file_selected(row))

    def _stack_index_of_row(self, row: int) -> int:
        item = self._file_list.item(row, 0) if self._file_list is not None else None
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else 0

    def _file_selected(self, row: int) -> None:
        if row >= 0:
            self._stack.setCurrentIndex(self._stack_index_of_row(row))

    def _current(self) -> PreviewTableController:
        return self._controllers[self._stack.currentIndex()]

    def _all_files(self, checked: bool) -> None:
        for controller in self._controllers:
            controller.set_all_checked(checked)

    def _next_overwrite(self) -> None:
        if self._file_list is None:
            return
        count = self._file_list.rowCount()
        start = self._file_list.currentRow()
        for step in range(1, count + 1):
            row = (start + step) % count
            if any(_is_overwrite(r) for r in self._group_rows[self._stack_index_of_row(row)]):
                self._file_list.selectRow(row)
                return

    def tick_fields(self, attrs) -> None:
        """Ticks every row whose field attribute is in `attrs` (for callers and tests that answer the review
        without a user)."""
        for controller in self._controllers:
            controller.set_checked_where(lambda key: key[1] in attrs)

    def field_rows(self) -> list[tuple[str, str, str]]:
        """(field label, current value, new value) for every row of every file, in file order."""
        return [(r.display_name, r.old_value, r.new_value) for rows in self._group_rows for r in rows]

    def accepted_changes(self) -> dict:
        """(file_index, attr) -> new value, for every row whose checkbox
        is still ticked."""
        out: dict = {}
        for controller in self._controllers:
            out.update(controller.accepted_changes())
        return out


def build_overwrite_review_rows(
    target_items: list, metadata_changes: dict[int, dict[str, str]], field_label_fn: Callable[[str], str]
) -> list[PreviewRow]:
    """Turns a plain {item_index: {attr: value}} change set into the
    (file, field) rows this dialog shows -- one row per field, grouped
    by the item's own basename, with the safe-fill-vs-real-overwrite
    default-checked split described in this module's own docstring.
    Pulled out as a standalone function (rather than inlined in
    MainWindow) so it's testable without constructing the dialog/a
    real QApplication.

    `target_items` only needs to duck-type `.path` (str) and `.metadata`
    (an object with one attribute per field, per `attr` in
    `metadata_changes`) -- matches every consuming project's own
    book/file object as-is, no adapter needed."""
    rows: list[PreviewRow] = []
    for index, fields in metadata_changes.items():
        item = target_items[index]
        display_name = os.path.basename(item.path)
        for attr, new_value in fields.items():
            current_value = (getattr(item.metadata, attr, "") or "").strip()
            rows.append(
                PreviewRow(
                    item_index=(index, attr),
                    display_name=field_label_fn(attr),
                    old_value=current_value,
                    new_value=new_value,
                    group=display_name,
                    default_checked=not current_value or current_value == new_value,
                )
            )
    return rows


def resolve_overwrite_conflicts(
    parent: QWidget,
    target_items: list,
    metadata_changes: dict[int, dict[str, str]],
    field_label_fn: Callable[[str], str],
) -> Optional[dict[int, dict[str, str]]]:
    """The one entry point a consuming project's MainWindow calls from
    every metadata-writing path that could overwrite existing data (a
    lookup, bulk edit, or Parse Filename all funnel through this same
    call) -- this is the standard confirmation step, not an opt-in
    extra: "we can be sure what is the real data" means seeing the
    actual old/new comparison, not trusting one batch-wide
    Overwrite-All/Keep-Existing choice.

    Checks whether applying `metadata_changes` would overwrite any
    field that already has a non-blank, different value. A totally
    clean batch (nothing would be overwritten anywhere) skips the
    dialog entirely and returns `metadata_changes` unchanged -- there's
    nothing to review. The moment ANYTHING in the batch conflicts,
    every field the whole batch would touch is shown (not just the
    conflicting ones), via OverwriteReviewDialog -- see its own and
    build_overwrite_review_rows()'s docstrings for the per-field
    ticked/unticked default.

    Returns the changes to actually apply (every field whose checkbox
    is still ticked when Apply is clicked), or None if the user
    cancelled outright."""
    has_conflict = any(
        (getattr(target_items[index].metadata, attr, "") or "").strip() not in ("", new_value)
        for index, fields in metadata_changes.items()
        for attr, new_value in fields.items()
    )
    if not has_conflict:
        return metadata_changes

    rows = build_overwrite_review_rows(target_items, metadata_changes, field_label_fn)
    dialog = OverwriteReviewDialog(rows, parent=parent)
    if dialog.exec() != dialog.DialogCode.Accepted:
        return None

    filtered: dict[int, dict[str, str]] = {}
    for (index, attr), value in dialog.accepted_changes().items():
        filtered.setdefault(index, {})[attr] = value
    return filtered

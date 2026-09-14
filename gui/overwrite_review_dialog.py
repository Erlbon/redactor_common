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

from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout, QTableWidget, QWidget

from redactor_common.gui.preview_table import PreviewRow, PreviewTableController


class OverwriteReviewDialog(QDialog):
    def __init__(self, rows: list[PreviewRow], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Review Changes")
        self.resize(900, 500)

        layout = QVBoxLayout(self)
        info = QLabel(
            "Review every field this would change before applying. A field "
            "that would overwrite an existing value starts unticked -- tick "
            "it to accept the new value, or leave it to keep what's already "
            "there. A field that's currently blank starts ticked, since "
            "there's nothing to lose."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        table = QTableWidget()
        self._controller = PreviewTableController(table, item_column_label="Field", group_column_label="File")
        self._controller.set_rows(rows)
        layout.addWidget(table, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Apply")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accepted_changes(self) -> dict:
        """(file_index, attr) -> new value, for every row whose checkbox
        is still ticked."""
        return self._controller.accepted_changes()


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

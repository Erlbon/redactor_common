"""
redactor_common/gui/history_dialog.py

Tools > History: the operations journal (core/journal.py) as a table, with the old and new value of every
changed field for the selected entry and a "Restore Old Values..." button that writes the old values back to
the file through the app's own writer.

The dialog knows nothing about tag formats. The app hands it:
    apply_restore(path, values) -> str   writes {field: old text} onto the file with the app's own save path
                                          (write, re-read, compare) and returns "" or a plain-language problem.
It runs the journal read and the restore under redactor_common progress dialogs, never on a frozen window.
A restore is itself journaled (kind "restore", so it can be restored in turn) and marks the entry "restored".
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from redactor_common.core.journal import KIND_RESTORE, Entry, Journal
from redactor_common.gui.progress import run_busy
from redactor_common.gui.table_columns import make_columns_resizable

MAX_ROWS = 2000  # the newest entries; the filter reaches the rest
KIND_LABELS = {"save": "Save", "redact": "Redact", "restore": "Restore"}
STATUS_LABELS = {
    "ok": "done", "failed": "FAILED", "pending": "not confirmed (interrupted?)", "restored": "restored",
}
COLUMNS = ["Time", "Change", "File", "Fields", "Status"]


def format_time(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


def describe_changes(entry: Entry) -> str:
    """The detail text: one line per field, old value then new value; a note for covers and cut fields."""
    lines = []
    for name, (old, new) in entry.changes.items():
        cut = "   (too long to restore)" if name in entry.truncated else ""
        lines.append(f"{name}:{cut}\n    was:  {old or '(empty)'}\n    now:  {new or '(empty)'}")
    if entry.cover_changed:
        lines.append("cover: changed (images are not kept in the history)")
    if entry.error:
        lines.append(f"problem: {entry.error}")
    if entry.note:
        lines.append(entry.note)
    return "\n\n".join(lines)


class HistoryDialog(QDialog):
    def __init__(
        self,
        journal: Journal,
        apply_restore: Callable[[str, dict[str, str]], str],
        parent: QWidget | None = None,
        title: str = "History",
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(980, 620)
        self._journal = journal
        self._apply_restore = apply_restore
        self.entries: list[Entry] = []

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Every tag change this program made to a file, newest first (Save, Redact, Restore). Pick one to see "
            "what changed. Covers are not kept here, and replaced or converted files are in the Recycle Bin."
        ))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter by file name or field...")
        self.filter_edit.editingFinished.connect(self.reload)
        layout.addWidget(self.filter_edit)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self._columns_sized = False  # the starting widths are set once, after the first fill; later reloads keep the user's
        self.table.itemSelectionChanged.connect(self._show_detail)
        layout.addWidget(self.table, 3)

        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        layout.addWidget(self.detail, 2)

        buttons = QHBoxLayout()
        self.status_label = QLabel("")
        buttons.addWidget(self.status_label, 1)
        self.restore_button = QPushButton("Restore Old Values...")
        self.restore_button.clicked.connect(self.restore_selected)
        self.restore_button.setEnabled(False)
        buttons.addWidget(self.restore_button)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.reload()

    # -- loading ----------------------------------------------------------------------------------

    def reload(self) -> None:
        text = self.filter_edit.text().strip()
        # Reading a 50,000-line journal takes a moment: off the GUI thread, under a progress dialog.
        self.entries = run_busy(
            self, "Reading the history...", self._journal.entries, limit=MAX_ROWS, text=text, title="History",
        )
        self.table.setRowCount(len(self.entries))
        for row, entry in enumerate(self.entries):
            cells = [
                format_time(entry.ts), KIND_LABELS.get(entry.kind, entry.kind), entry.path,
                ", ".join(entry.changes) + (" + cover" if entry.cover_changed else ""),
                STATUS_LABELS.get(entry.status, entry.status),
            ]
            for column, value in enumerate(cells):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row, column, item)
        if not self._columns_sized and self.entries:
            make_columns_resizable(self.table, wide_column=2)
            self._columns_sized = True
        self.status_label.setText(f"{len(self.entries)} change(s)" + (f" matching '{text}'" if text else ""))
        self.detail.clear()
        self.restore_button.setEnabled(False)

    def selected_entry(self) -> Entry | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows or rows[0].row() >= len(self.entries):
            return None
        return self.entries[rows[0].row()]

    def _show_detail(self) -> None:
        entry = self.selected_entry()
        if entry is None:
            self.detail.clear()
            self.restore_button.setEnabled(False)
            return
        self.detail.setPlainText(f"{entry.path}\n{format_time(entry.ts)}\n\n" + describe_changes(entry))
        self.restore_button.setEnabled(entry.restorable)

    # -- restoring --------------------------------------------------------------------------------

    def restore_selected(self) -> bool:
        """Writes the selected entry's old values back after a confirmation. Returns whether it was restored."""
        entry = self.selected_entry()
        if entry is None or not entry.restorable:
            return False
        values, skipped = Journal.restore_values(entry)
        if not os.path.exists(entry.path):
            QMessageBox.warning(self, "Restore", f"The file is not there any more:\n{entry.path}")
            return False
        fields = ", ".join(values)
        note = f"\n\n(Not restorable, too long to be kept: {', '.join(skipped)}.)" if skipped else ""
        answer = QMessageBox.question(
            self, "Restore Old Values",
            f"Write the old values of {fields} back to\n{entry.path}?\n\nThe fields now have the values this change "
            f"set; a later edit to them is overwritten. The restore is recorded in the history too.{note}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        problem = run_busy(self, "Restoring...", self._apply_restore, entry.path, values, title="Restore")
        if problem:
            QMessageBox.warning(self, "Restore", f"Not restored: {problem}")
            return False
        restore_id = self._journal.record(entry.path, KIND_RESTORE, Journal.reversed_changes(entry, values), note=f"restores {entry.id}")
        self._journal.mark_restored(entry.id, restore_id)
        self.reload()
        self.status_label.setText(f"Restored {fields}.")
        return True

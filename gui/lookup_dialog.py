"""
redactor_common/gui/lookup_dialog.py

Generic "search each item against an online source, review a table of
matches, apply the ones you keep" dialog -- promoted after the same
~200-line shape turned up independently across epubredactor's Google
Books/Calibre/Open Library dialogs and cbzredactor's Comic Vine/GCD
dialogs: a progress-dialog-wrapped search loop, a File/Cover/Found/
Apply table, one checkbox per row with a found match pre-checked, and
an accepted_metadata() accessor read after exec() returns Accepted.

A subclass supplies only what's actually source-specific -- how to
label a row and how to search one item -- via `item_label` and
`search_one` callables passed to __init__. Everything else (the table,
the progress dialog, checkbox wiring, error collection/summarizing,
cover thumbnail rendering) lives here once.

Cover images are shown for confirmation only and are never applied
automatically -- none of the consuming dialogs today write a fetched
cover back into anything (a CBZ's cover is page 1 of the archive
itself; an EPUB's cover has its own separate apply step). A subclass
that DOES want to apply a cover needs its own extra accessor reading
whatever it stashed on the LookupResult, same as no existing dialog
does today.

Usage:
    from redactor_common.gui.lookup_dialog import LookupDialogBase, LookupResult

    class MyLookupDialog(LookupDialogBase):
        def __init__(self, items, parent=None):
            super().__init__(
                items, parent,
                window_title="Look Up via My Source",
                info_text="Searching My Source for ...",
                search_label="Searching My Source…",
                item_label=lambda item: item.display_name,
                search_one=self._search_one,
            )

        def _search_one(self, item) -> LookupResult:
            ...  # call your own core/<source>_lookup.py module
            return LookupResult(fields={...}, cover_bytes=..., error=None)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressDialog,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from redactor_common.core.error_summary import summarize_errors

BOOK_COL, COVER_COL, FOUND_COL, APPLY_COL = range(4)
THUMB_SIZE = QSize(50, 70)


@dataclass
class LookupResult:
    """What `search_one(item)` returns for one row.

    `fields`: applied verbatim (as {attr: value}) if the row's
    checkbox stays checked on Apply -- empty means "nothing found",
    shown as "(no match)" rather than a checkable row.

    `cover_bytes`: shown as a thumbnail for visual confirmation only
    (see module docstring for why nothing here applies it automatically).

    `error`: collected into the dialog's status-line summary instead of
    (not in addition to) a Found-column entry for this row; the row's
    checkbox is disabled either way (nothing to apply).
    """

    fields: dict = field(default_factory=dict)
    cover_bytes: Optional[bytes] = None
    error: Optional[str] = None

    @property
    def found(self) -> bool:
        return bool(self.fields) and not self.error


class LookupDialogBase(QDialog):
    def __init__(
        self,
        items: list,
        parent: Optional[QWidget] = None,
        *,
        window_title: str,
        info_text: str,
        search_label: str,
        item_label: Callable[[object], str],
        search_one: Callable[[object], LookupResult],
        progress_threshold: int = 3,
    ):
        super().__init__(parent)
        self.setWindowTitle(window_title)
        self.resize(900, 520)
        self.items = items
        self._item_label = item_label
        self._search_one = search_one
        self._info_text = info_text
        self._search_label = search_label
        self._progress_threshold = progress_threshold
        self._checkboxes: dict[int, QCheckBox] = {}
        self._results: dict[int, dict] = {}

        self._build_ui()
        self._run_search()

    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        info = QLabel(self._info_text)
        info.setWordWrap(True)
        layout.addWidget(info)

        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["File", "Cover", "Found", "Apply"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(FOUND_COL, QHeaderView.ResizeMode.Stretch)
        self.table.setIconSize(THUMB_SIZE)
        layout.addWidget(self.table, 1)

        self._btn_row = QHBoxLayout()
        self.retry_btn = QPushButton("Search Again")
        self.retry_btn.clicked.connect(self._run_search)
        self._btn_row.addWidget(self.retry_btn)
        self._btn_row.addStretch(1)
        layout.addLayout(self._btn_row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Apply")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def add_toolbar_button(self, button: QPushButton) -> None:
        """Lets a subclass insert its own button (e.g. Comic Vine's
        "Change API Key…") into the same row as "Search Again", before
        the trailing stretch."""
        self._btn_row.insertWidget(self._btn_row.count() - 1, button)

    @staticmethod
    def _readonly_item(text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item

    # ------------------------------------------------------------------
    # Running the lookup

    def _run_search(self) -> None:
        self.table.setRowCount(len(self.items))
        self._checkboxes = {}
        self._results = {}
        progress = QProgressDialog(self._search_label, "Cancel", 0, len(self.items), self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)

        found_count = 0
        errors: list[str] = []

        for row, item in enumerate(self.items):
            if progress.wasCanceled():
                self.table.setRowCount(row)
                break
            progress.setValue(row)
            label = self._item_label(item)
            progress.setLabelText(f"Searching: {label}")
            QApplication.processEvents()

            self.table.setItem(row, BOOK_COL, self._readonly_item(label))
            cover_item = self._readonly_item("")
            self.table.setItem(row, COVER_COL, cover_item)
            self.table.setRowHeight(row, THUMB_SIZE.height() + 6)

            result = self._search_one(item)
            cb = QCheckBox()

            if result.error:
                errors.append(f"{label}: {result.error}")

            if result.found:
                summary = "; ".join(f"{k}: {v}" for k, v in result.fields.items())
                self.table.setItem(row, FOUND_COL, self._readonly_item(summary or "(matched)"))
                if result.cover_bytes:
                    pixmap = QPixmap()
                    if pixmap.loadFromData(result.cover_bytes):
                        scaled = pixmap.scaled(
                            THUMB_SIZE,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                        cover_item.setIcon(QIcon(scaled))
                cb.setChecked(True)
                self._results[row] = result.fields
                found_count += 1
            else:
                self.table.setItem(row, FOUND_COL, self._readonly_item("(error)" if result.error else "(no match)"))
                cb.setEnabled(False)

            self._checkboxes[row] = cb
            self.table.setCellWidget(row, APPLY_COL, cb)

        progress.setValue(len(self.items))
        self.table.resizeColumnsToContents()

        msg = f"Found something for {found_count} of {len(self.items)} file(s)."
        if errors:
            msg += f" {len(errors)} error(s): {summarize_errors(errors)}"
        self.status_label.setText(msg)

    # ------------------------------------------------------------------
    # Result accessor, read by the caller after exec() returns Accepted

    def accepted_metadata(self) -> dict[int, dict]:
        """item index -> {field_key: value}, for every checked row that
        found something."""
        return {
            row: self._results[row]
            for row, cb in self._checkboxes.items()
            if cb.isChecked() and cb.isEnabled() and row in self._results
        }

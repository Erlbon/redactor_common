"""
redactor_common/gui/lookup_dialog.py

Generic "search each item against an online source, review matches,
correct a bad guess, apply the ones you keep" dialog -- promoted after
the same shape turned up independently across epubredactor's Google
Books/Calibre/Open Library dialogs and cbzredactor's Comic Vine/GCD
dialogs.

Layout: a File/Found/Apply table on the left, and a detail panel on
the right showing the selected row's covers -- its existing/current
cover side by side with the source's found cover (large -- not a
cramped in-table icon), so a mismatch (wrong series, wrong issue) is
obvious at a glance instead of only surfacing after Apply -- plus an
editable query-correction form and a readable per-field breakdown of
what was found. A subclass supplies:

  - `item_label(item) -> str`: how to label a row.
  - `get_local_cover(item) -> bytes | None` (optional): the item's own
    already-loaded cover, for the "Current" side of the comparison.
    Omit (or return `None` for a given item) and that side just reads
    "No local cover" -- the "Found" side still works standalone.
  - `search_one(item, query_override) -> LookupResult`: do the actual
    search. `query_override` is a `{field_key: value}` dict -- empty
    the first time (meaning "use your own default query-building
    logic"), populated once the user edits a query field and clicks
    "Search This Item" for that row. Set `LookupResult.used_query` to
    whatever query values were actually used (whether from the
    override or your own default) so the correction form can show
    them.
  - `query_fields` (optional): `[(field_key, label), ...]` describing
    which query fields are correctable at all, e.g.
    `[("series", "Series"), ("number", "Number")]`. Omit entirely (or
    pass an empty list) for a subclass with nothing worth correcting
    per-item -- the correction form is simply not shown then.

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
                query_fields=[("title", "Title"), ("author", "Author")],
                get_local_cover=lambda item: item.existing_cover_bytes,
            )

        def _search_one(self, item, query_override: dict) -> LookupResult:
            title = query_override.get("title") or item.guessed_title
            author = query_override.get("author") or item.guessed_author
            ...  # call your own core/<source>_lookup.py module
            return LookupResult(
                fields={...}, cover_bytes=..., error=None,
                used_query={"title": title, "author": author},
            )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressDialog,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from redactor_common.core.error_summary import summarize_errors
from redactor_common.gui.image_label import AspectRatioImageLabel

BOOK_COL, FOUND_COL, APPLY_COL = range(3)
# Halved from the old single-cover (220, 320) -- two now sit side by
# side in the same detail panel, and this still reads comfortably at
# that width (see _build_cover_slot()).
COVER_PREVIEW_MIN_SIZE = (170, 250)


@dataclass
class LookupResult:
    """What `search_one(item, query_override)` returns for one row.

    `fields`: applied verbatim (as {attr: value}) if the row's
    checkbox stays checked on Apply -- empty means "nothing found",
    shown as "(no match)" rather than a checkable row.

    `cover_bytes`: shown as a large preview for visual confirmation
    only when this row is selected (see module docstring for why
    nothing here applies it automatically).

    `used_query`: the query values actually used for this search
    (whether from `query_override` or the subclass's own default
    guess) -- shown in the correction form so the user can see what
    was searched for and tweak it, not just guess blindly.

    `error`: collected into the dialog's status-line summary instead of
    (not in addition to) a Found-column entry for this row; the row's
    checkbox is disabled either way (nothing to apply).
    """

    fields: dict = field(default_factory=dict)
    cover_bytes: Optional[bytes] = None
    used_query: dict = field(default_factory=dict)
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
        search_one: Callable[[object, dict], LookupResult],
        query_fields: Optional[list[tuple[str, str]]] = None,
        get_local_cover: Optional[Callable[[object], Optional[bytes]]] = None,
        progress_threshold: int = 3,
    ):
        super().__init__(parent)
        self.setWindowTitle(window_title)
        self.resize(1150, 640)
        self.items = items
        self._item_label = item_label
        self._search_one = search_one
        self._info_text = info_text
        self._search_label = search_label
        self._query_fields = query_fields or []
        self._get_local_cover = get_local_cover
        self._progress_threshold = progress_threshold
        self._checkboxes: dict[int, QCheckBox] = {}
        self._row_results: dict[int, LookupResult] = {}
        self._query_overrides: dict[int, dict] = {}
        self._query_edits: dict[str, QLineEdit] = {}
        self._current_detail_row = -1

        self._build_ui()
        self._run_search()

    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        info = QLabel(self._info_text)
        info.setWordWrap(True)
        outer.addWidget(info)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["File", "Found", "Apply"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(FOUND_COL, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._on_row_selected)
        splitter.addWidget(self.table)

        splitter.addWidget(self._build_detail_panel())
        splitter.setSizes([520, 590])
        outer.addWidget(splitter, 1)

        self._btn_row = QHBoxLayout()
        self.retry_btn = QPushButton("Search Again")
        self.retry_btn.clicked.connect(self._run_search)
        self._btn_row.addWidget(self.retry_btn)
        self._btn_row.addStretch(1)
        outer.addLayout(self._btn_row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: gray; font-size: 11px;")
        outer.addWidget(self.status_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Apply")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _build_detail_panel(self) -> QWidget:
        """The selected row's covers -- its existing/current cover next
        to the source's found cover, side by side (see module docstring
        for why: a mismatched match is obvious at a glance this way,
        rather than only surfacing after Apply) -- plus an editable
        query-correction form and a readable per-field breakdown --
        everything the cramped table cells can't show well."""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        covers_row = QHBoxLayout()
        self.detail_cover_current = self._build_cover_slot(covers_row, "Current")
        self.detail_cover_found = self._build_cover_slot(covers_row, "Found")
        layout.addLayout(covers_row, 1)

        if self._query_fields:
            query_box = QGroupBox("Search Query")
            query_form = QFormLayout(query_box)
            for key, label in self._query_fields:
                edit = QLineEdit()
                self._query_edits[key] = edit
                query_form.addRow(label, edit)
            self.search_this_btn = QPushButton("Search This Item")
            self.search_this_btn.clicked.connect(self._search_current_row)
            query_form.addRow(self.search_this_btn)
            layout.addWidget(query_box)

        self.detail_summary = QLabel("")
        self.detail_summary.setWordWrap(True)
        self.detail_summary.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.detail_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.detail_summary, 1)

        self._set_detail_enabled(False)
        return panel

    @staticmethod
    def _build_cover_slot(covers_row: QHBoxLayout, caption: str) -> AspectRatioImageLabel:
        """One labeled cover preview ("Current" or "Found"), added as
        its own column in `covers_row`. Returns just the image label --
        the caption above it is static and never touched again."""
        column = QVBoxLayout()
        caption_label = QLabel(caption)
        caption_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        caption_label.setStyleSheet("font-weight: bold;")
        column.addWidget(caption_label)

        image = AspectRatioImageLabel()
        image.setMinimumSize(*COVER_PREVIEW_MIN_SIZE)
        image.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image.setStyleSheet("background-color: palette(base); border: 1px solid palette(mid);")
        image.setText("Select a row")
        column.addWidget(image, 1)

        covers_row.addLayout(column, 1)
        return image

    @staticmethod
    def _set_cover(label: AspectRatioImageLabel, cover_bytes: Optional[bytes], empty_text: str) -> None:
        pixmap = None
        if cover_bytes:
            pixmap = QPixmap()
            if not pixmap.loadFromData(cover_bytes):
                pixmap = None
        label.set_original_pixmap(pixmap)
        label.setText("" if pixmap else empty_text)

    def _set_detail_enabled(self, enabled: bool) -> None:
        for edit in self._query_edits.values():
            edit.setEnabled(enabled)
        if hasattr(self, "search_this_btn"):
            self.search_this_btn.setEnabled(enabled)

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
        self._row_results = {}
        self._query_overrides = {}
        self._current_detail_row = -1
        self._set_detail_enabled(False)
        for label in (self.detail_cover_current, self.detail_cover_found):
            label.set_original_pixmap(None)
            label.setText("Select a row")
        self.detail_summary.setText("")

        progress = QProgressDialog(self._search_label, "Cancel", 0, len(self.items), self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)

        for row, item in enumerate(self.items):
            if progress.wasCanceled():
                self.table.setRowCount(row)
                break
            progress.setValue(row)
            progress.setLabelText(f"Searching: {self._item_label(item)}")
            QApplication.processEvents()
            self._process_row(row, item, {})

        progress.setValue(len(self.items))
        self.table.resizeColumnsToContents()
        self._refresh_status()
        if self.table.rowCount() > 0:
            self.table.selectRow(0)

    def _process_row(self, row: int, item: object, query_override: dict) -> None:
        """Runs search_one() for one row and updates its table cells --
        used both for the initial batch search and for "Search This
        Item" re-running just the selected row."""
        label = self._item_label(item)
        result = self._search_one(item, query_override)
        self._row_results[row] = result

        self.table.setItem(row, BOOK_COL, self._readonly_item(label))
        cb = self._checkboxes.get(row) or QCheckBox()
        cb.setChecked(result.found)
        cb.setEnabled(result.found)

        if result.found:
            summary = "; ".join(f"{k}: {v}" for k, v in result.fields.items())
            self.table.setItem(row, FOUND_COL, self._readonly_item(summary or "(matched)"))
        else:
            self.table.setItem(row, FOUND_COL, self._readonly_item("(error)" if result.error else "(no match)"))

        self._checkboxes[row] = cb
        self.table.setCellWidget(row, APPLY_COL, cb)

    def _refresh_status(self) -> None:
        found_count = sum(1 for result in self._row_results.values() if result.found)
        errors = [
            f"{self._item_label(self.items[row])}: {result.error}"
            for row, result in self._row_results.items()
            if result.error
        ]
        msg = f"Found something for {found_count} of {len(self.items)} file(s)."
        if errors:
            msg += f" {len(errors)} error(s): {summarize_errors(errors)}"
        self.status_label.setText(msg)

    # ------------------------------------------------------------------
    # Detail panel: viewing and correcting the selected row

    def _on_row_selected(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            self._current_detail_row = -1
            self._set_detail_enabled(False)
            for label in (self.detail_cover_current, self.detail_cover_found):
                label.set_original_pixmap(None)
                label.setText("Select a row")
            self.detail_summary.setText("")
            return

        row = rows[0].row()
        self._current_detail_row = row
        self._set_detail_enabled(True)
        result = self._row_results.get(row)

        local_cover = self._get_local_cover(self.items[row]) if self._get_local_cover else None
        self._set_cover(self.detail_cover_current, local_cover, "No local cover")
        self._set_cover(
            self.detail_cover_found, result.cover_bytes if result else None, "No cover available"
        )

        used_query = (result.used_query if result else None) or self._query_overrides.get(row, {})
        for key, edit in self._query_edits.items():
            edit.setText(used_query.get(key, ""))

        if result and result.found:
            self.detail_summary.setText(
                "\n".join(f"<b>{k}:</b> {v}" for k, v in result.fields.items())
            )
            self.detail_summary.setTextFormat(Qt.TextFormat.RichText)
        elif result and result.error:
            self.detail_summary.setText(f"Error: {result.error}")
            self.detail_summary.setTextFormat(Qt.TextFormat.PlainText)
        else:
            self.detail_summary.setText("(no match)")
            self.detail_summary.setTextFormat(Qt.TextFormat.PlainText)

    def _search_current_row(self) -> None:
        row = self._current_detail_row
        if row < 0:
            return
        override = {key: edit.text().strip() for key, edit in self._query_edits.items() if edit.text().strip()}
        self._query_overrides[row] = override
        self._process_row(row, self.items[row], override)
        self._refresh_status()
        self._on_row_selected()  # re-sync the detail panel to the fresh result

    # ------------------------------------------------------------------
    # Result accessor, read by the caller after exec() returns Accepted

    def accepted_metadata(self) -> dict[int, dict]:
        """item index -> {field_key: value}, for every checked row that
        found something."""
        return {
            row: result.fields
            for row, cb in self._checkboxes.items()
            if cb.isChecked() and cb.isEnabled() and (result := self._row_results.get(row)) and result.found
        }

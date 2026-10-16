"""
redactor_common/gui/notes_review_dialog.py

The REVIEW PAGE for folder notes, shared by the apps that write them (videoredactor today; mp3redactor's own, richer
page predates it). The rules are core/notes_review.py's ReviewSession; the proposals come from the app's engine. Nothing
here touches the network on the GUI thread: a rebuild after a person's choice runs in run_busy().

  NotesReviewDialog(session, view, rebuild=...)   two views of the same ReviewSession, switchable:
                                                    one at a time  (one folder, or one series with its seasons): Skip / Write note / Back
                                                    list           (several folders): ticks, Write N ticked notes
  ProposalPanel                                   what the page shows of one proposal: path, confidence, reasons, candidates
                                                  (pick the right one), the note preview (read-only: Pluteus edits notes)

Nothing is written without an explicit click on Write note / Write N ticked notes / Write all remaining confident. An
uncertain or nothing-found proposal cannot be ticked or written until a candidate was chosen (the app's `rebuild` builds
it again and, if a note could be made, it is confident).
"""

from __future__ import annotations

import os
from html import escape
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QSizePolicy,
    QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser, QTextEdit, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from redactor_common.core.notes import STATE_LEGACY, parse_front_matter
from redactor_common.core.notes_model import Candidate, Confidence, Proposal
from redactor_common.core.notes_review import (
    CONFIDENCE_WORDS, ERROR, EXISTS, PENDING, SKIPPED, TOO_LARGE, VIEW_LIST, VIEW_ONE, WRITTEN, ReviewItem,
    ReviewSession, note_diff,
)
from redactor_common.gui.progress import run_busy

DEFAULT_SIZE = (1100, 700)

BADGE_STYLES = {
    Confidence.CONFIDENT: "background:#c9ecd1; color:#12401f;",
    Confidence.UNCERTAIN: "background:#fbe3a6; color:#4d3600;",
    Confidence.NONE: "background:#dcdcdc; color:#333333;",
}
UPDATE_BADGE_STYLE = "background:#cfe0f7; color:#10325c; border-radius:4px; padding:3px 8px; font-weight:bold;"
RECYCLE_NOTE = "The old note goes to the Recycle Bin. You can restore it from the Recycle Bin."


def diff_html(old_text: str, new_text: str) -> str:
    """The unified diff of the note now against the new draft as HTML: removed lines red, added lines green."""
    diff = note_diff(old_text, new_text)
    if not diff:
        return "<p>The new note is the same as the note now.</p>"
    rows = []
    for line in diff.split("\n"):
        colour = ("#9b1c1c" if line.startswith("-") and not line.startswith("---")
                  else "#17622b" if line.startswith("+") and not line.startswith("+++")
                  else "#555555" if line.startswith(("@@", "---", "+++")) else "")
        style = f' style="color:{colour}"' if colour else ""
        rows.append(f"<span{style}>{escape(line) or '&nbsp;'}</span>")
    return "<pre style='font-family:Consolas,monospace'>" + "<br>".join(rows) + "</pre>"


def open_in_system(path: str) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(path))


def _shrinkable(*labels: QLabel) -> None:
    """Labels whose text (a long path, a long banner) must never decide how wide the dialog is."""
    for label in labels:
        label.setWordWrap(True)
        label.setMinimumWidth(0)
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)


def _button(text: str) -> QPushButton:
    button = QPushButton(text)
    button.setAutoDefault(False)
    return button


def _bullets(lines: list[str]) -> str:
    return "<ul style='margin:0'>" + "".join(f"<li>{escape(line)}</li>" for line in lines) + "</ul>" if lines else ""


class ProposalPanel(QWidget):
    """The detail of one proposal. Read-only: a person's choices go out as signals."""

    candidateChosen = pyqtSignal(object)  # a Candidate
    openNoteRequested = pyqtSignal()
    showFolderRequested = pyqtSignal()

    def __init__(self, can_choose: Callable[[Proposal, Candidate], bool], parent=None,
                 candidate_columns: tuple[str, ...] = ("Name", "Details", "Score", "Source", "Matches")):
        super().__init__(parent)
        self._can_choose = can_choose
        self._item: Optional[ReviewItem] = None
        self._candidates: list[Candidate] = []

        self.path_label = QLabel()
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        font = self.path_label.font()
        font.setBold(True)
        self.path_label.setFont(font)
        self.kind_label = QLabel()
        self.badge = QLabel()
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setMinimumWidth(110)
        self.update_badge = QLabel("Update")
        self.update_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.update_badge.setStyleSheet(UPDATE_BADGE_STYLE)
        self.update_badge.setVisible(False)
        self.state_label = QLabel()
        title_box = QVBoxLayout()
        title_box.addWidget(self.path_label)
        title_box.addWidget(self.kind_label)
        head = QHBoxLayout()
        head.addLayout(title_box, 1)
        head.addWidget(self.state_label)
        head.addWidget(self.update_badge, 0, Qt.AlignmentFlag.AlignTop)
        head.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignTop)

        self.reasons_label = QLabel()
        self.reasons_label.setTextFormat(Qt.TextFormat.RichText)
        self.problems_label = QLabel()
        self.problems_label.setTextFormat(Qt.TextFormat.RichText)

        self.candidate_box = QWidget()
        cand_layout = QVBoxLayout(self.candidate_box)
        cand_layout.setContentsMargins(0, 0, 0, 0)
        self.candidate_caption = QLabel("Candidates found. Pick the right one to build the note from it:")
        cand_layout.addWidget(self.candidate_caption)
        self.candidate_table = QTableWidget(0, len(candidate_columns))
        self.candidate_table.setHorizontalHeaderLabels(list(candidate_columns))
        self.candidate_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.candidate_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.candidate_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.candidate_table.verticalHeader().setVisible(False)
        self.candidate_table.setMaximumHeight(140)
        header = self.candidate_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)  # every column can be dragged
        header.setStretchLastSection(True)
        self.candidate_table.itemSelectionChanged.connect(self._update_buttons)
        cand_layout.addWidget(self.candidate_table)
        cand_row = QHBoxLayout()
        self.candidate_button = _button("Use selected candidate")
        self.candidate_button.clicked.connect(self._candidate_clicked)
        self.candidate_note = QLabel()
        cand_row.addWidget(self.candidate_button)
        cand_row.addWidget(self.candidate_note, 1)
        cand_layout.addLayout(cand_row)

        self.preview = QTextBrowser()
        self.preview.setOpenExternalLinks(True)
        self.raw = QTextEdit()
        self.raw.setReadOnly(True)
        self.raw.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        mono = self.raw.font()
        mono.setFamily("Consolas")
        self.raw.setFont(mono)
        self.diff_view = QTextBrowser()
        self.tabs = QTabWidget()
        self.tabs.addTab(self.preview, "Preview")
        self.tabs.addTab(self.raw, "Raw text")
        self.diff_tab = self.tabs.addTab(self.diff_view, "Before / after")
        self.tabs.setTabVisible(self.diff_tab, False)

        self.sources_label = QLabel()
        self.open_note_button = _button("Open note")
        self.open_note_button.clicked.connect(self.openNoteRequested.emit)
        self.show_folder_button = _button("Show in folder")
        self.show_folder_button.clicked.connect(self.showFolderRequested.emit)
        foot = QHBoxLayout()
        foot.addWidget(self.sources_label, 1)
        foot.addWidget(self.open_note_button)
        foot.addWidget(self.show_folder_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(head)
        layout.addWidget(self.reasons_label)
        layout.addWidget(self.problems_label)
        layout.addWidget(self.candidate_box)
        layout.addWidget(self.tabs, 1)
        layout.addLayout(foot)
        _shrinkable(self.path_label, self.kind_label, self.reasons_label, self.problems_label, self.sources_label,
                    self.candidate_caption, self.candidate_note)
        self.show_item(None)

    @staticmethod
    def render_body(text: str) -> str:
        """The note without its front matter (the raw tab shows everything)."""
        if not text:
            return ""
        _meta, body = parse_front_matter(text)
        return body or text

    def show_item(self, item: Optional[ReviewItem]) -> None:
        self._item = item
        if item is None:
            self.path_label.setText("Nothing selected")
            for w in (self.kind_label, self.state_label, self.badge, self.reasons_label, self.problems_label,
                      self.sources_label):
                w.clear()
            self.badge.setStyleSheet("")
            self.update_badge.setVisible(False)
            self.candidate_box.setVisible(False)
            self.preview.clear()
            self.raw.clear()
            self.tabs.setTabVisible(self.diff_tab, False)
            for button in (self.open_note_button, self.show_folder_button):
                button.setEnabled(False)
            return
        p = item.proposal
        self.path_label.setText(str(p.folder))
        self.kind_label.setText(f"Note: {os.path.basename(p.path)}" + (f" - {p.title}" if p.title else ""))
        self.badge.setText(item.confidence_word)
        self.badge.setStyleSheet(BADGE_STYLES[p.confidence] + "border-radius:4px; padding:3px 8px; font-weight:bold;")
        self.state_label.setText(item.state_word + (f": {item.message}" if item.message and item.state != WRITTEN else ""))
        self.update_badge.setVisible(p.is_update)
        self.reasons_label.setText(_bullets(p.reasons))
        self.problems_label.setText(
            "<span style='color:#b00020'>" + _bullets(p.problems) + "</span>" if p.problems else "")
        self.sources_label.setText("Sources: " + ", ".join(p.sources) if p.sources else "")
        self._fill_candidates(p)
        text = p.note_text
        self.raw.setPlainText(text)
        if text:
            self.preview.setMarkdown(self.render_body(text))
        else:
            self.preview.setPlainText("No note can be made yet: pick a candidate above." if p.candidates
                                      else "Nothing was found for this folder.")
        self.tabs.setTabVisible(self.diff_tab, p.is_update and bool(text))
        if p.is_update and text:
            self.diff_view.setHtml(diff_html(p.old_text, text) + f"<p>{escape(RECYCLE_NOTE)}</p>")
        elif self.tabs.currentIndex() == self.diff_tab:
            self.tabs.setCurrentIndex(0)
        written = item.state == WRITTEN and bool(item.written_path)
        self.open_note_button.setEnabled(written or p.is_update)
        self.show_folder_button.setEnabled(True)

    def _fill_candidates(self, p: Proposal) -> None:
        self._candidates = list(p.candidates)
        show = bool(self._candidates) and not (self._item and self._item.finished)
        self.candidate_box.setVisible(show)
        self.candidate_table.setRowCount(len(self._candidates))
        for row, c in enumerate(self._candidates):
            cells = (c.name, c.detail, f"{c.score:.2f}", c.source + (f" + {c.also}" if c.also else ""),
                     ", ".join(c.hits[:4]) + (f" (+{len(c.hits) - 4})" if len(c.hits) > 4 else ""))
            for col, text in enumerate(cells[: self.candidate_table.columnCount()]):
                self.candidate_table.setItem(row, col, QTableWidgetItem(text))
        if self._candidates:
            self.candidate_table.selectRow(0)
        self._update_buttons()

    def selected_candidate(self) -> Optional[Candidate]:
        rows = self.candidate_table.selectionModel().selectedRows() if self.candidate_table.selectionModel() else []
        return self._candidates[rows[0].row()] if rows and rows[0].row() < len(self._candidates) else None

    def _update_buttons(self) -> None:
        candidate = self.selected_candidate()
        ok = bool(candidate and self._item and self._can_choose(self._item.proposal, candidate))
        self.candidate_button.setEnabled(ok)
        self.candidate_note.setText("" if ok or candidate is None else "This candidate cannot be used for this folder.")

    def _candidate_clicked(self) -> None:
        candidate = self.selected_candidate()
        if candidate is not None:
            self.candidateChosen.emit(candidate)


class NotesReviewDialog(QDialog):
    def __init__(
        self, session: ReviewSession, view: str,
        rebuild: Callable[[Proposal, Optional[Candidate]], Proposal],
        can_choose: Callable[[Proposal, Candidate], bool] = lambda _p, _c: True,
        banner: str = "", parent=None, title: str = "Create Notes", size: Optional[tuple[int, int]] = None,
        on_size: Optional[Callable[[int, int], None]] = None, open_path: Callable[[str], None] = open_in_system,
        candidate_columns: tuple[str, ...] = ("Name", "Details", "Score", "Source", "Matches"),
        lookup_note: str = "", history_note: str = "",
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self._lookup_note = lookup_note  # 'Looked up N folders in H:MM:SS (R requests, C from the cache).'
        self._history_note = history_note  # 'Recorded in Tools > History (it is switched on).'
        self.followup: list[str] = []
        self._busy = False  # a rebuild is running (run_busy): no second one may start
        self.status_text = ""
        self.session = session
        self.view = view
        self.index = 0
        self.last_summary = ""
        self._rebuild = rebuild
        self._can_choose = can_choose
        self._candidate_columns = candidate_columns
        self._on_size = on_size
        self._open_path = open_path
        self.resize(*(size or DEFAULT_SIZE))
        self.setSizeGripEnabled(True)

        self.header_label = QLabel()
        head_font = self.header_label.font()
        head_font.setPointSize(head_font.pointSize() + 2)
        head_font.setBold(True)
        self.header_label.setFont(head_font)
        self.switch_button = _button("")
        self.switch_button.setFlat(True)
        self.switch_button.setStyleSheet("color:#1a5fb4; text-decoration:underline; border:none; padding:2px 6px;")
        self.switch_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.switch_button.clicked.connect(self.toggle_view)
        top = QHBoxLayout()
        top.addWidget(self.header_label, 1)
        top.addWidget(self.switch_button)
        self.banner_label = QLabel(banner)
        self.banner_label.setStyleSheet("background:#eef3fb; color:#1c2b45; border:1px solid #c5d3ea; padding:4px 8px;")
        self.banner_label.setVisible(bool(banner))
        _shrinkable(self.header_label, self.banner_label)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_one_page())
        self.stack.addWidget(self._build_list_page())
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.banner_label)
        layout.addWidget(self.stack, 1)
        self._apply_view()

    # -- building --------------------------------------------------------------------------------------------

    def _make_panel(self) -> ProposalPanel:
        panel = ProposalPanel(self._can_choose, candidate_columns=self._candidate_columns)
        panel.candidateChosen.connect(self._choose_candidate)
        panel.openNoteRequested.connect(self.open_current_note)
        panel.showFolderRequested.connect(self.show_current_folder)
        return panel

    def _build_one_page(self) -> QWidget:
        page = QWidget()
        self.one_panel = self._make_panel()
        self.back_button = _button("Back")
        self.skip_button = _button("Skip")
        self.write_button = QPushButton("Write note")
        self.write_button.setDefault(True)
        self.write_all_button = _button("Write all remaining confident")
        self.close_one_button = _button("Close")
        self.back_button.clicked.connect(self.go_back)
        self.skip_button.clicked.connect(self.skip_current)
        self.write_button.clicked.connect(self.write_current)
        self.write_all_button.clicked.connect(self.write_remaining)
        self.close_one_button.clicked.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(self.back_button)
        row.addWidget(self.write_all_button)
        row.addStretch(1)
        row.addWidget(self.close_one_button)
        row.addWidget(self.skip_button)
        row.addWidget(self.write_button)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.one_panel, 1)
        layout.addLayout(row)
        return page

    def _build_list_page(self) -> QWidget:
        page = QWidget()
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Folder", "Note", "Confidence", "State"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        header = self.tree.header()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)  # every column can be dragged
        header.setStretchLastSection(True)
        self.tree.setColumnWidth(0, 260)
        self.tree.currentItemChanged.connect(self._on_tree_current)
        self.tree.itemChanged.connect(self._on_tree_check)
        self.list_panel = self._make_panel()
        self.filter_check = QCheckBox("Only uncertain or not found")
        self.filter_check.toggled.connect(self._filter_toggled)
        self.tick_all_button = _button("Tick all confident")
        self.untick_all_button = _button("Untick all")
        self.tick_all_button.clicked.connect(self._tick_all)
        self.untick_all_button.clicked.connect(self._untick_all)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.filter_check)
        left_layout.addWidget(self.tree, 1)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(self.list_panel)
        splitter.setSizes([430, 640])
        self.write_ticked_button = QPushButton("Write 0 ticked notes")
        self.write_ticked_button.setAutoDefault(False)
        self.write_ticked_button.clicked.connect(self.write_ticked)
        self.close_list_button = _button("Close")
        self.close_list_button.clicked.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(self.tick_all_button)
        row.addWidget(self.untick_all_button)
        row.addStretch(1)
        row.addWidget(self.write_ticked_button)
        row.addWidget(self.close_list_button)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter, 1)
        layout.addLayout(row)
        return page

    # -- views -----------------------------------------------------------------------------------------------

    def toggle_view(self) -> None:
        if self.view == VIEW_ONE:
            self.view = VIEW_LIST
            self.filter_check.setChecked(False)
        else:
            self.view = VIEW_ONE
            current = self.tree.currentItem()
            if current is not None:
                self.index = current.data(0, Qt.ItemDataRole.UserRole)
        self._apply_view()

    def _apply_view(self) -> None:
        one = self.view == VIEW_ONE
        self.stack.setCurrentIndex(0 if one else 1)
        self.switch_button.setText("Switch to list view" if one else "Switch to one-at-a-time view")
        if one:
            self._show_one()
        else:
            self._fill_tree()
            self._select_tree_index(self.index)
            self._refresh_list_header()

    def _refresh_all(self) -> None:
        if self.view == VIEW_ONE:
            self._show_one()
        else:
            self._fill_tree()
            self._select_tree_index(self.index)

    # -- one at a time ---------------------------------------------------------------------------------------

    def _show_one(self) -> None:
        items = self.session.items
        if not items:
            self.header_label.setText("Nothing to review")
            self.one_panel.show_item(None)
            for b in (self.write_button, self.skip_button, self.back_button, self.write_all_button):
                b.setEnabled(False)
            return
        self.index = max(0, min(self.index, len(items) - 1))
        item = items[self.index]
        self.header_label.setText(f"Create notes  {self.index + 1} of {len(items)}")
        self.one_panel.show_item(item)
        self.back_button.setEnabled(self.index > 0)
        self.skip_button.setEnabled(item.state == PENDING)
        self.write_button.setEnabled(item.tickable)
        self.write_button.setText("Update note" if item.proposal.is_update else "Write note")
        if item.tickable:
            self.write_button.setFocus()
        remaining = sum(1 for i in items if i.tickable)
        self.write_all_button.setText(f"Write all remaining confident ({remaining})")
        self.write_all_button.setEnabled(remaining > 0)

    def go_back(self) -> None:
        if self.index > 0:
            self.index -= 1
            self._show_one()

    def _advance(self) -> None:
        nxt = self.session.next_pending(self.index)
        if nxt is None:
            self._finish()
        else:
            self.index = nxt
            self._show_one()

    def skip_current(self) -> None:
        self.session.skip(self.index)
        self._advance()

    def _confirm_updates(self, indices: list[int]) -> bool:
        """An UPDATE replaces an existing note: ask once, say where the old one goes. True = go on."""
        n = sum(1 for i in indices if self.session.items[i].proposal.is_update)
        if not n:
            return True
        legacy = sum(1 for i in indices if self.session.items[i].proposal.is_update
                     and self.session.items[i].proposal.old_state.kind == STATE_LEGACY)
        text = f"Replace {n} note{'s' if n != 1 else ''} made by an older version?\n\n{RECYCLE_NOTE}"
        if legacy:
            text += (f"\n\n{legacy} of them {'was' if legacy == 1 else 'were'} made by the first versions: "
                     "it cannot be told whether you edited " + ("it." if legacy == 1 else "them."))
        answer = QMessageBox.question(self, self.windowTitle(), text,
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def write_current(self) -> None:
        if not self._confirm_updates([self.index]):
            return
        self._write_index(self.index)
        self._advance()

    def write_remaining(self) -> None:
        wanted = [i for i, item in enumerate(self.session.items)
                  if item.tickable and (not item.proposal.is_update or item.ticked)]
        if not self._confirm_updates(wanted):
            return
        results = self.session.write_remaining_confident()
        self._report_problems(results)
        if self.view == VIEW_ONE and self.session.next_pending(-1) is None:
            self._finish()  # nothing left to decide: the summary, then the page closes
            return
        self._summary_box()
        self._refresh_all()

    def _write_index(self, index: int) -> None:
        result = self.session.write(index)
        self._report_problems([result])

    def _report_problems(self, results) -> None:
        bad = [r for r in results if r.status in ("exists", "too_large", "error", "changed")]
        if bad and len(results) == 1:  # one note asked for: say at once why it was not written
            QMessageBox.warning(self, self.windowTitle(),
                                f"The note was not written.\n\n{bad[0].message or bad[0].status}\n{bad[0].path}")

    def _finish(self) -> None:
        self._summary_box()
        self.accept()

    def _summary_box(self) -> None:
        totals = self.session.totals()
        self.last_summary = (totals.text() + (f"\n\n{self._lookup_note}" if self._lookup_note else "")
                             + (f"\n\n{self._history_note}" if self._history_note and totals.written else ""))
        QMessageBox.information(self, f"{self.windowTitle()}: summary", self.last_summary)

    # -- the list ----------------------------------------------------------------------------------------------

    def _fill_tree(self) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        only_open = self.filter_check.isChecked()
        for i, item in enumerate(self.session.items):
            if only_open and item.proposal.writable:
                continue
            row = QTreeWidgetItem([str(item.proposal.folder), os.path.basename(item.proposal.path), item.confidence_word,
                                   item.state_word])
            row.setData(0, Qt.ItemDataRole.UserRole, i)
            row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            row.setCheckState(0, Qt.CheckState.Checked if item.ticked else Qt.CheckState.Unchecked)
            if not item.tickable:
                row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            self.tree.addTopLevelItem(row)
        self.tree.blockSignals(False)
        self._refresh_list_header()

    def _filter_toggled(self, _on: bool = False) -> None:
        """Refilling the list (filter toggled, Tick all, Untick all) clears the current row (the tree was cleared with its
        signals blocked): select one again, the one that was current when it is still listed, else the first."""
        self._fill_tree()
        self._select_tree_index(self.index)

    def _select_tree_index(self, index: int) -> None:
        for r in range(self.tree.topLevelItemCount()):
            row = self.tree.topLevelItem(r)
            if row.data(0, Qt.ItemDataRole.UserRole) == index:
                self.tree.setCurrentItem(row)
                return
        if self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        else:
            self.list_panel.show_item(None)

    def _on_tree_current(self, current, _previous) -> None:
        if current is None:
            self.list_panel.show_item(None)
            return
        self.index = current.data(0, Qt.ItemDataRole.UserRole)
        self.list_panel.show_item(self.session.items[self.index])

    def _on_tree_check(self, row, column) -> None:
        if column != 0:
            return
        i = row.data(0, Qt.ItemDataRole.UserRole)
        ticked = row.checkState(0) == Qt.CheckState.Checked
        if not self.session.set_ticked(i, ticked):
            self.tree.blockSignals(True)
            row.setCheckState(0, Qt.CheckState.Unchecked)
            self.tree.blockSignals(False)
        self._refresh_list_header()

    def _tick_all(self) -> None:
        self.session.tick_confident()
        self._filter_toggled(False)

    def _untick_all(self) -> None:
        for i in range(len(self.session.items)):
            self.session.set_ticked(i, False)
        self._filter_toggled(False)

    def _refresh_list_header(self) -> None:
        n = len(self.session.ticked_pending())
        self.write_ticked_button.setText(f"Write {n} ticked note{'s' if n != 1 else ''}")
        self.write_ticked_button.setEnabled(n > 0)
        totals = self.session.totals()
        self.header_label.setText(
            f"{len(self.session.items)} folders: {totals.written} written, {totals.skipped} skipped, "
            f"{totals.uncertain_left} uncertain left")

    def write_ticked(self) -> None:
        indices = self.session.ticked_pending()
        if not self._confirm_updates(indices):
            return
        results = self.session.write_ticked()
        self._report_problems(results)
        self._summary_box()
        self._refresh_all()

    # -- choices -----------------------------------------------------------------------------------------------

    def _choose_candidate(self, candidate: Candidate) -> None:
        if self._busy or not self.session.items:
            return  # a second click (or Enter) while the first rebuild is still running
        # The row the choice was made on, captured BEFORE the call: the selection can move while it runs (2026-10-16
        # review: row B was overwritten with A's rebuilt note). The window is also disabled during run_busy, so this is
        # defence in depth.
        index = max(0, min(self.index, len(self.session.items) - 1))
        item = self.session.items[index]
        self._busy = True
        try:
            rebuilt = run_busy(self, "Building the note from your choice...", self._rebuild, item.proposal, candidate)
        finally:
            self._busy = False
        if index >= len(self.session.items) or self.session.items[index] is not item:
            self._say("The row changed while the note was being built: that result was dropped. Choose again.")
            return
        self.session.replace(index, rebuilt)
        self._refresh_all()

    def _say(self, text: str) -> None:
        self.status_text = text
        self.banner_label.setText(text)
        self.banner_label.setVisible(True)

    def open_current_note(self) -> None:
        item = self.session.items[self.index]
        if item.written_path:
            self._open_path(item.written_path)

    def show_current_folder(self) -> None:
        item = self.session.items[self.index]
        if item.written_path:
            self._open_path(str(Path(item.written_path).parent))

    # -- closing -----------------------------------------------------------------------------------------------

    def reject(self) -> None:
        waiting = len(self.session.ticked_pending())
        if waiting:
            answer = QMessageBox.question(
                self, self.windowTitle(),
                f"{waiting} ticked note{'s have' if waiting != 1 else ' has'} not been written. Close anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        super().reject()

    def done(self, result: int) -> None:
        if self._on_size is not None:
            self._on_size(self.width(), self.height())
        super().done(result)

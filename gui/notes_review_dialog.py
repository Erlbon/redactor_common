"""
redactor_common/gui/notes_review_dialog.py

The REVIEW PAGE for folder notes, shared by every app and media type that writes them. Its template is mp3redactor's page,
the one used on real libraries (2026-11-06, Erlend: "the true notes capabilities ... the template for others"); what only
some media types have is switched on through ReviewHooks. The rules are core/notes_review.py's ReviewSession; the
proposals come from the app's engine. Nothing here touches the network on the GUI thread: a rebuild after a person's
choice runs in run_busy(), the best guesses under run_work_with_progress().

  NotesReviewDialog(session, view, rebuild=..., hooks=...)   two views of the same ReviewSession, switchable:
                                     one at a time  (one folder, or one owner folder with its children): Skip / Write note / Back
                                     list           (several folders): ticks, Write N ticked notes; the rows are UPDATED IN
                                                    PLACE after a pick (the list never jumps), a hint under it says why a row
                                                    cannot be ticked yet
  ProposalPanel                    what the page shows of one proposal: path, confidence, reasons, candidates (pick the right
                                   one), the note preview (read-only: Pluteus edits notes), and the hooks' extras
  ReviewHooks                      the extras: a level choice, an explicit minimal local note, Look up by link, the genres a
                                   genre folder covers, a Local files tab, Tick best guesses, why a candidate cannot be used,
                                   a follow-up run ('Create the album notes for these artists too'), a troubleshooting log

Nothing is written without an explicit click on Write note / Write N ticked notes / Write all remaining confident. An
uncertain or nothing-found proposal cannot be ticked or written until a candidate (or a level, a link) was chosen: the
app's `rebuild` builds it again and, if a note could be made, it is confident.

`rebuild(proposal, candidate)` is all a page without hooks calls; with a level, genres or a link it is called as
`rebuild(proposal, candidate, level, genres=..., link=...)` (only the keywords that were given).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QDesktopServices, QTextDocument
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser,
    QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from redactor_common.core.notes import STATE_LEGACY, parse_front_matter
from redactor_common.core.notes_model import Candidate, Confidence, Proposal
from redactor_common.core.notes_review import (
    CONFIDENCE_WORDS, ERROR, EXISTS, PENDING, SKIPPED, TOO_LARGE, VIEW_LIST, VIEW_ONE, WRITTEN, ReviewItem,
    ReviewSession, note_diff,
)
from redactor_common.gui.owned_dialog import OwnedDialog
from redactor_common.gui.progress import run_busy
from redactor_common.gui.table_columns import make_columns_resizable
from redactor_common.gui.work_progress import run_work_with_progress

__all__ = ["NotesReviewDialog", "ProposalPanel", "ReviewHooks", "diff_html", "open_in_system", "short_path"]

DEFAULT_SIZE = (1100, 700)

# Confidence colours: a tinted background with dark text, readable in a light and a dark theme.
BADGE_STYLES = {
    Confidence.CONFIDENT: "background:#c9ecd1; color:#12401f;",
    Confidence.UNCERTAIN: "background:#fbe3a6; color:#4d3600;",
    Confidence.NONE: "background:#dcdcdc; color:#333333;",
}
TEXT_COLOURS = {Confidence.CONFIDENT: "#1b7f3b", Confidence.UNCERTAIN: "#a86a00", Confidence.NONE: "#707070"}
UPDATE_BADGE_STYLE = "background:#cfe0f7; color:#10325c; border-radius:4px; padding:3px 8px; font-weight:bold;"
LEGACY_WARNING = "Made by the first versions: cannot tell whether you edited it. Look at Before / after before you update it."
RECYCLE_NOTE = "The old note goes to the Recycle Bin. You can restore it from the Recycle Bin."
NOT_TICKABLE_HINT = "Not tickable yet: pick a candidate or a level first."


@dataclass
class ReviewHooks:
    """What a media type adds to the review page. Every part is optional and off by default: a page without hooks is the
    plain review page (candidates, preview, write)."""

    # a level choice ('Build this folder as:'): keys the proposal suggests (proposal.suggested_levels) -> their labels
    level_labels: dict[str, str] = field(default_factory=dict)
    current_level_key: Callable[[Proposal], str] = lambda p: str(getattr(p, "level", "") or "")
    skip_level: str = "skip"  # the key that skips the folder (its own button: a skipped row is gone from the list)
    # the explicit minimal note from the folder alone (never automatic, never in a batch): rebuild(level=local_level)
    local_level: str = ""
    local_label: str = "Create a minimal note from the folder"
    local_tooltip: str = ""
    can_make_local: Callable[[Proposal], bool] = lambda p: False
    # 'Look up by link': link_placeholder(p) is the hint text of the box ('' = no box for this row); check_link(p, text) is
    # '' for a link the engine can use, else the plain message shown under the box (nothing is fetched to check it)
    link_placeholder: Optional[Callable[[Proposal], str]] = None
    check_link: Callable[[Proposal, str], str] = lambda p, text: ""
    link_errors: tuple = (ValueError,)  # what rebuild(link=...) raises for a link it cannot use (said under the box)
    # a genre / collection folder: the genres it covers, edited and looked up again (None = no box for this row)
    genres_of: Optional[Callable[[Proposal], Optional[list[str]]]] = None
    # a 'Local files' tab: evidence_view() makes the widget (show_folder(folder, kind), clear(), `_pending`)
    evidence_view: Optional[Callable[[], QWidget]] = None
    evidence_applies: Callable[[Proposal], bool] = lambda p: False
    # Tick best guesses, and why a candidate cannot be used
    best_guess: Optional[Callable[[Proposal], Optional[Candidate]]] = None
    why_not: Optional[Callable[[Proposal, Candidate], str]] = None
    # a follow-up run for the rows of `followup_kind` written on this page (the app runs it: NotesReviewDialog.followup)
    followup_kind: str = ""
    followup_label: str = ""
    followup_tooltip: str = ""
    followup_question: Optional[Callable[[int], str]] = None  # asked when the page closes; n written -> the question
    # the opt-in troubleshooting log: log(step, **fields), only counts, scroll values, indexes and folder NAMES
    log: Optional[Callable[..., None]] = None
    recycle_note: str = RECYCLE_NOTE


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


def short_path(folder: Path, root: Optional[str]) -> str:
    try:
        return os.path.relpath(str(folder), root) if root else str(folder)
    except ValueError:  # another drive
        return str(folder)


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


def _plural_n(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _columns_resizable(view, wide_column: Optional[int] = None) -> None:
    """The family's header rule (every column can be dragged): at once, and again when the dialog has been laid out."""
    make_columns_resizable(view, wide_column)

    def later() -> None:
        try:
            make_columns_resizable(view, wide_column)
        except RuntimeError:  # the dialog was closed and deleted before the event loop got here
            pass

    QTimer.singleShot(0, later)


# ---------------------------------------------------------------------------------------------------------------
# One proposal
# ---------------------------------------------------------------------------------------------------------------

class ProposalPanel(QWidget):
    """The detail of one proposal. Read-only: a person's choices go out as signals."""

    candidateChosen = pyqtSignal(object)  # a Candidate
    levelChosen = pyqtSignal(str)  # a suggested level key (or the hooks' local_level)
    genresChosen = pyqtSignal(list)  # the genres a genre / collection folder covers, as edited
    linkChosen = pyqtSignal(str)  # a pasted link, already checked
    openNoteRequested = pyqtSignal()
    showFolderRequested = pyqtSignal()

    def __init__(self, can_choose: Callable[[Proposal, Candidate], bool], parent=None,
                 candidate_columns: tuple[str, ...] = ("Name", "Details", "Score", "Source", "Matches"),
                 hooks: Optional[ReviewHooks] = None):
        super().__init__(parent)
        self._can_choose = can_choose
        self.hooks = hooks or ReviewHooks()
        self._item: Optional[ReviewItem] = None
        self._candidates: list[Candidate] = []
        self._edits_row: Optional[str] = None

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
        self.legacy_label = QLabel(LEGACY_WARNING)
        self.legacy_label.setWordWrap(True)
        self.legacy_label.setStyleSheet("background:#fff0c2; color:#4d3600; border:1px solid #e0c36a; padding:4px 8px;")
        self.legacy_label.setVisible(False)

        self.reasons_label = QLabel()
        self.reasons_label.setTextFormat(Qt.TextFormat.RichText)
        self.problems_label = QLabel()
        self.problems_label.setTextFormat(Qt.TextFormat.RichText)

        # the level choice (hooks.level_labels)
        self.level_box = QWidget()
        level_row = QHBoxLayout(self.level_box)
        level_row.setContentsMargins(0, 0, 0, 0)
        self.level_caption = QLabel("Build this folder as:")
        level_row.addWidget(self.level_caption)
        self.level_combo = QComboBox()
        self.level_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.level_combo.setMinimumContentsLength(14)
        level_row.addWidget(self.level_combo, 1)
        # choosing a level acts at once, like Look up by link; only 'Skip' waits for a second click (a skipped row is gone)
        self.level_combo.activated.connect(self._level_activated)
        self.level_button = _button("Skip this folder")
        self.level_button.clicked.connect(self._level_clicked)
        self.level_button.setVisible(False)
        level_row.addWidget(self.level_button)
        level_row.addStretch(1)

        # the candidates
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
        self._candidate_columns_set = False
        self.candidate_table.itemSelectionChanged.connect(self._update_buttons)
        cand_layout.addWidget(self.candidate_table)
        cand_row = QHBoxLayout()
        self.candidate_button = _button("Use selected candidate")
        self.candidate_button.clicked.connect(self._candidate_clicked)
        self.candidate_note = QLabel()
        cand_row.addWidget(self.candidate_button)
        cand_row.addWidget(self.candidate_note, 1)
        cand_layout.addLayout(cand_row)

        # the explicit local-only note (hooks.local_level: never automatic, never in a batch)
        self.local_box = QWidget()
        local_row = QHBoxLayout(self.local_box)
        local_row.setContentsMargins(0, 0, 0, 0)
        self.local_button = _button(self.hooks.local_label)
        self.local_button.setToolTip(self.hooks.local_tooltip)
        self.local_button.clicked.connect(lambda: self.levelChosen.emit(self.hooks.local_level))
        local_row.addWidget(self.local_button)
        local_row.addStretch(1)

        # 'Look up by link' (hooks.link_placeholder)
        self.link_box = QWidget()
        link_layout = QVBoxLayout(self.link_box)
        link_layout.setContentsMargins(0, 0, 0, 0)
        link_row = QHBoxLayout()
        link_row.addWidget(QLabel("Link:"))
        self.link_edit = QLineEdit()
        self.link_button = _button("Look up by link")
        self.link_button.clicked.connect(self._link_clicked)
        self.link_edit.returnPressed.connect(self._link_clicked)
        self.link_edit.textChanged.connect(lambda _t: self.set_link_error(""))
        link_row.addWidget(self.link_edit, 1)
        link_row.addWidget(self.link_button)
        self.link_error = QLabel()
        self.link_error.setStyleSheet("color:#b00020;")
        self.link_error.setWordWrap(True)
        self.link_error.setVisible(False)
        link_layout.addLayout(link_row)
        link_layout.addWidget(self.link_error)

        # the genres a genre / collection folder covers (hooks.genres_of)
        self.genres_box = QWidget()
        genres_row = QHBoxLayout(self.genres_box)
        genres_row.setContentsMargins(0, 0, 0, 0)
        genres_row.addWidget(QLabel("Genres covered:"))
        self.genres_edit = QLineEdit()
        self.genres_edit.setPlaceholderText("one genre, or several separated by commas (Ska, Ska punk)")
        self.genres_edit.setToolTip("The genres this folder covers, separated by commas. Each is checked on Wikipedia as a "
                                    "music genre; the note gets one section per genre.")
        self.genres_button = _button("Look up again")
        self.genres_button.clicked.connect(self._genres_clicked)
        self.genres_edit.returnPressed.connect(self._genres_clicked)
        genres_row.addWidget(self.genres_edit, 1)
        genres_row.addWidget(self.genres_button)

        # the preview (rendered) and the raw text; read-only
        self.preview = QTextBrowser()
        self.preview.setOpenExternalLinks(True)
        self.raw = QTextEdit()
        self.raw.setReadOnly(True)
        self.raw.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        mono = self.raw.font()
        mono.setFamily("Consolas")
        self.raw.setFont(mono)
        self.diff_view = QTextBrowser()  # an UPDATE: the note now (red) against the new draft (green)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.preview, "Preview")
        self.tabs.addTab(self.raw, "Raw text")
        self.diff_tab = self.tabs.addTab(self.diff_view, "Before / after")
        self.tabs.setTabVisible(self.diff_tab, False)
        self.evidence_view = self.hooks.evidence_view() if self.hooks.evidence_view else None
        self.evidence_tab = self.tabs.addTab(self.evidence_view, "Local files") if self.evidence_view is not None else -1
        if self.evidence_tab >= 0:
            self.tabs.setTabVisible(self.evidence_tab, False)
        self._auto_local = False  # the tab was switched to Local files by the page, not by the person
        self.tabs.currentChanged.connect(lambda _i: self._sync_evidence())

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
        layout.addWidget(self.legacy_label)
        layout.addWidget(self.reasons_label)
        layout.addWidget(self.problems_label)
        layout.addWidget(self.level_box)
        layout.addWidget(self.candidate_box)
        layout.addWidget(self.local_box)
        layout.addWidget(self.genres_box)
        layout.addWidget(self.link_box)
        layout.addWidget(self.tabs, 1)
        layout.addLayout(foot)
        _shrinkable(self.path_label, self.kind_label, self.reasons_label, self.problems_label, self.sources_label,
                    self.candidate_caption, self.candidate_note, self.level_caption)
        self.show_item(None)

    # -- showing ---------------------------------------------------------------------------------------------

    @staticmethod
    def render_body(text: str) -> str:
        """The note without its front matter (the raw tab shows everything)."""
        if not text:
            return ""
        _meta, body = parse_front_matter(text)
        return body or text

    @staticmethod
    def _bullets(lines: list[str]) -> str:
        return "<br>".join(f"&bull; {escape(line)}" for line in lines)

    def level_alternatives(self, proposal: Proposal) -> list[str]:
        """The levels a person may switch this proposal to (never the one it already has), when there is a real choice
        beyond skipping."""
        if not self.hooks.level_labels:
            return []
        current = self.hooks.current_level_key(proposal)
        options = [s for s in getattr(proposal, "suggested_levels", []) or [] if s != current]
        return options if any(o != self.hooks.skip_level for o in options) else []

    def show_item(self, item: Optional[ReviewItem]) -> None:
        self._item = item
        if item is None:
            self.path_label.setText("Nothing selected")
            for w in (self.kind_label, self.state_label, self.badge, self.reasons_label, self.problems_label,
                      self.sources_label):
                w.clear()
            self.badge.setStyleSheet("")
            for w in (self.update_badge, self.legacy_label, self.level_box, self.candidate_box, self.local_box,
                      self.genres_box, self.link_box, self.open_note_button, self.show_folder_button):
                w.hide()
            self.tabs.setTabVisible(self.diff_tab, False)
            self.diff_view.clear()
            if self.evidence_view is not None:
                self.tabs.setTabVisible(self.evidence_tab, False)
                self.evidence_view.clear()
            self.preview.clear()
            self.raw.clear()
            self._edits_row = None  # nothing shown: the typed link / genres belong to no row any more
            self.link_edit.clear()
            self.set_link_error("")
            return
        p = item.proposal
        hooks = self.hooks
        self.path_label.setText(str(p.folder))
        level = str(getattr(p, "level", "") or "")
        self.kind_label.setText(f"Note: {os.path.basename(p.path)}" + (f" - {p.title}" if p.title else "")
                                + (f"  |  level: {level}" if level else ""))
        self.update_badge.setVisible(p.is_update)
        self.legacy_label.setVisible(p.is_update and p.old_state.kind == STATE_LEGACY)
        self.tabs.setTabVisible(self.diff_tab, p.is_update and bool(p.note_text))
        if p.is_update and p.note_text:
            self.diff_view.setHtml(diff_html(p.old_text, p.note_text) + f"<p>{escape(hooks.recycle_note)}</p>")
        else:
            self.diff_view.clear()
            if self.tabs.currentIndex() == self.diff_tab:
                self.tabs.setCurrentIndex(0)
        self.badge.setText(CONFIDENCE_WORDS[p.confidence])
        self.badge.setStyleSheet(BADGE_STYLES[p.confidence] + " border-radius:4px; padding:3px 8px; font-weight:bold;")
        self.state_label.setText(item.state_word)
        if item.state == WRITTEN:
            self.state_label.setStyleSheet("color:#1b7f3b; font-weight:bold;")
        elif item.state in (EXISTS, ERROR, TOO_LARGE):
            self.state_label.setStyleSheet("color:#b00020; font-weight:bold;")
        else:
            self.state_label.setStyleSheet("")
        level_reasons = list(getattr(p, "level_reasons", []) or [])
        reasons = list(p.reasons) + [r for r in level_reasons if r not in p.reasons]
        if item.message and item.state in (EXISTS, ERROR, TOO_LARGE):
            reasons.insert(0, f"NOT written: {item.message}")
        self.reasons_label.setText(self._bullets(reasons) if reasons else "")
        self.problems_label.setText(
            "<span style='color:#b00020'>" + self._bullets(p.problems) + "</span>" if p.problems else "")
        self.reasons_label.setVisible(bool(reasons))
        self.problems_label.setVisible(bool(p.problems))

        alternatives = self.level_alternatives(p) if item.state == PENDING else []
        self.level_box.setVisible(bool(alternatives))
        self.level_combo.clear()
        self.level_combo.addItem("Choose...", None)  # nothing is built until a level is picked from the list
        for key in alternatives:
            self.level_combo.addItem(hooks.level_labels.get(key, key), key)
        self.level_combo.setCurrentIndex(0)
        self.level_button.setVisible(False)

        self._candidates = list(p.candidates)
        self.candidate_box.setVisible(bool(self._candidates) and item.state == PENDING and (
            p.confidence != Confidence.CONFIDENT or item.user_resolved))
        self._show_evidence_tab(item)
        placeholder = hooks.link_placeholder(p) if hooks.link_placeholder and item.state == PENDING else ""
        self.link_box.setVisible(bool(placeholder))
        if placeholder:
            self.link_edit.setPlaceholderText(placeholder)
        # What was typed into the link / genres fields belongs to ONE folder: a different row starts with an empty link
        # and its own genres (a link pasted for the previous band must never be offered for this one). The same row shown
        # again (after a rebuild in place, a state change) keeps what the person typed.
        row_key = str(p.folder)
        new_row = row_key != self._edits_row
        if new_row:
            self._edits_row = row_key
            self.link_edit.clear()
            self.set_link_error("")
        genres = hooks.genres_of(p) if hooks.genres_of and item.state == PENDING else None
        self.genres_box.setVisible(genres is not None)
        if genres is not None and (new_row or not self.genres_edit.hasFocus()):
            self.genres_edit.setText(", ".join(genres))
        self.local_box.setVisible(bool(hooks.local_level) and item.state == PENDING and not p.writable
                                  and hooks.can_make_local(p))
        self._fill_candidates(p)

        self.raw.setPlainText(p.note_text)
        flags = QTextDocument.MarkdownFeature.MarkdownDialectGitHub | QTextDocument.MarkdownFeature.MarkdownNoHTML
        if p.note_text:
            # MarkdownNoHTML: a note (or a Wikipedia extract in it) containing <b>, <img> or <script> shows that text
            self.preview.document().setMarkdown(self.render_body(p.note_text), flags)
        else:
            self.preview.document().setMarkdown(
                "*No draft yet.* " + ("Pick one of the candidates above, or a level, to build a note."
                                      if (self._candidates or alternatives) else
                                      "Nothing was found online for this folder (see the reasons above)."
                                      + (f" You can still use '{hooks.local_label}' above."
                                         if self.local_box.isVisibleTo(self) else "")), flags)
        self.sources_label.setText("Sources: " + ", ".join(p.sources) if p.sources else "Sources: none (no draft)")
        written = item.state == WRITTEN and bool(item.written_path)
        self.open_note_button.setVisible(written or p.is_update)
        self.show_folder_button.setVisible(True)
        self._update_buttons()

    def _fill_candidates(self, p: Proposal) -> None:
        table = self.candidate_table
        table.setRowCount(0)
        for cand in self._candidates:
            row = table.rowCount()
            table.insertRow(row)
            hits = ", ".join(cand.hits) if cand.hits else "none"
            source = cand.source + (f" + {cand.also}" if cand.also else "")
            for col, text in enumerate((cand.name, cand.detail, f"{cand.score:.2f}", source, hits)[: table.columnCount()]):
                table.setItem(row, col, QTableWidgetItem(text))
                table.item(row, col).setToolTip(cand.note or text)
        if self._candidates:
            table.selectRow(0)
        if self._candidates and not self._candidate_columns_set:  # once: a person's dragged widths stay
            self._candidate_columns_set = True
            _columns_resizable(table, 0)

    def selected_candidate(self) -> Optional[Candidate]:
        row = self.candidate_table.currentRow()
        return self._candidates[row] if 0 <= row < len(self._candidates) else None

    def _update_buttons(self) -> None:
        cand = self.selected_candidate()
        ok = bool(cand and self._item and self._can_choose(self._item.proposal, cand))
        self.candidate_button.setEnabled(ok)
        if ok or cand is None or self._item is None:
            self.candidate_note.setText("")
        else:
            why = self.hooks.why_not(self._item.proposal, cand) if self.hooks.why_not else ""
            self.candidate_note.setText(why or "This candidate cannot be used for this folder.")

    def _candidate_clicked(self) -> None:
        cand = self.selected_candidate()
        if cand is not None:
            self.candidateChosen.emit(cand)

    def _show_evidence_tab(self, item: ReviewItem) -> None:
        """The Local files tab, where the hooks say it applies. A row with no note to preview (uncertain, not found) opens on
        it: what the folder holds is the evidence the person needs; a row with a note goes back to the preview if the page
        had switched."""
        if self.evidence_view is None:
            return
        p = item.proposal
        has = self.hooks.evidence_applies(p)
        self.tabs.setTabVisible(self.evidence_tab, has)
        if has and not p.note_text and self.tabs.currentIndex() == 0:
            self._auto_local = True
            self.tabs.setCurrentIndex(self.evidence_tab)
        elif self._auto_local and p.note_text and self.tabs.currentIndex() == self.evidence_tab:
            self._auto_local = False
            self.tabs.setCurrentIndex(0)
        self._sync_evidence()

    def _sync_evidence(self) -> None:
        item = self._item
        if self.evidence_view is None or item is None:
            return
        if self.tabs.currentIndex() != self.evidence_tab or not self.tabs.isTabVisible(self.evidence_tab):
            return
        folder = item.proposal.target.folder
        if getattr(self.evidence_view, "_pending", None) == (str(folder), item.proposal.kind):  # shown, or being read
            return
        self.evidence_view.show_folder(folder, item.proposal.kind)

    def set_link_error(self, text: str) -> None:
        self.link_error.setText(text)
        self.link_error.setVisible(bool(text))

    def _link_clicked(self) -> None:
        if self._item is None:
            return
        text = self.link_edit.text().strip()
        problem = self.hooks.check_link(self._item.proposal, text)  # checked here: nothing is fetched yet
        if problem:
            self.set_link_error(problem)
            return
        self.set_link_error("")
        self.linkChosen.emit(text)

    def _genres_clicked(self) -> None:
        genres = [g.strip() for g in self.genres_edit.text().replace(";", ",").split(",") if g.strip()]
        if genres:
            self.genresChosen.emit(genres)

    def _level_activated(self, index: int) -> None:
        """A level picked in the list: built at once (no second button), except 'Skip', which shows its own button first."""
        key = self.level_combo.itemData(index)
        skip = key == self.hooks.skip_level
        self.level_button.setVisible(skip)
        if key and not skip:
            self.levelChosen.emit(str(key))

    def _level_clicked(self) -> None:  # the 'Skip this folder' button
        key = self.level_combo.currentData()
        if key:
            self.levelChosen.emit(str(key))


# ---------------------------------------------------------------------------------------------------------------
# The review page
# ---------------------------------------------------------------------------------------------------------------

class NotesReviewDialog(OwnedDialog):
    def __init__(
        self, session: ReviewSession, view: str,
        rebuild: Callable[..., Proposal],
        can_choose: Callable[[Proposal, Candidate], bool] = lambda _p, _c: True,
        banner: str = "", parent=None, title: str = "Create Notes", size: Optional[tuple[int, int]] = None,
        on_size: Optional[Callable[[int, int], None]] = None, open_path: Callable[[str], None] = open_in_system,
        candidate_columns: tuple[str, ...] = ("Name", "Details", "Score", "Source", "Matches"),
        lookup_note: str = "", history_note: str = "", hooks: Optional[ReviewHooks] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.hooks = hooks or ReviewHooks()
        self._lookup_note = lookup_note  # 'Looked up N folders in H:MM:SS (R requests, C from the cache).'
        self._history_note = history_note  # 'Recorded in Tools > History (it is switched on).'
        self.followup: list[str] = []  # the folders (hooks.followup_kind) the person asked the follow-up run for
        self._busy = False  # a rebuild / the best guesses are running: no second one may start
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
        self._root = self._common_root()
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
        self.followup_button = _button(self.hooks.followup_label or "Follow up")
        self.followup_button.setToolTip(self.hooks.followup_tooltip)
        self.followup_button.setVisible(False)
        self.followup_button.clicked.connect(self._followup_clicked)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_one_page())
        self.stack.addWidget(self._build_list_page())
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.banner_label)
        layout.addWidget(self.followup_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.stack, 1)
        self._fill_tree()
        self._apply_view()

    # -- building --------------------------------------------------------------------------------------------

    def _make_panel(self) -> ProposalPanel:
        panel = ProposalPanel(self._can_choose, candidate_columns=self._candidate_columns, hooks=self.hooks)
        panel.candidateChosen.connect(self._choose_candidate)
        panel.levelChosen.connect(self._choose_level)
        panel.genresChosen.connect(self._choose_genres)
        panel.linkChosen.connect(self._choose_link)
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
        self.tree.setHeaderLabels(["Folder", "Level", "Confidence", "State"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # A STABLE geometry (a person saw the rows jump up and down on a selection): both scroll bars are always there (one
        # appearing or disappearing moves every row), scrolling is per pixel, long paths are elided in the middle.
        self.tree.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.tree.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self._tree_columns_set = False
        self.tree.currentItemChanged.connect(self._on_tree_current)
        self.tree.itemChanged.connect(self._on_tree_check)
        self.list_panel = self._make_panel()

        self.filter_check = QCheckBox("Only uncertain or not found")
        self.filter_check.toggled.connect(self._filter_toggled)
        self.tick_all_button = _button("Tick all confident")
        self.untick_all_button = _button("Untick all")
        self.tick_guesses_button = _button("Tick best guesses")
        self.tick_guesses_button.setToolTip(
            "Uncertain matches that have a candidate and a clear level: use the top candidate and tick them. "
            "RISK: the match was not confirmed by what is in your folders, so a note may describe the wrong one. "
            "They are still written as drafts (status: draft) and nothing is written until you click Write.")
        self.tick_guesses_button.setVisible(self.hooks.best_guess is not None)
        self.tick_guesses_button.clicked.connect(self._tick_best_guesses)
        self.tick_all_button.clicked.connect(self._tick_all)
        self.untick_all_button.clicked.connect(self._untick_all)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.filter_check)
        left_layout.addWidget(self.tree, 1)
        self.row_hint = QLabel()  # the path of the current row and why it cannot be ticked: stable, under the list
        self.row_hint.setStyleSheet("color:#666666;")
        self.row_hint.setFixedHeight(2 * self.row_hint.fontMetrics().lineSpacing() + 4)  # two lines, always: no jumping
        self.row_hint.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.row_hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        left_layout.addWidget(self.row_hint)

        # The panel's content changes with every row: in a scroll area its size hints never push the splitter around.
        self.list_scroll = QScrollArea()
        self.list_scroll.setWidgetResizable(True)
        self.list_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.list_scroll.setWidget(self.list_panel)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(self.list_scroll)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([430, 640])
        self.splitter = splitter

        self.write_ticked_button = QPushButton("Write 0 ticked notes")
        self.write_ticked_button.setAutoDefault(False)
        self.write_ticked_button.clicked.connect(self.write_ticked)
        self.close_list_button = _button("Close")
        self.close_list_button.clicked.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(self.tick_all_button)
        row.addWidget(self.untick_all_button)
        row.addWidget(self.tick_guesses_button)
        row.addStretch(1)
        row.addWidget(self.write_ticked_button)
        row.addWidget(self.close_list_button)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter, 1)
        layout.addLayout(row)
        return page

    def _common_root(self) -> Optional[str]:
        folders = [str(item.proposal.folder.parent) for item in self.session.items]
        try:
            return os.path.commonpath(folders) if folders else None
        except ValueError:
            return None

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
        self._update_followup()

    # -- the follow-up run ---------------------------------------------------------------------------------------

    def _written_followup_folders(self) -> list[str]:
        kind = self.hooks.followup_kind
        return [str(i.proposal.folder) for i in self.session.items if kind and i.state == WRITTEN and i.proposal.kind == kind]

    def _update_followup(self) -> None:
        self.followup_button.setVisible(bool(self.hooks.followup_kind) and bool(self._written_followup_folders()))

    def _followup_clicked(self) -> None:
        self.followup = self._written_followup_folders()
        if self.followup:
            self.accept()

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
        self._update_followup()

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
        text = f"Replace {_plural_n(n, 'note')} made by an older version?\n\n{self.hooks.recycle_note}"
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
        folders = self._written_followup_folders() if self.hooks.followup_question and not self.followup else []
        if folders:  # the page is closing: the one click for the follow-up run
            answer = QMessageBox.question(
                self, self.windowTitle(), self.hooks.followup_question(len(folders)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer == QMessageBox.StandardButton.Yes:
                self.followup = folders
        self.accept()

    def _summary_box(self) -> None:
        totals = self.session.totals()
        self.last_summary = (totals.text() + (f"\n\n{self._lookup_note}" if self._lookup_note else "")
                             + (f"\n\n{self._history_note}" if self._history_note and totals.written else ""))
        QMessageBox.information(self, f"{self.windowTitle()}: summary", self.last_summary)

    # -- the list --------------------------------------------------------------------------------------------

    def _filter_toggled(self, _on: bool = False) -> None:
        """'Only uncertain or not found' (and Tick all / Untick all): the list is rebuilt, and the row that was current
        stays current (a rebuild empties the selection), else the first."""
        self._fill_tree()
        self._select_tree_index(self.index)
        self._log("filter", on=self.filter_check.isChecked())

    def _log(self, step: str, touched=(), **fields) -> None:
        """The hooks' opt-in troubleshooting log: counts, scroll values, indexes and folder NAMES only."""
        if self.hooks.log is None:
            return
        tree = self.tree
        current = tree.currentItem()
        self.hooks.log(
            step, rows=tree.topLevelItemCount(), scroll=(tree.verticalScrollBar().value(), tree.horizontalScrollBar().value()),
            current=current.data(0, Qt.ItemDataRole.UserRole) if current is not None else None, index=self.index,
            touched=[self.session.items[i].proposal.folder.name for i in touched if 0 <= i < len(self.session.items)],
            **fields)

    def _update_row_hint(self) -> None:
        """Under the list, for the current row: its path, and why it has no tick box (a tooltip on the rows popped up at the
        mouse on every row it crossed)."""
        row = self.tree.currentItem()
        if row is None:
            self.row_hint.setText("")
            return
        item = self.session.items[row.data(0, Qt.ItemDataRole.UserRole)]
        metrics = self.row_hint.fontMetrics()
        width = max(self.row_hint.width(), 240)
        lines = [metrics.elidedText(item.proposal.path, Qt.TextElideMode.ElideMiddle, width)]  # one line, never wrapped
        if not item.tickable and item.state == PENDING:
            lines.append(NOT_TICKABLE_HINT)
        self.row_hint.setText("\n".join(lines))

    def _style_row(self, row: QTreeWidgetItem, i: int, item: ReviewItem) -> None:
        """Everything a row of the list shows, set from the session item `i` (a new row, or one updated in place)."""
        p = item.proposal
        level_text = (str(getattr(p, "level", "") or "") or p.kind) + (" - update" if p.is_update else "")
        for column, text in enumerate((short_path(p.folder, self._root), level_text, item.confidence_word, item.state_word)):
            row.setText(column, text)
        row.setData(0, Qt.ItemDataRole.UserRole, i)
        flags = row.flags() | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
        if item.tickable:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        else:
            flags &= ~Qt.ItemFlag.ItemIsUserCheckable
        row.setFlags(flags)
        if item.tickable:
            row.setCheckState(0, Qt.CheckState.Checked if item.ticked else Qt.CheckState.Unchecked)
        else:  # no checkbox at all: it has to be resolved (candidate / level) before it can be written
            row.setData(0, Qt.ItemDataRole.CheckStateRole, None)
        row.setForeground(2, QBrush(QColor(TEXT_COLOURS[p.confidence])))

    def _update_rows(self, indices) -> None:
        """After a pick (candidate, level, link, best guesses) the rows of those session items are UPDATED IN PLACE: the
        same row objects, positions, scroll position and selection; nothing is rebuilt (a rebuild is what made the list
        jump). With the 'Only uncertain' filter on, a row that became confident leaves the list."""
        tree = self.tree
        only_open = self.filter_check.isChecked()
        tree.blockSignals(True)
        try:
            for index in indices:
                for r in range(tree.topLevelItemCount()):
                    row = tree.topLevelItem(r)
                    if row.data(0, Qt.ItemDataRole.UserRole) != index:
                        continue
                    item = self.session.items[index]
                    if only_open and item.proposal.confidence == Confidence.CONFIDENT:
                        tree.takeTopLevelItem(r)
                    else:
                        self._style_row(row, index, item)
                    break
        finally:
            tree.blockSignals(False)
        current = tree.currentItem()
        if current is None and tree.topLevelItemCount():
            tree.setCurrentItem(tree.topLevelItem(0))
        elif current is not None:
            self.index = current.data(0, Qt.ItemDataRole.UserRole)
            self.list_panel.show_item(self.session.items[self.index])
        else:
            self.list_panel.show_item(None)
        self._refresh_list_header()
        self._update_row_hint()
        self._log("update_rows", touched=list(indices))

    def _fill_tree(self) -> None:
        tree = self.tree
        selected = self.index
        scroll_y, scroll_x = tree.verticalScrollBar().value(), tree.horizontalScrollBar().value()
        tree.blockSignals(True)
        tree.clear()
        only_open = self.filter_check.isChecked()
        for i, item in enumerate(self.session.items):
            if only_open and item.proposal.confidence == Confidence.CONFIDENT:
                continue
            row = QTreeWidgetItem()
            self._style_row(row, i, item)
            tree.addTopLevelItem(row)
        tree.blockSignals(False)
        tree.doItemsLayout()  # the scroll range is only updated by a layout
        tree.verticalScrollBar().setValue(scroll_y)  # clear() resets the view to the top: put it back where it was
        tree.horizontalScrollBar().setValue(scroll_x)
        if not self._tree_columns_set:  # once, with the first fill: a person's dragged widths stay on later refills
            self._tree_columns_set = True
            _columns_resizable(tree, 0)
        self.index = selected
        self._refresh_list_header()
        self._update_row_hint()
        self._log("fill_tree")

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
        self._update_row_hint()
        self._log("current", touched=[self.index])

    def _on_tree_check(self, row, column) -> None:
        if column != 0:
            return
        index = row.data(0, Qt.ItemDataRole.UserRole)
        wanted = row.checkState(0) == Qt.CheckState.Checked
        if not self.session.set_ticked(index, wanted):
            self.tree.blockSignals(True)
            row.setData(0, Qt.ItemDataRole.CheckStateRole, None)  # an unresolved proposal cannot be ticked
            self.tree.blockSignals(False)
        self._refresh_list_header()

    def _tick_all(self) -> None:
        self.session.tick_confident()
        self._filter_toggled()

    def _untick_all(self) -> None:
        for i in range(len(self.session.items)):
            self.session.set_ticked(i, False)
        self._filter_toggled()

    def guess_indices(self) -> list[int]:
        guess = self.hooks.best_guess
        if guess is None:
            return []
        return [i for i, item in enumerate(self.session.items)
                if item.state == PENDING and not item.proposal.writable and guess(item.proposal) is not None]

    def _tick_best_guesses(self) -> None:
        """Rebuilds every uncertain proposal that has a usable top candidate (hooks.best_guess) around that candidate, under
        a cancellable progress dialog; the rebuilt notes are ticked. Nothing is written."""
        if self._busy:
            return  # a second click (or Enter) while the first run is still going
        indices = self.guess_indices()
        if not indices:
            QMessageBox.information(self, self.windowTitle(),
                                    "No uncertain match has a candidate and a clear level to guess from.")
            return
        done: list[int] = []

        def work(progress):
            for n, i in enumerate(indices):
                if progress.should_cancel():
                    break
                proposal = self.session.items[i].proposal
                progress.set_value(n)
                progress.set_label(f"Looking up {proposal.folder.name} ({n + 1} of {len(indices)})…")
                try:
                    rebuilt = self._call_rebuild(proposal, self.hooks.best_guess(proposal), "")
                except Exception:  # noqa: BLE001 -- that folder stays as it was
                    continue
                self.session.replace(i, rebuilt)
                done.append(i)

        self._busy = True
        try:
            run_work_with_progress(self, len(indices), "Looking up the best guesses…", work, cancellable=True,
                                   title=self.windowTitle())
        finally:
            self._busy = False
        self._update_rows(done)  # the rows that were rebuilt, in place
        self._update_followup()

    def _refresh_list_header(self) -> None:
        if self.view != VIEW_LIST:
            return
        items = self.session.items
        counts = {c: sum(1 for i in items if i.proposal.confidence == c) for c in Confidence}
        self.header_label.setText(
            f"Create notes  {len(items)} folders: {counts[Confidence.CONFIDENT]} confident, "
            f"{counts[Confidence.UNCERTAIN]} uncertain, {counts[Confidence.NONE]} nothing found")
        n = len(self.session.ticked_pending())
        self.write_ticked_button.setText(f"Write {n} ticked note{'s' if n != 1 else ''}")
        self.write_ticked_button.setEnabled(n > 0)

    def write_ticked(self) -> None:
        if not self._confirm_updates(self.session.ticked_pending()):
            return
        results = self.session.write_ticked()
        self._report_problems(results)
        self._summary_box()
        self._fill_tree()
        self._select_tree_index(self.index)
        self._update_followup()

    # -- choices (candidate, level, genres, link) ------------------------------------------------------------

    def _call_rebuild(self, proposal: Proposal, candidate: Optional[Candidate], level: str, **extra) -> Proposal:
        """The app's rebuild: (proposal, candidate) when nothing more was chosen, so a page without hooks keeps the plain
        two-argument rebuild."""
        if not level and not extra:
            return self._rebuild(proposal, candidate)
        return self._rebuild(proposal, candidate, level, **extra)

    def _choose_candidate(self, candidate: Candidate) -> None:
        self._resolve(candidate, "")

    def _choose_level(self, level: str) -> None:
        if level == self.hooks.skip_level:
            self.session.skip(self.index)
            if self.view == VIEW_ONE:
                self._advance()
            else:
                self._update_rows([self.index])
            return
        self._resolve(None, level)

    def _choose_genres(self, genres: list) -> None:
        self._resolve(None, "", genres=genres)

    def _choose_link(self, link: str) -> None:
        self._resolve(None, "", link=link)

    def _resolve(self, candidate: Optional[Candidate], level: str, **extra) -> None:
        if self._busy or not self.session.items:
            return  # a second click (or Enter) while the first rebuild is still running
        # The row this choice was made on, captured BEFORE the call: the selection can move while it runs (2026-10-16
        # review: row B was overwritten with A's rebuilt note). The window is also disabled during run_busy.
        index = max(0, min(self.index, len(self.session.items) - 1))
        item = self.session.items[index]
        what = (candidate.name if candidate else ("the link" if "link" in extra else
                                                  (", ".join(extra["genres"]) if "genres" in extra
                                                   else self.hooks.level_labels.get(level, level) or "the note")))
        self._busy = True
        try:
            rebuilt = run_busy(self, f"Looking up {what}…", self._call_rebuild, item.proposal, candidate, level, **extra)
        except self.hooks.link_errors as exc:  # a link the engine cannot use: said under the box, nothing changes
            if "link" not in extra:
                QMessageBox.warning(self, self.windowTitle(), f"The lookup failed: {exc}")
                return
            (self.list_panel if self.view == VIEW_LIST else self.one_panel).set_link_error(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 -- shown as one readable message, never a traceback
            QMessageBox.warning(self, self.windowTitle(), f"The lookup failed: {exc}")
            return
        finally:
            self._busy = False
        if index >= len(self.session.items) or self.session.items[index] is not item:
            self._say("The row changed while the note was being built: that result was dropped. Choose again.")
            return
        self.session.replace(index, rebuilt)
        if self.view == VIEW_LIST:
            self._update_rows([index])  # that one row, in place
            self._update_followup()
        else:
            self._refresh_all()

    def _say(self, text: str) -> None:
        self.status_text = text
        self.banner_label.setText(text)
        self.banner_label.setVisible(True)

    # -- opening ---------------------------------------------------------------------------------------------

    def open_current_note(self) -> None:
        item = self.session.items[self.index]
        path = item.written_path or (item.proposal.path if item.proposal.is_update else "")
        if path:
            self._open_path(path)

    def show_current_folder(self) -> None:
        item = self.session.items[self.index]
        self._open_path(str(Path(item.written_path).parent) if item.written_path else str(item.proposal.folder))

    # -- closing ---------------------------------------------------------------------------------------------

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

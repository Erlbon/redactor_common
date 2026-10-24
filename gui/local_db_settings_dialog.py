"""
redactor_common/gui/local_db_settings_dialog.py

The Settings dialog for a local metadata database (core/local_db.py):
instructions for getting the file (the app never bundles or downloads
it), a path with Browse..., and Check File, which opens it read-only
and reports what's in it. Saving hands the path back to the app, which
stores it in its own settings.

Usage (cbzredactor's GCD dump):

    LocalDatabaseSettingsDialog(
        title="GCD Local Database",
        instructions_html=INSTRUCTIONS,
        path=app_settings.load_gcd_local_database(),
        check=lambda path: describe(GcdLocalDatabase(path)),   # -> str, raises on a bad file
        error_types=(GcdLocalError,),
        save=app_settings.save_gcd_local_database,
        parent=self,
    ).exec()

`build` (optional): adds a button (`build_label`) beside Check File for
a database the user makes rather than downloads -- e.g. cbzredactor's
"Build from ComicRack Library...", which converts a ComicDb.xml with
core/dump_import.py. It's called with the dialog as parent and returns
the new file's path (filled in, not yet saved) or None if abandoned.

THE BUILD TAIL (2026-10-14). Every built-database dialog (mp3
MusicBrainz, epub Open Library and ISFDB, video IMDb, cbz ComicRack)
repeated the same tail around its own build function; it lives here now,
switched on by keyword arguments so the plain download-a-file dialogs
are unchanged:

    default_path=default_database_path,   # placeholder, Browse start, build destination
    browse_for_destination=True,          # Browse = a Save dialog (the file may not exist yet)
    show_status=True,                     # "No database yet." / "Not built yet" / what Check File says
    free_space_warning=(15 << 30, " That may not be enough: ..."),

and in the subclass's build method, after its own input checks:

    return self.run_build(
        "Build ISFDB Database", "Reading the ISFDB backup…",
        lambda dest, progress, cancelled: build_isfdb_database(backup, dest, progress, cancelled),
        confirm="Build {name} from the ISFDB backup?\\n\\nIt reads the whole backup, ...",
    )

run_build() asks (with "the existing database is replaced only when the
new one is complete" added when there is one), saves the dialog's
settings first so the sources are remembered even if the build is
cancelled, releases any cached open handle on the old file, runs the
build on a worker thread behind a progress dialog whose Cancel really
stops it (gui/dump_import_runner.py: `work` gets `progress(fraction)`
and `cancelled()` callbacks), reports the summary or the error, and
fills in the new path. run_database_build() is the same without the
dialog, for a build started from elsewhere (cbz's menu action).

The status line's check opens the database, which can take a while on
a multi-GB file or a network share, so it runs under run_busy() (off
the GUI thread) and, while the user types a path, only after typing
pauses (`status_debounce_ms`).
"""

from __future__ import annotations

import os
import shutil
from typing import Any, Callable, Optional, Union

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from redactor_common.core.dump_import import DumpImportError
from redactor_common.core.local_db import LocalDatabaseError, forget_cached
from redactor_common.gui.dump_import_runner import run_dump_import
from redactor_common.gui.progress import run_busy
from redactor_common.gui.owned_dialog import OwnedDialog

PathDefault = Union[str, Callable[[], str], None]
BuildWork = Callable[[str, Callable[[float], None], Callable[[], bool]], Any]

STATUS_NO_PATH = "No database yet."
STATUS_MISSING = "Not built yet -- the file doesn't exist."
REPLACING_NOTE = "\n\nThe existing database is replaced only when the new one is complete."


def free_space_text(path: str, warning_bytes: Optional[int] = None, warning_text: str = "") -> str:
    """"Free space where the database goes: N GB." for the drive `path` would
    be on (walking up to the nearest folder that exists), plus `warning_text`
    when that is under `warning_bytes`; "" when it can't be told."""
    folder = os.path.dirname(os.path.abspath(path)) if path else ""
    while folder and not os.path.isdir(folder):
        parent = os.path.dirname(folder)
        if parent == folder:
            return ""
        folder = parent
    if not folder:
        return ""
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        return ""
    text = f"Free space where the database goes: {free / (1 << 30):,.1f} GB."
    if warning_bytes is not None and free < warning_bytes:
        text += warning_text
    return text


def _summary_text(summary: Any) -> str:
    describe = getattr(summary, "describe", None)
    return describe() if callable(describe) else str(summary)


def run_database_build(
    parent: Optional[QWidget],
    title: str,
    label: str,
    work: BuildWork,
    dest: str,
    *,
    confirm: Optional[str] = None,
    confirm_title: str = "Build Database",
    error_types: tuple[type[BaseException], ...] = (),
    before: Optional[Callable[[str], None]] = None,
    show_summary: bool = True,
) -> Optional[str]:
    """Builds `dest` with `work(dest, progress, cancelled)` behind a
    cancellable progress dialog. Returns `dest`, or None when the user
    said no, cancelled, or the build failed (the error was shown).

    confirm: question text first ("{name}" = the file's name, "{dest}" =
        its path); REPLACING_NOTE is appended when `dest` exists, or put
        where "{replacing}" stands in the text (nothing when `dest` is new).
    before(dest): runs after the user agreed, before the build (save the
        settings, so the sources are remembered even if it is cancelled).
    error_types: shown as a warning besides DumpImportError; anything
        else propagates (a bug, not a bad dump).
    show_summary: the result's describe() (or str()) in an information box."""
    if confirm is not None:
        text = confirm.replace("{name}", os.path.basename(dest)).replace("{dest}", dest)
        note = REPLACING_NOTE if os.path.exists(dest) else ""
        if "{replacing}" in text:
            text = text.replace("{replacing}", note)  # the app decides where the note goes
        else:
            text += note
        answer = QMessageBox.question(parent, confirm_title, text)
        if answer != QMessageBox.StandardButton.Yes:
            return None
    if before is not None:
        before(dest)
    forget_cached(dest)  # a lookup may still hold the old build open
    try:
        summary = run_dump_import(parent, title, label, lambda progress, cancelled: work(dest, progress, cancelled))
    except (DumpImportError, *error_types) as exc:
        QMessageBox.warning(parent, title, str(exc))
        return None
    if summary is None:
        return None
    if show_summary:
        QMessageBox.information(parent, title, _summary_text(summary))
    return dest


class LocalDatabaseSettingsDialog(OwnedDialog):
    def __init__(
        self,
        *,
        title: str,
        instructions_html: str,
        path: str,
        check: Callable[[str], str],
        save: Callable[[str], None],
        error_types: tuple[type[BaseException], ...] = (LocalDatabaseError,),
        file_filter: str = "SQLite database (*.db *.sqlite *.sqlite3);;All files (*)",
        build: Optional[Callable[[QDialog], Optional[str]]] = None,
        build_label: str = "Build from Dump…",
        parent=None,
        default_path: PathDefault = None,
        browse_for_destination: bool = False,
        show_status: bool = False,
        free_space_warning: Optional[tuple[int, str]] = None,
        space_label_placement: str = "above",
        status_debounce_ms: int = 400,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(540)
        self._check = check
        self._save = save
        self._error_types = error_types
        self._file_filter = file_filter
        self._default_path = default_path
        self._browse_for_destination_mode = browse_for_destination
        self._free_space_warning = free_space_warning
        outer = QVBoxLayout(self)

        intro = QLabel(instructions_html)
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setOpenExternalLinks(True)
        outer.addWidget(intro)

        row = QHBoxLayout()
        self.path_edit = QLineEdit(path)
        self.path_edit.setPlaceholderText(self.default_path() or "Path to the database file")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.path_edit, 1)
        row.addWidget(browse)
        outer.addLayout(row)

        # Status line, free-space line and app sections (opt-in): between the path row and Check File.
        self.status_label: Optional[QLabel] = None
        self.space_label: Optional[QLabel] = None
        self._sections: Optional[QVBoxLayout] = None
        if show_status:
            self.status_label = QLabel()
            self.status_label.setWordWrap(True)
            outer.addWidget(self.status_label)
        if free_space_warning is not None:
            self.space_label = QLabel()
            self.space_label.setWordWrap(True)
            if space_label_placement == "above":
                outer.addWidget(self.space_label)
            # "manual": the app puts `space_label` where it wants (inside its own section, via add_section()
            # or its own layout); the dialog only keeps its text current.

        self._actions = QHBoxLayout()
        check_button = QPushButton("Check File")
        check_button.clicked.connect(self._run_check)
        self._actions.addWidget(check_button)
        self._build = build
        self.build_button: Optional[QPushButton] = None
        if build is not None:
            self.build_button = QPushButton(build_label)
            self.build_button.clicked.connect(self._run_build)
            self._actions.addWidget(self.build_button)
        self._actions.addStretch(1)
        outer.addLayout(self._actions)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        self._status_timer: Optional[QTimer] = None
        if show_status or free_space_warning is not None:
            self._status_timer = QTimer(self)
            self._status_timer.setSingleShot(True)
            self._status_timer.setInterval(max(0, status_debounce_ms))
            self._status_timer.timeout.connect(self.refresh_status)
            self.path_edit.textChanged.connect(self._status_timer.start)
            self.refresh_status()

    # -- layout --------------------------------------------------------------------

    def add_section(self, widget: QWidget) -> None:
        """Adds an app's own widget (a "Build from ..." group box) below the
        status lines and above the Check File / Build row."""
        if self._sections is None:
            self._sections = QVBoxLayout()
            self._sections.setContentsMargins(0, 0, 0, 0)
            outer = self.layout()
            index = next(i for i in range(outer.count()) if outer.itemAt(i).layout() is self._actions)
            outer.insertLayout(index, self._sections)
        self._sections.addWidget(widget)

    # -- paths ---------------------------------------------------------------------

    def default_path(self) -> str:
        """Where a new database is suggested to go ("" when the app gave none)."""
        source = self._default_path
        value = source() if callable(source) else source
        return os.fspath(value) if value else ""

    def destination(self) -> str:
        """The typed path, or the default when the field is empty."""
        return self.path_edit.text().strip() or self.default_path()

    def _browse(self) -> None:
        if self._browse_for_destination_mode:
            self._browse_for_destination()
            return
        path, _ = QFileDialog.getOpenFileName(self, "Choose the database", self.path_edit.text(), self._file_filter)
        if path:
            self.path_edit.setText(path)

    def _browse_for_destination(self) -> None:
        """The database may not exist yet, so this is a Save dialog that doesn't
        ask to overwrite (picking an existing file just selects it)."""
        path, _ = QFileDialog.getSaveFileName(
            self, "Choose where the database is (or will be) stored",
            self.destination(), self._file_filter, options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if path:
            self.path_edit.setText(path)

    # -- status ----------------------------------------------------------------------

    def free_space_text(self) -> str:
        if self._free_space_warning is None:
            return free_space_text(self.destination())
        warning_bytes, warning_text = self._free_space_warning
        return free_space_text(self.destination(), warning_bytes, warning_text)

    def status_text(self) -> str:
        """What the status line says for the path now typed. The check (for an
        existing file) runs under run_busy, off the GUI thread."""
        path = self.path_edit.text().strip()
        if not path:
            return STATUS_NO_PATH
        if not os.path.isfile(path):
            return STATUS_MISSING
        ok, message = run_busy(self, "Checking the database...", self.check_result, path, title="Check File")
        return message if ok else f"Problem: {message}"

    def refresh_status(self) -> None:
        """Updates the status and free-space lines (no-op for the ones not shown)."""
        if self._status_timer is not None:
            self._status_timer.stop()
        if self.status_label is not None:
            self.status_label.setText(self.status_text())
        if self.space_label is not None:
            self.space_label.setText(self.free_space_text())

    # -- check / build ---------------------------------------------------------------

    def _run_build(self) -> None:
        path = self._build(self)
        if path:
            self.path_edit.setText(path)

    def run_build(
        self,
        title: str,
        label: str,
        work: BuildWork,
        *,
        dest: Optional[str] = None,
        confirm: Optional[str] = None,
        confirm_title: str = "Build Database",
        error_types: tuple[type[BaseException], ...] = (),
        save_first: bool = True,
        show_summary: bool = True,
    ) -> Optional[str]:
        """The shared build tail (see the module docstring). `dest` defaults to
        destination(). save_first: the dialog's `save` runs with `dest` once
        the user agreed, before the build. On success the path field gets
        `dest` and the status refreshes; returns `dest`, or None."""
        dest = dest or self.destination()
        if not dest:
            QMessageBox.information(self, confirm_title, "Choose where the database is to be stored first.")
            return None
        built = run_database_build(
            self, title, label, work, dest,
            confirm=confirm, confirm_title=confirm_title, error_types=error_types,
            before=self._save if save_first else None, show_summary=show_summary,
        )
        if built:
            self.path_edit.setText(built)
            self.refresh_status()
        return built

    def check_result(self, path: str | None = None) -> tuple[bool, str]:
        """(ok, message) for `path` (default: the path now typed) -- what Check File shows. The check itself
        runs off the GUI thread from _run_check(), so it only reads `path`, never a widget."""
        if path is None:
            path = self.path_edit.text()
        try:
            return True, self._check(path.strip())
        except self._error_types as exc:
            return False, str(exc)

    def _run_check(self) -> None:
        # Opening a big database file can take a while (a 5 GB SQLite file, a network share): run it under a
        # progress dialog on a worker thread, not on the GUI thread. The `check` callable must not touch widgets.
        path = self.path_edit.text()
        ok, message = run_busy(self, "Checking the database...", self.check_result, path, title="Check File")
        (QMessageBox.information if ok else QMessageBox.warning)(self, "Check File", message)

    def accept(self) -> None:
        self._save(self.path_edit.text().strip())
        super().accept()

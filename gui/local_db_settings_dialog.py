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
"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import Qt
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
)

from redactor_common.core.local_db import LocalDatabaseError
from redactor_common.gui.progress import run_busy


class LocalDatabaseSettingsDialog(QDialog):
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
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(540)
        self._check = check
        self._save = save
        self._error_types = error_types
        self._file_filter = file_filter
        outer = QVBoxLayout(self)

        intro = QLabel(instructions_html)
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setOpenExternalLinks(True)
        outer.addWidget(intro)

        row = QHBoxLayout()
        self.path_edit = QLineEdit(path)
        self.path_edit.setPlaceholderText("Path to the database file")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.path_edit, 1)
        row.addWidget(browse)
        outer.addLayout(row)

        actions = QHBoxLayout()
        check_button = QPushButton("Check File")
        check_button.clicked.connect(self._run_check)
        actions.addWidget(check_button)
        self._build = build
        self.build_button: Optional[QPushButton] = None
        if build is not None:
            self.build_button = QPushButton(build_label)
            self.build_button.clicked.connect(self._run_build)
            actions.addWidget(self.build_button)
        actions.addStretch(1)
        outer.addLayout(actions)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose the database", self.path_edit.text(), self._file_filter)
        if path:
            self.path_edit.setText(path)

    def _run_build(self) -> None:
        path = self._build(self)
        if path:
            self.path_edit.setText(path)

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

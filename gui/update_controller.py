"""
redactor_common/gui/update_controller.py

Help > Check for Updates, and the optional once-a-day check at startup, for every app of the family. The logic
(GitHub's release API, the verified download, the rename swap) is core/selfupdate.py; this is the dialogs.

Wiring, in a MainWindow.__init__ (before the menus are built):

    self.updates = UpdateController(self, APP_NAME, APP_VERSION, APP_REPO_URL, updates_json_path)
    help=standard_help_items(APP_NAME, ..., updates_slot=self.updates.check_now)
    ...
    self.updates.start_auto_check()        # does nothing unless switched on in Preferences

and in the app's Preferences: add selfupdate.updates_section() to the pages and wrap the backend with
selfupdate.with_update_settings(backend, UpdateSettings(updates_json_path)).

The manual check runs under run_busy (the window never looks frozen). The automatic one runs on a plain thread with no
dialog at all and says nothing unless there is a version to offer that the user has not skipped; a failure there is
silent (it is retried the next day). The download runs under a counted progress dialog with a working Cancel.

After the swap the user chooses Restart Now (the window is closed the normal way, so unsaved changes are asked about,
and the new exe is started once the app has really quit) or Later (the new version starts next time).
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QApplication, QDialogButtonBox, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget

from redactor_common.core import selfupdate
from redactor_common.core.selfupdate import Release, UpdateError, UpdateSettings
from redactor_common.gui.about_dialog import _markdown_browser
from redactor_common.gui.owned_dialog import OwnedDialog
from redactor_common.gui.progress import run_busy, run_work_with_progress

AUTO_CHECK_DELAY_MS = 5000      # let the window settle (and a folder load start) before asking GitHub

# UpdateDialog.choice
UPDATE = "update"
OPEN_PAGE = "open_page"
SKIP = "skip"
LATER = "later"


class UpdateDialog(OwnedDialog):
    """'Version X is available': the release notes, and Update Now (or Open Download Page when this copy cannot
    replace itself), Skip This Version, Not Now. The answer is in `choice` after exec()."""

    def __init__(self, app_name: str, current: str, release: Release, can_update: bool, reason: str = "",
                 parent: QWidget | None = None, automatic: bool = False):
        super().__init__(parent)
        self.choice = LATER
        self.setWindowTitle("Update Available")
        self.resize(600, 520)
        layout = QVBoxLayout(self)
        intro = QLabel(f"<b>{app_name} {release.display}</b> is available. You have {current}.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        if not can_update and reason:
            why = QLabel(reason)
            why.setWordWrap(True)
            layout.addWidget(why)
        layout.addWidget(_markdown_browser(release.notes or "*(no release notes)*"), 1)
        if automatic:
            hint = QLabel("The daily check can be switched off in Preferences.")
            hint.setWordWrap(True)
            layout.addWidget(hint)

        buttons = QDialogButtonBox()
        if can_update:
            main = buttons.addButton("&Update Now", QDialogButtonBox.ButtonRole.AcceptRole)
            main.clicked.connect(lambda: self._choose(UPDATE))
        else:
            main = buttons.addButton("&Open Download Page", QDialogButtonBox.ButtonRole.AcceptRole)
            main.clicked.connect(lambda: self._choose(OPEN_PAGE))
            main.setEnabled(bool(release.page_url))
        main.setDefault(True)
        skip = buttons.addButton("&Skip This Version", QDialogButtonBox.ButtonRole.DestructiveRole)
        skip.clicked.connect(lambda: self._choose(SKIP))
        later = buttons.addButton("&Not Now", QDialogButtonBox.ButtonRole.RejectRole)
        later.clicked.connect(lambda: self._choose(LATER))
        self.main_button: QPushButton = main
        layout.addWidget(buttons)

    def _choose(self, choice: str) -> None:
        self.choice = choice
        self.accept()


class _AutoResult(QObject):
    found = pyqtSignal(object)      # a Release, emitted from the worker thread, delivered on the GUI thread


class UpdateController(QObject):
    def __init__(self, window: QWidget, app_name: str, app_version: str, repo_url: str,
                 settings_path: str | Path, *, fetch=None, platform: str = sys.platform,
                 frozen: bool | None = None, exe: Path | None = None):
        super().__init__(window)
        self.window = window
        self.app_name = app_name
        self.app_version = app_version
        self.repo = selfupdate.repo_from_url(repo_url)
        self.slug = self.repo.split("/", 1)[1].lower()
        self.settings = UpdateSettings(settings_path)
        self._fetch = fetch
        self._platform = platform
        self._frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
        self._exe = Path(exe) if exe else selfupdate.running_exe()
        self._auto = _AutoResult(self)
        self._auto.found.connect(self._on_auto_found)
        self._restart_connected = False
        self.restart_pending = False

    # --- checking --------------------------------------------------------------------------------------------------

    def _latest(self) -> Release:
        return selfupdate.fetch_latest(self.repo, self.slug, self._fetch, self._platform)

    def check_now(self) -> None:
        """Help > Check for Updates."""
        try:
            release = run_busy(self.window, "Checking for a new version...", self._latest, title="Check for Updates")
        except UpdateError as exc:
            QMessageBox.warning(self.window, "Check for Updates", str(exc))
            return
        self.settings.update(last_check=time.time())
        if not selfupdate.is_newer(release, self.app_version):
            QMessageBox.information(self.window, "Check for Updates",
                                    f"You have the newest version of {self.app_name} ({self.app_version}).")
            return
        self.offer(release)

    def start_auto_check(self, delay_ms: int = AUTO_CHECK_DELAY_MS) -> bool:
        """Starts the daily check (after `delay_ms`, on a thread) when it is switched on and due. True if started."""
        if not self.settings.due():
            return False
        QTimer.singleShot(delay_ms, self._start_auto_thread)
        return True

    def _start_auto_thread(self) -> None:
        threading.Thread(target=self._auto_work, name="update-check", daemon=True).start()

    def _auto_work(self) -> None:
        try:
            release = self._latest()
        except Exception:  # noqa: BLE001 - the automatic check is silent; it runs again tomorrow
            return
        self.settings.update(last_check=time.time())
        if selfupdate.is_newer(release, self.app_version) and release.tag != self.settings.skipped:
            self._auto.found.emit(release)

    def _on_auto_found(self, release: Release) -> None:
        if QApplication.activeModalWidget() is not None:
            # Something modal is open (a progress dialog, a lookup): ask again when it has closed.
            QTimer.singleShot(AUTO_CHECK_DELAY_MS, lambda: self._on_auto_found(release))
            return
        self.offer(release, automatic=True)

    # --- offering and installing -----------------------------------------------------------------------------------

    def offer(self, release: Release, automatic: bool = False) -> str:
        """Shows the Update Available dialog and acts on the answer. Returns the choice."""
        can, reason = selfupdate.can_self_replace(self._exe, self._platform, self._frozen)
        if can and not release.downloadable:
            can, reason = False, "This release has no verifiable download for this system; use the download page."
        dialog = UpdateDialog(self.app_name, self.app_version, release, can, reason, self.window, automatic)
        dialog.exec()
        choice = dialog.choice
        if choice == SKIP:
            self.settings.update(skipped=release.tag)
        elif choice == OPEN_PAGE and release.page_url:
            QDesktopServices.openUrl(QUrl(release.page_url))
        elif choice == UPDATE:
            self.update_to(release)
        return choice

    def update_to(self, release: Release) -> bool:
        """Downloads, verifies and installs `release`, then offers the restart. True when installed."""
        new_file = self._exe.with_name(self._exe.name + selfupdate.NEW_SUFFIX)

        def work(progress, cancel):
            return selfupdate.download(release, new_file, progress, cancel)

        try:
            got = run_work_with_progress(self.window, f"Downloading {self.app_name} {release.display}...", work,
                                         release.asset_size, title="Update")
            if got is None:
                return False            # cancelled; the partial file is already gone
            selfupdate.install(got, self._exe)
        except UpdateError as exc:
            QMessageBox.warning(self.window, "Update", str(exc))
            return False
        answer = QMessageBox.question(
            self.window, "Update",
            f"{self.app_name} {release.display} is installed. It starts the next time the program is opened.\n\n"
            "Restart now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.restart()
        return True

    def restart(self) -> None:
        """Closes the main window the normal way (it may ask about unsaved changes and stay open) and, once the app
        has quit, starts the new exe."""
        app = QApplication.instance()
        if not self._restart_connected:
            app.aboutToQuit.connect(self._start_new_exe)
            self._restart_connected = True
        self.restart_pending = True
        if self.window.close():
            app.quit()
        else:
            self.restart_pending = False    # the user kept the window open; the new version starts next time

    def _start_new_exe(self) -> None:
        if not self.restart_pending:
            return
        argv, env = selfupdate.restart_command(self._exe)
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(argv, env=env, cwd=str(self._exe.parent), creationflags=flags, close_fds=True)
        except OSError:
            pass                            # the update is installed either way; it starts next time

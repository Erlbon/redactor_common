"""
redactor_common/gui/update_controller.py

Help > Check for Updates, and the optional once-a-day check at startup, for every app of the family. The logic
(GitHub's release API, the manifest, the declared channel, the verified download, the exe swap) is core/selfupdate.py
and core/update_manifest.py; this is the dialogs. See docs/update-framework.md.

Wiring, in a MainWindow.__init__ (before the menus are built):

    self.updates = UpdateController(self, APP_NAME, APP_VERSION, APP_REPO_URL, updates_json_path)
    help=standard_help_items(APP_NAME, ..., updates_slot=self.updates.check_now)
    ...
    self.updates.start_auto_check()        # does nothing unless switched on in Preferences

and in the app's Preferences: add selfupdate.updates_section() to the pages and wrap the backend with
selfupdate.with_update_settings(backend, UpdateSettings(updates_json_path)).

An app whose window does not end the program when closed (Accipio hides to the tray) passes `quit_app`: the callable
its File > Exit runs, returning False when the user kept the program open.

The manual check runs under run_busy (the window never looks frozen). The automatic one runs on a plain thread with no
dialog at all and says nothing unless there is a version to offer that the user has not skipped; a failure there is
silent (it is retried the next day). The download runs under a counted progress dialog with a working Cancel.

Whatever this copy cannot do is said in the Update Available box, with the reason and a Copy Details button; the
download page is offered next to the reason, never instead of one.

windows-exe: after the swap the user chooses Restart Now (the window is closed the normal way, so unsaved changes are
asked about, and the new exe is started once the app has really quit) or Later (the new version starts next time).
windows-installer: the new installer is downloaded and checked, then the app quits and the installer, started once it
has, updates the folder and starts the program again.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QApplication, QDialogButtonBox, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from redactor_common.core import selfupdate
from redactor_common.core.selfupdate import Decision, Release, UpdateError, UpdateSettings
from redactor_common.core.update_manifest import WINDOWS_EXE, WINDOWS_INSTALLER
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
    """'Version X is available': the release notes, and Update Now (or, when this copy cannot replace itself, the
    reason, Open Download Page and Copy Details), Skip This Version, Not Now. The answer is in `choice` after exec()."""

    def __init__(self, app_name: str, current: str, release: Release, can_update: bool, reason: str = "",
                 parent: QWidget | None = None, automatic: bool = False, switch_place: str = "Preferences",
                 details: str = ""):
        super().__init__(parent)
        self.choice = LATER
        self.setWindowTitle("Update Available")
        self.resize(600, 520)
        layout = QVBoxLayout(self)
        intro = QLabel(f"<b>{app_name} {release.display}</b> is available. You have {current}.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.reason_label: QLabel | None = None
        if not can_update:
            why = QLabel(f"<b>This copy cannot update itself:</b> {reason or 'no reason was given.'}")
            why.setWordWrap(True)
            layout.addWidget(why)
            self.reason_label = why
        layout.addWidget(_markdown_browser(release.notes or "*(no release notes)*"), 1)
        if automatic:
            hint = QLabel(f"The daily check can be switched off in {switch_place}.")
            hint.setWordWrap(True)
            layout.addWidget(hint)

        buttons = QDialogButtonBox()
        self.copy_button: QPushButton | None = None
        if can_update:
            main = buttons.addButton("&Update Now", QDialogButtonBox.ButtonRole.AcceptRole)
            main.clicked.connect(lambda: self._choose(UPDATE))
        else:
            main = buttons.addButton("&Open Download Page", QDialogButtonBox.ButtonRole.AcceptRole)
            main.clicked.connect(lambda: self._choose(OPEN_PAGE))
            main.setEnabled(bool(release.page_url))
            copy = buttons.addButton("&Copy Details", QDialogButtonBox.ButtonRole.HelpRole)
            text = details or reason
            copy.clicked.connect(lambda: QApplication.clipboard().setText(text))
            self.copy_button = copy
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
    found = pyqtSignal(object)      # a Decision, emitted from the worker thread, delivered on the GUI thread


class UpdateController(QObject):
    def __init__(self, window: QWidget, app_name: str, app_version: str, repo_url: str,
                 settings_path: str | Path, *, fetch=None, frozen: bool | None = None, exe: Path | None = None,
                 switch_place: str = "Preferences", channel: str | None = None,
                 quit_app: Callable[[], bool] | None = None, launch: Callable[..., object] | None = None):
        """`switch_place`: where this app keeps the daily-check switch, as the update dialog names it
        (Dossier: "the Help menu"). `channel`: this copy's channel when known better than its update-channel file
        (tests). `quit_app`: how the app quits the way File > Exit does (default: close the window, then quit).
        `launch`: starts the new exe or installer once the app has quit (tests)."""
        super().__init__(window)
        self.window = window
        self.switch_place = switch_place
        self.app_name = app_name
        self.app_version = app_version
        self.repo = selfupdate.repo_from_url(repo_url)
        self.slug = self.repo.split("/", 1)[1].lower()
        self.settings = UpdateSettings(settings_path)
        self._fetch = fetch
        self._frozen = selfupdate.is_frozen() if frozen is None else frozen
        self._exe = Path(exe) if exe else selfupdate.running_exe()
        self.channel = selfupdate.channel_of(self._exe, self._frozen) if channel is None else channel
        self._quit_app = quit_app or self._close_and_quit
        self._launch = launch or _launch_detached
        self._pending_command: list[str] | None = None
        self._auto = _AutoResult(self)
        self._auto.found.connect(self._on_auto_found)
        self._restart_connected = False
        self.restart_pending = False

    # --- checking --------------------------------------------------------------------------------------------------

    def decide(self, tag: str = "") -> Decision:
        return selfupdate.decide(self.repo, self.slug, self.app_version, self.channel, self._fetch, tag,
                                 writable=lambda: selfupdate.can_self_replace(self._exe))

    def check_now(self) -> None:
        """Help > Check for Updates."""
        try:
            decision = run_busy(self.window, "Checking for a new version...", self.decide, title="Check for Updates")
        except UpdateError as exc:
            QMessageBox.warning(self.window, "Check for Updates", str(exc))
            return
        self.settings.update(last_check=time.time())
        if decision.action == selfupdate.NONE:
            QMessageBox.information(self.window, "Check for Updates",
                                    f"You have the newest version of {self.app_name} ({self.app_version}).")
            return
        self.offer(decision)

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
            decision = self.decide()
        except Exception:  # noqa: BLE001 - the automatic check is silent; it runs again tomorrow
            return
        self.settings.update(last_check=time.time())
        if decision.action != selfupdate.NONE and decision.release.tag != self.settings.skipped:
            self._auto.found.emit(decision)

    def _on_auto_found(self, decision: Decision) -> None:
        if QApplication.activeModalWidget() is not None:
            # Something modal is open (a progress dialog, a lookup): ask again when it has closed.
            QTimer.singleShot(AUTO_CHECK_DELAY_MS, lambda: self._on_auto_found(decision))
            return
        self.offer(decision, automatic=True)

    # --- offering and installing -----------------------------------------------------------------------------------

    def offer(self, decision: Decision, automatic: bool = False) -> str:
        """Shows the Update Available dialog and acts on the answer. Returns the choice."""
        release = decision.release
        dialog = UpdateDialog(self.app_name, self.app_version, release, decision.action == selfupdate.INSTALL,
                              decision.reason, self.window, automatic, self.switch_place,
                              details=decision.details(self.app_name))
        dialog.exec()
        choice = dialog.choice
        if choice == SKIP:
            self.settings.update(skipped=release.tag)
        elif choice == OPEN_PAGE and release.page_url:
            QDesktopServices.openUrl(QUrl(release.page_url))
        elif choice == UPDATE:
            self.update_to(decision)
        return choice

    def update_to(self, decision: Decision) -> bool:
        """Downloads, verifies and installs what `decision` names. True when installed (exe) or handed to the
        installer (which runs once the app has quit)."""
        if decision.action != selfupdate.INSTALL:
            return False
        if decision.channel == WINDOWS_INSTALLER:
            return self._update_by_installer(decision.release)
        if decision.channel != WINDOWS_EXE:
            return False
        release = decision.release
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

    def _update_by_installer(self, release: Release) -> bool:
        """An installed copy: download the new installer (checked like any update), then quit and let it run; it
        starts the program again when it is done. True when the installer will start (at quit)."""
        setup = selfupdate.installer_download_path(release)

        def work(progress, cancel):
            return selfupdate.download(release, setup, progress, cancel)

        try:
            got = run_work_with_progress(self.window, f"Downloading {self.app_name} {release.display}...", work,
                                         release.asset_size, title="Update")
        except UpdateError as exc:
            QMessageBox.warning(self.window, "Update", str(exc))
            return False
        if got is None:
            return False
        answer = QMessageBox.question(
            self.window, "Update",
            f"{self.app_name} {release.display} is ready to install. {self.app_name} closes, the update installs, and "
            f"{self.app_name} starts again.\n\nUpdate now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            try:
                got.unlink()
            except OSError:
                pass
            return False
        return self.restart(selfupdate.installer_command(got))

    def restart(self, command: list[str] | None = None) -> bool:
        """Quits the app the normal way (it may ask about unsaved changes and stay open) and, once it has quit,
        starts the new exe (or `command`: the new installer of an installed copy). False when the app stayed open."""
        self._pending_command = command
        app = QApplication.instance()
        if not self._restart_connected:
            app.aboutToQuit.connect(self._start_new_exe)
            self._restart_connected = True
        self.restart_pending = True
        if not self._quit_app():
            self.restart_pending = False    # the user kept the program open; the new version starts next time
        return self.restart_pending

    def _close_and_quit(self) -> bool:
        if not self.window.close():
            return False
        QApplication.instance().quit()
        return True

    def _start_new_exe(self) -> None:
        if not self.restart_pending:
            return
        argv, env = selfupdate.restart_command(self._exe)
        if self._pending_command:
            argv = self._pending_command
        try:
            self._launch(argv, env=env, cwd=str(self._exe.parent))
        except OSError:
            pass                            # the update is installed (exe) or still downloadable; it starts next time


def _launch_detached(argv: list[str], env: dict, cwd: str) -> None:
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(argv, env=env, cwd=cwd, creationflags=flags, close_fds=True)

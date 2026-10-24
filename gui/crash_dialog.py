"""
redactor_common/gui/crash_dialog.py

The visible error box for unhandled exceptions, shared by all four Redactor apps. Registered once by
app_bootstrap.run_app() as the `also_call` of core.crash_log.install(), which still writes the crash log first.

* ONE dialog at a time ("Something went wrong"): while it is open, further errors only raise its counter and are
  added to its Details; the same traceback again within RATE_LIMIT_SECONDS of the last one is only counted.
* The short message names what stopped ("Save All stopped because of an unexpected error: <text>" when the
  caller gave a context), the full traceback sits in a collapsible Details area, and the buttons are
  [Copy Details] [Open Crash Log Folder] [Close], with the crash log's path shown.
* Works from worker threads: the error is formatted on the calling thread and handed to the GUI thread through a
  queued signal.
* Never makes things worse: anything that goes wrong while the dialog is built or shown falls back to the log
  alone. KeyboardInterrupt, SystemExit and GeneratorExit are not errors and show nothing.

Usage:
    reporter = crash_dialog.install(log_path)              # returns the object; its .callback is the also_call
    crash_log.install(log_path, also_call=reporter.callback)
    ...
    crash_dialog.report_exception(exc, "Save All stopped because of an unexpected error")  # a caught one
"""

from __future__ import annotations

import time
import traceback
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QToolButton,
    QVBoxLayout,
)

from redactor_common.core import crash_log
from redactor_common.core.os_utils import open_with_default_app
from redactor_common.gui.owned_dialog import OwnedDialog

DIALOG_TITLE = "Something went wrong"
RATE_LIMIT_SECONDS = 5.0
GENERIC_CONTEXT = "Something went wrong that this app didn't expect"
_NOT_ERRORS = (KeyboardInterrupt, SystemExit, GeneratorExit)


def summary_for(exc_value: BaseException, context: str = "") -> str:
    """The one-line message: '<context>: <exception text>'."""
    text = "".join(traceback.format_exception_only(type(exc_value), exc_value)).strip()
    return f"{context or GENERIC_CONTEXT}: {text}"


def _signature(exc_type, exc_value, exc_tb) -> str:
    """Identical tracebacks share one key: the type, the last frame (file and line) and the message."""
    frames = traceback.extract_tb(exc_tb) if exc_tb is not None else []
    last = frames[-1] if frames else None
    where = f"{last.filename}:{last.lineno}" if last else ""
    return f"{getattr(exc_type, '__name__', exc_type)}|{where}|{exc_value}"


class CrashDialog(OwnedDialog):
    """The box itself: message, counter, collapsible traceback, crash log path, three buttons."""

    def __init__(self, summary: str, details: str, log_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(DIALOG_TITLE)
        self.setModal(False)
        self.resize(640, 220)
        self._log_path = str(log_path)
        self._count = 1
        layout = QVBoxLayout(self)
        self.summary_label = QLabel(summary)
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary_label)
        self.count_label = QLabel("")
        self.count_label.setVisible(False)
        layout.addWidget(self.count_label)
        self.details_toggle = QToolButton()
        self.details_toggle.setText("Details")
        self.details_toggle.setCheckable(True)
        self.details_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.details_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.details_toggle.toggled.connect(self._on_details_toggled)
        layout.addWidget(self.details_toggle)
        self.details_edit = QPlainTextEdit(details)
        self.details_edit.setReadOnly(True)
        self.details_edit.setVisible(False)
        layout.addWidget(self.details_edit, 1)
        self.log_label = QLabel(f"Crash log: {self._log_path}")
        self.log_label.setWordWrap(True)
        self.log_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.log_label)
        row = QHBoxLayout()
        self.copy_button = QPushButton("Copy Details")
        self.copy_button.clicked.connect(self.copy_details)
        self.open_button = QPushButton("Open Crash Log Folder")
        self.open_button.clicked.connect(self.open_log_folder)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        row.addWidget(self.copy_button)
        row.addWidget(self.open_button)
        row.addStretch(1)
        row.addWidget(buttons)
        layout.addLayout(row)

    @property
    def count(self) -> int:
        return self._count

    def add_error(self, summary: str, details: str) -> None:
        """Another error while the box is open: count it and add it to Details (the box does not stack)."""
        self._count += 1
        self.count_label.setText(f"{self._count - 1} more error(s) since this box opened; the latest: {summary}")
        self.count_label.setWordWrap(True)
        self.count_label.setVisible(True)
        self.details_edit.appendPlainText("\n" + "=" * 60 + "\n" + details)

    def _on_details_toggled(self, expanded: bool) -> None:
        self.details_toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.details_edit.setVisible(expanded)
        self.resize(self.width(), 460 if expanded else 220)

    def details_text(self) -> str:
        return self.details_edit.toPlainText()

    def copy_details(self) -> None:
        QApplication.clipboard().setText(f"{self.summary_label.text()}\n\n{self.details_text()}")

    def open_log_folder(self) -> None:
        folder = Path(self._log_path).parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        open_with_default_app(str(folder))


class CrashReporter(QObject):
    """Receives errors from any thread and shows at most one CrashDialog on the GUI thread."""

    _incoming = pyqtSignal(str, str, str)  # signature, summary, details (queued when sent from another thread)

    def __init__(self, log_path: str | Path):
        super().__init__()
        self.log_path = str(log_path)
        self.dialog: CrashDialog | None = None
        self.dialogs_opened = 0
        self.suppressed = 0  # identical errors seen again within the rate limit
        self._last_signature = ""
        self._last_time = 0.0
        app = QApplication.instance()
        if app is not None and QThread.currentThread() is not app.thread():
            self.moveToThread(app.thread())
        self._incoming.connect(self._on_incoming)

    # -- the ExceptionCallback of core.crash_log.install ------------------------------------------------------

    def callback(self, exc_type, exc_value, exc_tb) -> None:
        self.report(exc_type, exc_value, exc_tb)

    def report(self, exc_type, exc_value, exc_tb, context: str = "") -> None:
        """Never raises. Safe from any thread."""
        try:
            if issubclass(exc_type, _NOT_ERRORS):
                return
            details = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            self._incoming.emit(_signature(exc_type, exc_value, exc_tb), summary_for(exc_value, context), details)
        except Exception:  # noqa: BLE001 - the error box must never become a second error
            pass

    # -- GUI thread -------------------------------------------------------------------------------------------------

    def _on_incoming(self, signature: str, summary: str, details: str) -> None:
        try:
            if self.dialog is not None:
                self.dialog.add_error(summary, details)
                return
            now = time.monotonic()
            if signature == self._last_signature and now - self._last_time < RATE_LIMIT_SECONDS:
                self.suppressed += 1
                self._last_time = now
                return
            self._last_signature, self._last_time = signature, now
            dialog = CrashDialog(summary, details, self.log_path)
            dialog.finished.connect(self._on_dialog_closed)
            self.dialog = dialog
            self.dialogs_opened += 1
            dialog.show()
        except Exception:  # noqa: BLE001 - fall back to the log alone (crash_log has already written the entry)
            self.dialog = None

    def _on_dialog_closed(self, _result: int = 0) -> None:
        self._last_time = time.monotonic()
        self.dialog = None


_reporter: CrashReporter | None = None


def install(log_path: str | Path) -> CrashReporter:
    """Creates (or re-points) the process-wide reporter; pass `.callback` to crash_log.install(also_call=...)."""
    global _reporter
    if _reporter is None:
        _reporter = CrashReporter(log_path)
    else:
        _reporter.log_path = str(log_path)
    return _reporter


def get_reporter() -> CrashReporter | None:
    return _reporter


def report_exception(exc: BaseException, context: str = "") -> None:
    """For an exception the caller caught itself (e.g. around Save All): writes the crash-log entry like an
    unhandled one and shows the box. Never raises."""
    try:
        reporter = _reporter
        if reporter is None:
            return
        crash_log.write_crash_entry(type(exc), exc, exc.__traceback__, reporter.log_path)
        reporter.report(type(exc), exc, exc.__traceback__, context)
    except Exception:  # noqa: BLE001
        pass


def callback_for(log_path: str | Path) -> Callable:
    """What app_bootstrap registers: the reporter's callback, created lazily on first use."""
    return install(log_path).callback

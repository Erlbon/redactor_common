"""
redactor_common/gui/app_bootstrap.py

The shared startup sequence every Redactor app's main.py was repeating
(cbz and epub near-identically, mp3 and video each missing parts):

  1. crash logging (redactor_common.core.crash_log), installed before
     QApplication exists, with a friendly "Unexpected Error" dialog on
     top of the log entry;
  2. an explicit Windows AppUserModelID, so running from python.exe
     shows the app's own taskbar icon instead of Python's;
  3. QApplication + application name + window icon;
  4. apply_theme() and apply_message_box_style().

Usage from a project's main.py:

    from redactor_common.gui.app_bootstrap import run_app
    sys.exit(run_app(
        app_name=APP_NAME,
        app_user_model_id="Erlbon.CbzRedactor.GUI.1",
        crash_log_path=crash_log.log_path(),
        icon_path=resource_path("assets", "icon.ico"),
        window_factory=MainWindow,
    ))
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path
from typing import Callable

from redactor_common.core import crash_log


def set_windows_app_user_model_id(app_user_model_id: str) -> None:
    """Windows-only; a harmless no-op elsewhere or if the API is missing."""
    if sys.platform != "win32" or not app_user_model_id:
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_user_model_id)
    except (AttributeError, OSError):
        pass


def make_crash_dialog(log_path: str | Path) -> crash_log.ExceptionCallback:
    """Returns an `also_call` for crash_log.install() that shows a
    best-effort "Unexpected Error" message box naming the log file. Does
    nothing if no QApplication exists yet (a crash during the earliest
    startup is still logged either way)."""

    def _show(exc_type, exc_value, exc_tb) -> None:
        from PyQt6.QtWidgets import QApplication, QMessageBox

        if QApplication.instance() is None:
            return
        summary = "".join(traceback.format_exception_only(exc_type, exc_value)).strip()
        QMessageBox.critical(
            None,
            "Unexpected Error",
            "Something went wrong that this app didn't expect.\n\n"
            f"{summary}\n\n"
            f"Details have been saved to {log_path} -- "
            "that file may help track down what happened.",
        )

    return _show


def run_app(
    app_name: str,
    window_factory: Callable[[], object],
    crash_log_path: str | Path | None = None,
    app_user_model_id: str = "",
    icon_path: str | Path | None = None,
    argv: list[str] | None = None,
) -> int:
    """Runs the whole startup sequence and the Qt event loop; returns
    the exit code for sys.exit(). `window_factory` is called after the
    QApplication and theme exist (pass the MainWindow class itself)."""
    if crash_log_path is not None:
        crash_log.install(crash_log_path, also_call=make_crash_dialog(crash_log_path))
    set_windows_app_user_model_id(app_user_model_id)

    from PyQt6.QtGui import QIcon
    from PyQt6.QtWidgets import QApplication

    from redactor_common.gui.qmessagebox_style import apply_message_box_style
    from redactor_common.gui.theme import apply_theme

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(app_name)
    apply_theme(app)  # Fusion + a WCAG-contrast-verified light/dark palette
    apply_message_box_style(app)  # long unwrappable lines stay under 480px wide
    if icon_path and os.path.exists(icon_path):
        app.setWindowIcon(QIcon(str(icon_path)))

    window = window_factory()
    window.show()
    return app.exec()

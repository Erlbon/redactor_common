"""gui/work_progress.py (promoted from mp3redactor) and LocalDatabaseSettingsDialog's Check File running off the GUI thread."""

import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox, QProgressDialog, QWidget  # noqa: E402

from redactor_common.gui.work_progress import WorkProgress, run_work_with_progress  # noqa: E402

_app = QApplication.instance() or QApplication([])


def test_work_runs_off_the_gui_thread_and_the_event_loop_keeps_going():
    window = QWidget()
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(10)
    seen = {}

    def work(progress):
        seen["thread"] = threading.current_thread()
        threading.Event().wait(0.4)  # a "long ffmpeg run"
        progress.on_progress(1)
        return "done"

    result, cancelled = run_work_with_progress(window, 1, "Working...", work)
    timer.stop()
    assert result == "done" and not cancelled
    assert seen["thread"] is not threading.main_thread()
    assert len(ticks) >= 5  # the GUI thread kept handling events while the work ran


def test_cancel_reaches_the_work_and_errors_are_reraised():
    window = QWidget()
    shown = []

    def press_cancel():
        for dialog in window.findChildren(QProgressDialog):
            if dialog.isVisible():
                dialog.canceled.emit()  # what the Cancel button sends
                shown.append(dialog.labelText())

    QTimer.singleShot(500, press_cancel)  # after the dialog has appeared (SHOW_AFTER_MS)

    def work(progress):
        for _ in range(500):
            if progress.should_cancel():
                return "stopped"
            threading.Event().wait(0.01)
        return "ran to the end"

    assert run_work_with_progress(window, 3, "Working...", work) == ("stopped", True)
    assert len(shown) == 1 and shown[0].startswith("Cancelling")

    def boom(_progress):
        raise ValueError("bad file")

    with pytest.raises(ValueError, match="bad file"):
        run_work_with_progress(window, 1, "Working...", boom)


def test_work_progress_state_is_thread_safe_and_snapshots():
    progress = WorkProgress(5)
    progress.on_progress(2)
    progress.set_label("a.mp3")
    assert progress.snapshot() == (2, "a.mp3") and not progress.should_cancel()
    progress.cancelled.set()
    assert progress.should_cancel()


def test_local_db_check_file_runs_off_the_gui_thread(monkeypatch):
    from redactor_common.gui.local_db_settings_dialog import LocalDatabaseSettingsDialog

    main = threading.get_ident()
    seen = {}

    def check(path):
        seen["thread"] = threading.get_ident()
        seen["path"] = path
        return "4 titles"

    dialog = LocalDatabaseSettingsDialog(
        title="Test DB", instructions_html="x", path="  C:/db/test.db  ", check=check, save=lambda p: None, error_types=(ValueError,),
    )
    shown = []
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: shown.append(a[2])))
    dialog._run_check()
    assert seen["thread"] != main and seen["path"] == "C:/db/test.db"  # stripped, read before the worker started
    assert shown == ["4 titles"]
    dialog.path_edit.setText("C:/other.db")
    assert dialog.check_result("C:/explicit.db")[0] and seen["path"] == "C:/explicit.db"  # an explicit path wins over the widget

"""gui/progress.run_work_with_progress: the work runs off the GUI thread, Cancel reaches it, partial results come back."""

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QProgressDialog, QPushButton, QWidget  # noqa: E402

from redactor_common.gui.progress import run_work_with_progress  # noqa: E402

_app = QApplication.instance() or QApplication([])


def _dialogs():
    return [w for w in QApplication.allWidgets() if isinstance(w, QProgressDialog) and w.isVisible()]


def test_runs_off_the_gui_thread_and_returns_the_result():
    window = QWidget()
    seen = {}

    def work(progress, cancel):
        seen["thread"] = threading.current_thread()
        for i in range(5):
            progress(i + 1, 5)
            time.sleep(0.02)
        return "result"

    assert run_work_with_progress(window, "Working...", work, 5) == "result"
    assert seen["thread"] is not threading.main_thread()
    _app.processEvents()
    assert _dialogs() == []


def test_cancel_sets_the_event_and_partial_result_is_returned():
    window = QWidget()
    window.show()

    def work(progress, cancel):
        done = 0
        while not cancel.is_set() and done < 200:
            done += 1
            progress(done, 200)
            time.sleep(0.01)
        return ("cancelled" if cancel.is_set() else "finished", done)

    from PyQt6.QtCore import QTimer

    def press_cancel():
        for d in QApplication.allWidgets():
            if isinstance(d, QProgressDialog) and d.isVisible():
                d.findChild(QPushButton).click()  # the real Cancel button

    QTimer.singleShot(150, press_cancel)
    outcome, done = run_work_with_progress(window, "Working...", work, 200)
    assert outcome == "cancelled" and 0 < done < 200
    _app.processEvents()
    assert _dialogs() == []


def test_exception_is_reraised():
    def work(progress, cancel):
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        run_work_with_progress(QWidget(), "x", work, 3)

"""run_with_progress(on_cancel=...): Cancel reaches a running step's cancel event at once, not only between items."""
import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QProgressDialog, QWidget  # noqa: E402

from redactor_common.gui.progress import run_with_progress  # noqa: E402

_app = QApplication.instance() or QApplication([])


def test_on_cancel_runs_when_cancel_is_clicked_mid_item():
    parent = QWidget()
    event = threading.Event()
    seen = []

    def step(item, index):
        # Simulate the user clicking Cancel while item 0 is still running.
        dialog = parent.findChild(QProgressDialog)
        assert dialog is not None
        dialog.canceled.emit()  # what the Cancel button does (Qt wires canceled -> cancel() itself)
        seen.append((item, event.is_set()))

    finished = run_with_progress(parent, [1, 2, 3], step, "Working...", threshold=1, on_cancel=event.set)
    assert finished is False
    assert seen == [(1, True)]  # the event was set DURING item 1, and item 2 never started


def test_on_cancel_is_ignored_when_not_cancellable():
    parent = QWidget()
    calls = []
    run_with_progress(parent, [1, 2], lambda i, n: None, "Working...", threshold=1, cancellable=False, on_cancel=lambda: calls.append(1))
    assert calls == []

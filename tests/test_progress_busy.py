"""gui/progress.run_busy: a finished call never leaves its progress dialog behind."""

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QProgressDialog, QWidget  # noqa: E402

from redactor_common.gui.progress import run_busy  # noqa: E402

_app = QApplication.instance() or QApplication([])


def _visible_progress_dialogs():
    return [w for w in QApplication.allWidgets() if isinstance(w, QProgressDialog) and w.isVisible()]


def test_no_progress_dialog_is_left_on_screen_after_the_calls():
    """Work that ends around the dialog's 300 ms show-delay used to leave "Scanning..." boxes that nothing ever
    closed (2026-10-14#08, mp3redactor Open Folder): the dialog's own timer showed them AFTER the call had
    returned. One window, several calls in a row, the way the real app keeps running."""
    window = QWidget()
    window.show()
    for work_ms in (0, 150, 290, 350, 600):
        result = run_busy(window, "Scanning...", lambda ms=work_ms: (time.sleep(ms / 1000), "done")[1])
        assert result == "done"
        end = time.perf_counter() + 0.8  # longer than the show-delay
        while time.perf_counter() < end:
            _app.processEvents()
            time.sleep(0.01)
        assert _visible_progress_dialogs() == [], f"after a {work_ms} ms call"
    window.close()

"""
redactor_common/gui/work_progress.py

Runs a slow core function on a worker thread under a counted progress
dialog, for work that reports through `progress(done, total)` /
`should_cancel()` callbacks (the scan_service checks, the import
conversion, the M4B build).

Why not redactor_common's ProgressReporter: that one is driven from the
GUI thread, so the window only repaints when a file *finishes*. One
long ffmpeg conversion, a keyfinder run on a long track or an M4B join
froze the window for its whole length, and Cancel could not even be
clicked. Why not run_busy: it has no count, and its Cancel abandons the
call while it is still changing files.

Here the work runs off the GUI thread (redactor_common's
call_in_background) and a timer on the GUI thread copies the count and
label into the dialog. Cancel sets an event the work polls through
`should_cancel()`; the call is always waited for, so nothing is still
changing files when this returns. The dialog appears only after
SHOW_AFTER_MS, so quick work never flashes one, and stays window-modal
(also while cancelling) so the list cannot be changed under the work.

`work` must not touch widgets. Promoted from mp3redactor (2026-10-13); the counted variant of
progress.run_busy: use run_busy for ONE call with no count, run_with_progress for a loop that is
safe to run on the GUI thread between items, and this for slow work that reports through
progress()/should_cancel() callbacks (an ffmpeg conversion, a keyfinder run, an M4B join).
"""

from __future__ import annotations

import threading
from typing import Callable, TypeVar

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QProgressDialog, QWidget

from redactor_common.gui.background_call import call_in_background
from redactor_common.gui.progress import PROGRESS_DIALOG_WIDTH, _close_quietly, _InputBlock

T = TypeVar("T")

SHOW_AFTER_MS = 300
REFRESH_MS = 100
_MAX_LABEL_LENGTH = 70
CANCELLING_TEXT = "Cancelling... (finishing the file in progress)"


def _elide(text: str) -> str:
    text = text or ""
    return text if len(text) <= _MAX_LABEL_LENGTH else text[: _MAX_LABEL_LENGTH - 1].rstrip() + "…"


class WorkProgress:
    """The thread-safe progress state the worker writes and the dialog reads."""

    def __init__(self, total: int):
        self.total = total
        self._lock = threading.Lock()
        self._done = 0
        self._total_seen: int | None = None
        self._label: str | None = None
        self.cancelled = threading.Event()

    def on_progress(self, done: int, _total: int | None = None) -> None:
        """A `progress(done, total)` callback for core functions."""
        with self._lock:
            self._done = done

    def report(self, done: int, total: int) -> None:
        """`progress(done, total)` that also moves the dialog's maximum (the older progress.run_work_with_progress
        contract, where the worker may learn the real total as it goes)."""
        with self._lock:
            self._done = int(done)
            self._total_seen = max(int(total), 1)

    def set_value(self, done: int) -> None:
        self.on_progress(done)

    def set_label(self, text: str) -> None:
        with self._lock:
            self._label = text

    def should_cancel(self) -> bool:
        """A `should_cancel()` callback for core functions."""
        return self.cancelled.is_set()

    def snapshot(self) -> tuple[int, str | None]:
        with self._lock:
            return self._done, self._label

    def total_seen(self) -> int | None:
        with self._lock:
            return self._total_seen


def run_work_with_progress(
    parent: QWidget,
    total: int,
    label: str,
    work: Callable[[WorkProgress], T],
    cancellable: bool = True,
    title: str | None = None,
    *,
    show_after_ms: int = SHOW_AFTER_MS,
    cancelling_text: str = CANCELLING_TEXT,
    dynamic_total: bool = False,
    refresh_ms: int = REFRESH_MS,
) -> tuple[T, bool]:
    """Runs `work(progress)` on a worker thread under a counted, window-modal
    progress dialog. Returns (work's result, whether Cancel was pressed). An
    exception in `work` is re-raised here.

    This is THE implementation behind both run_work_with_progress functions (progress.py's older
    (parent, label, work(progress, cancel), total) -> T is a thin adapter over it). The parent's window is disabled
    for the whole call (progress._InputBlock), the dialog appears after `show_after_ms` (0: at once), Cancel keeps
    it on screen reading `cancelling_text` until the work has returned, and `dynamic_total` lets the worker move
    the maximum through WorkProgress.report()."""
    progress = WorkProgress(total)
    with _InputBlock(parent) as block:
        dialog = QProgressDialog(label, "Cancel" if cancellable else None, 0, max(total, 0), parent)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setMinimumDuration(24 * 3600 * 1000)  # shown by show_timer below, not by Qt's own timer
        dialog.setFixedWidth(PROGRESS_DIALOG_WIDTH)
        if title:
            dialog.setWindowTitle(title)
        block.allow(dialog)
        running = [True]

        def show_dialog() -> None:
            if running[0]:
                block.reveal()
                dialog.show()

        def on_cancel() -> None:
            if not running[0]:
                return
            progress.cancelled.set()
            dialog.setLabelText(cancelling_text)
            # QProgressDialog hides itself on Cancel (its canceled signal is wired to cancel()/reset()); it stays up
            # (modal) until the work has stopped, so the window cannot be used under work that is still running.
            QTimer.singleShot(0, show_dialog)

        def refresh() -> None:
            done, text = progress.snapshot()
            seen = progress.total_seen() if dynamic_total else None
            if seen is not None and dialog.maximum() != seen:
                dialog.setMaximum(seen)
            maximum = dialog.maximum()
            if maximum > 0:
                dialog.setValue(min(done, maximum))
            if text and not progress.cancelled.is_set():
                dialog.setLabelText(_elide(text))

        if cancellable:
            dialog.canceled.connect(on_cancel)
        show_timer = QTimer(dialog)
        show_timer.setSingleShot(True)
        show_timer.timeout.connect(show_dialog)
        if show_after_ms <= 0:
            show_dialog()
        else:
            show_timer.start(show_after_ms)
        refresh_timer = QTimer(dialog)
        refresh_timer.timeout.connect(refresh)
        refresh_timer.start(refresh_ms)
        try:
            result = call_in_background(work, progress)
        finally:
            running[0] = False
            show_timer.stop()
            refresh_timer.stop()
            if cancellable:
                try:
                    dialog.canceled.disconnect(on_cancel)  # a normal close must never look like a Cancel click
                except (TypeError, RuntimeError):
                    pass
            _close_quietly(dialog)
    return result, progress.cancelled.is_set()

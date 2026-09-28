"""
redactor_common/gui/background_call.py

Runs one slow call (a network request, a big file scan) on a worker
thread while the GUI keeps painting and responding, then hands the
result back to the caller as if it had been an ordinary blocking call:

    result = call_in_background(fetch_issue_details, url, cancel_signal=dialog.canceled)

Before this, the lookup dialogs made their HTTP requests directly on
the GUI thread: every request (about 3 s each against a slow source)
froze the window -- no repaint, and the progress dialog's Cancel button
couldn't even be clicked until the request finished.

Semantics:
- Returns `fn(*args, **kwargs)`'s return value; an exception raised in
  the worker is re-raised here, on the calling (GUI) thread, unchanged.
- `cancel_signal` (optional, e.g. a QProgressDialog's `canceled`):
  firing it returns control immediately by raising `BackgroundCancelled`
  -- the call already in flight is abandoned, not waited for (a Python
  thread can't be interrupted); its eventual result is discarded.
- Must be called from the GUI thread, with a QApplication running.
  While waiting it runs a local event loop, so the caller must keep
  the rest of the app from being used meanwhile (an application-modal
  progress dialog does that) -- otherwise the user could, say, remove
  the very items being processed.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from PyQt6.QtCore import QEventLoop, QObject, pyqtSignal


class BackgroundCancelled(Exception):
    """The cancel signal fired before the background call finished."""


class _Finished(QObject):
    done = pyqtSignal()


def call_in_background(
    fn: Callable[..., Any],
    *args: Any,
    cancel_signal: Optional[Any] = None,
    **kwargs: Any,
) -> Any:
    outcome: dict[str, Any] = {}
    finished = _Finished()
    loop = QEventLoop()
    finished.done.connect(loop.quit)

    cancelled = threading.Event()
    if cancel_signal is not None:
        def _on_cancel(*_a):
            cancelled.set()
            loop.quit()
        cancel_signal.connect(_on_cancel)

    def _work() -> None:
        try:
            outcome["result"] = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 -- re-raised on the caller's thread
            outcome["error"] = exc
        finally:
            finished.done.emit()

    thread = threading.Thread(target=_work, name="background-call", daemon=True)
    thread.start()
    try:
        if not outcome and not cancelled.is_set():
            loop.exec()
    finally:
        if cancel_signal is not None:
            try:
                cancel_signal.disconnect(_on_cancel)
            except (TypeError, RuntimeError):
                pass

    if "error" in outcome:
        raise outcome["error"]
    if "result" in outcome:
        return outcome["result"]
    if cancelled.is_set():
        raise BackgroundCancelled()
    # The loop can only stop early through cancel; anything else means
    # the worker finished between checks -- wait for its outcome.
    thread.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("result")

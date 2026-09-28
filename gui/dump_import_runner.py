"""
redactor_common/gui/dump_import_runner.py

Runs a long dump import (core/dump_import.py -- seconds to many minutes)
on a worker thread behind a progress dialog whose Cancel really stops
it:

    counts = run_dump_import(self, "Build Library Database", "Reading ComicDb.xml...",
                             lambda progress, cancelled: build(src, dest, progress, cancelled))
    if counts is None:  # cancelled
        ...

`work(progress, cancelled)` runs on the worker thread: it reports
`progress(fraction)` (0..1) and checks `cancelled()`, raising
core.dump_import.ImportCancelled when it's true (the readers do both).

Unlike a lookup (gui/background_call.py abandons a cancelled call and
returns at once), an import is waited for after Cancel: the worker has
to stop writing and delete its partial file before the app goes on --
the dialog says "Cancelling..." meanwhile, which takes well under a
second since the readers check every few hundred records.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QProgressDialog, QWidget

from redactor_common.core.dump_import import ImportCancelled
from redactor_common.gui.background_call import call_in_background
from redactor_common.gui.progress import PROGRESS_DIALOG_WIDTH

_STEPS = 1000


def run_dump_import(
    parent: Optional[QWidget],
    title: str,
    label: str,
    work: Callable[[Callable[[float], None], Callable[[], bool]], Any],
) -> Any:
    """`work(progress, cancelled)`'s result, or None if the user cancelled.
    Any other exception from `work` is re-raised here (GUI thread)."""
    dialog = QProgressDialog(label, "Cancel", 0, _STEPS, parent)
    dialog.setWindowTitle(title)
    # Application-modal: the event loop keeps running while we wait, and
    # nothing else in the app may be used meanwhile.
    dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
    dialog.setMinimumDuration(0)
    dialog.setAutoClose(False)
    dialog.setAutoReset(False)
    dialog.setFixedWidth(PROGRESS_DIALOG_WIDTH)

    stop = threading.Event()
    state = {"fraction": 0.0}

    def on_cancel() -> None:
        stop.set()
        dialog.setLabelText("Cancelling...")

    dialog.canceled.connect(on_cancel)
    timer = QTimer(dialog)
    timer.setInterval(100)
    timer.timeout.connect(lambda: dialog.setValue(int(state["fraction"] * _STEPS)))

    def progress(fraction: float) -> None:  # worker thread: no Qt calls here
        state["fraction"] = fraction

    dialog.show()
    timer.start()
    try:
        return call_in_background(work, progress, stop.is_set)
    except ImportCancelled:
        return None
    finally:
        timer.stop()
        dialog.close()
        dialog.deleteLater()

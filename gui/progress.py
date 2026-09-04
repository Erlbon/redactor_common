"""
redactor_common/gui/progress.py

One shared helper for the "loading/information screens for file/folder
and other operations that inform the user what's happening" pattern
used throughout both projects (file load, save, table rebuild, batch
metadata operations).

Two things worth keeping consistent that both projects had arrived at
independently, in slightly different shapes, at different call sites:

1. Only show a dialog at all once the item count crosses a threshold
   (epub's v56 fix: a progress dialog for a routine small batch just
   flickers uselessly -- REBUILD_PROGRESS_THRESHOLD=500 vs the 3-file
   LOAD_PROGRESS_THRESHOLD for a much more expensive per-item cost).
2. Cancellable vs. not is a real design decision, not a default: an
   operation that's just re-displaying an already-made decision (a
   table rebuild) shouldn't offer Cancel, since canceling partway
   wouldn't undo anything, just leave the view inconsistent. An
   operation that's actually doing the work as it goes (loading files)
   should.
"""

from __future__ import annotations

from typing import Callable, Iterable, TypeVar

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QProgressDialog, QWidget

T = TypeVar("T")


def run_with_progress(
    parent: QWidget,
    items: Iterable[T],
    step: Callable[[T, int], None],
    label: str,
    threshold: int = 3,
    cancellable: bool = True,
    update_every: int = 1,
) -> bool:
    """Runs `step(item, index)` for each item in `items`, showing a
    QProgressDialog only if len(items) >= threshold (items is consumed
    into a list first to get the count). Pumps the event loop so the
    dialog actually repaints and, if cancellable, so Cancel is
    responsive -- QProgressDialog doesn't do this on its own.

    Returns True if every item was processed, False if the user
    cancelled partway (only possible when cancellable=True).

    `update_every`: call setValue()/processEvents() every N items
    rather than every single one -- cheap per-item operations (e.g.
    populating a table row) shouldn't pay a full event-loop pump each
    time; use a larger value (e.g. 50) for those.
    """
    items = list(items)
    dialog = None
    if len(items) >= threshold:
        dialog = QProgressDialog(
            label, "Cancel" if cancellable else None, 0, len(items), parent
        )
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.show()

    for index, item in enumerate(items):
        if dialog is not None and cancellable and dialog.wasCanceled():
            dialog.close()
            return False
        step(item, index)
        if dialog is not None and (index % update_every == 0 or index == len(items) - 1):
            dialog.setValue(index + 1)
            QApplication.processEvents()

    if dialog is not None:
        dialog.close()
    return True

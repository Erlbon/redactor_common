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

# Wide enough for a typical "Verb: reasonably-long-filename.ext" label
# to read comfortably -- paired with _elide_label() below so a per-item
# label (see `label_for`) never needs more than this to fit. Applied as
# a FIXED width (see setFixedWidth() below), not just a minimum: a
# QProgressDialog grows to fit whatever the longest label text seen so
# far needed, but doesn't shrink back down once a later, shorter label
# follows -- a minimum alone still lets that one-way growth happen, and
# looks like the dialog jumping around in size as items with very
# different filename lengths go by.
PROGRESS_DIALOG_WIDTH = 420
_MAX_LABEL_LENGTH = 70


def _elide_label(text: str, max_length: int = _MAX_LABEL_LENGTH) -> str:
    """Pure logic, split out for testability: truncates `text` to at
    most `max_length` characters, keeping the start and adding a
    trailing "…" if it was longer. Used on label_for()'s per-item
    result -- the one static `label` (the whole-run fallback) is never
    elided, since the caller wrote that one deliberately and it doesn't
    vary per item."""
    text = text or ""
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + "…"


def run_with_progress(
    parent: QWidget,
    items: Iterable[T],
    step: Callable[[T, int], None],
    label: str,
    threshold: int = 3,
    cancellable: bool = True,
    update_every: int = 1,
    label_for: Callable[[T], str] | None = None,
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

    `label_for`: optional per-item label text (e.g. "Saving: foo.epub")
    instead of the one static `label` for the whole run -- set on every
    item regardless of `update_every`, since setLabelText() alone is
    cheap (no repaint without the processEvents() pump that already
    follows it). Without this, epub and video had each independently
    hand-rolled their own near-identical copy of this whole function
    just to get per-item filenames in the dialog.
    """
    items = list(items)
    dialog = None
    if len(items) >= threshold:
        dialog = QProgressDialog(
            label, "Cancel" if cancellable else None, 0, len(items), parent
        )
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(0)
        # Fixed, not just minimum -- a QProgressDialog grows to fit
        # whatever its longest label text so far needed, but doesn't
        # shrink back down once a later, shorter label follows (Qt only
        # grows a widget to fit new content, it doesn't proactively
        # re-shrink it). A minimum-only width still lets that one-way
        # ratchet happen upward past it; fixing the width outright is
        # what actually keeps the dialog visually steady for the whole
        # run. _elide_label() below caps how much text the label ever
        # has to fit, so eliding to fit this fixed width reads sensibly
        # rather than getting mid-word cut off.
        dialog.setFixedWidth(PROGRESS_DIALOG_WIDTH)
        dialog.show()

    for index, item in enumerate(items):
        if dialog is not None and cancellable and dialog.wasCanceled():
            dialog.close()
            return False
        if dialog is not None and label_for is not None:
            dialog.setLabelText(_elide_label(label_for(item)))
        step(item, index)
        if dialog is not None and (index % update_every == 0 or index == len(items) - 1):
            dialog.setValue(index + 1)
            QApplication.processEvents()

    if dialog is not None:
        dialog.close()
    return True

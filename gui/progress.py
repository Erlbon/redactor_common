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

import threading
from typing import Any, Callable, Iterable, TypeVar

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import QApplication, QProgressDialog, QWidget

from redactor_common.gui.background_call import BackgroundCancelled, call_in_background

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


def _close_quietly(dialog: QProgressDialog) -> None:
    """Closes `dialog` WITHOUT emitting `canceled`: QProgressDialog emits it on every close, so a hook connected
    to it (a worker's cancel event, an on_cancel callback) would otherwise fire on a normal, finished run."""
    block = getattr(dialog, "blockSignals", None)
    if block is not None:
        block(True)
    # reset() also stops the dialog's own show-after-minimumDuration timer. Without it, work that ends after
    # the dialog was due (about 300 ms) but before it was actually shown leaves that timer running: it shows
    # the dialog AFTER the work is done and nothing ever closes it (a "Scanning for MP3 files..." box stuck
    # on screen for good, 2026-10-14).
    reset = getattr(dialog, "reset", None)
    if reset is not None:
        reset()
    dialog.close()
    # The dialog is parented to the caller's window, so without this every call left one more hidden
    # QProgressDialog child behind for the life of the window.
    delete_later = getattr(dialog, "deleteLater", None)
    if delete_later is not None:
        delete_later()


class _InputBlock:
    """Keeps the user out of the app for the whole length of a worker-thread call (run_busy, run_work_with_progress).

    A window-modal QProgressDialog only blocks the parent once it is SHOWN, which is after a short delay; before that, and
    while the dialog is hidden by a Cancel click, a click, key press or menu action could start a second call or change
    the rows the first one is working on (2026-10-16 review: a nested second run_busy 100 ms after the first). So the
    parent's top-level window is disabled from the first moment and restored in __exit__ (also after an exception), together
    with the keyboard focus and the busy cursor.

    A window that was already disabled by the caller (videoredactor's lookup does that) is left alone and is not
    re-enabled here: the previous state is what comes back, not blindly True.

    A quick call must not make the window flash grey: repaints are held back until reveal() (called when the dialog is
    really shown), so a call that ends first never shows the disabled look."""

    def __init__(self, parent: QWidget | None):
        self._parent = parent
        self._window: QWidget | None = None
        self._updates_were_on = True
        self._revealed = False
        self._cursor = False
        self._focus: QWidget | None = None
        self._was_active = False

    def __enter__(self) -> "_InputBlock":
        window = None
        if self._parent is not None:
            try:
                window = self._parent.window()
            except RuntimeError:
                window = None
        if window is not None and window.isEnabled():
            self._window = window
            self._focus = window.focusWidget()  # the window's own idea of it: also right while it is not the active window
            self._updates_were_on = window.updatesEnabled()
            self._was_active = window.isActiveWindow()
            window.setUpdatesEnabled(False)
            window.setEnabled(False)
        if QApplication.instance() is not None:
            QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
            self._cursor = True
        return self

    def allow(self, dialog: QWidget) -> None:
        """A window (the progress dialog) is a child of the disabled window and would be disabled with it: its Cancel
        button must stay clickable."""
        if self._window is not None:
            dialog.setEnabled(True)

    def reveal(self) -> None:
        """The dialog is about to be shown: let the disabled window repaint (it now looks inactive behind the dialog)."""
        if self._window is not None and not self._revealed:
            self._revealed = True
            try:
                self._window.setUpdatesEnabled(self._updates_were_on)
            except RuntimeError:
                pass

    def __exit__(self, *_exc) -> None:
        window, self._window = self._window, None
        if window is not None:
            try:
                window.setEnabled(True)  # before the repaint is released: a quick call never shows the grey look
                window.setUpdatesEnabled(self._updates_were_on)
                # Disabling the active window makes Windows hand the activation to another window, and
                # re-enabling does not take it back: a modal dialog (the lookup dialogs) then sat there
                # with live buttons but fields and tables that could not get focus (cbzredactor,
                # 2026-10-20). Take the activation back before the focus.
                if self._was_active and window.isVisible() and not window.isActiveWindow():
                    window.activateWindow()
                focus = self._focus
                if focus is not None and focus.isEnabled() and focus.isVisible():
                    focus.setFocus(Qt.FocusReason.OtherFocusReason)
            except RuntimeError:  # the window was deleted during the call
                pass
        if self._cursor:
            QApplication.restoreOverrideCursor()
            self._cursor = False


def _make_dialog(
    parent: QWidget, label: str, total: int, cancellable: bool, title: str | None = None
) -> QProgressDialog:
    dialog = QProgressDialog(label, "Cancel" if cancellable else None, 0, total, parent)
    dialog.setWindowModality(Qt.WindowModality.WindowModal)
    dialog.setMinimumDuration(0)
    if title:
        dialog.setWindowTitle(title)
    # Fixed, not just minimum -- a QProgressDialog grows to fit
    # whatever its longest label text so far needed, but doesn't
    # shrink back down once a later, shorter label follows (Qt only
    # grows a widget to fit new content, it doesn't proactively
    # re-shrink it). A minimum-only width still lets that one-way
    # ratchet happen upward past it; fixing the width outright is
    # what actually keeps the dialog visually steady for the whole
    # run. _elide_label() caps how much text the label ever has to
    # fit, so eliding to fit this fixed width reads sensibly rather
    # than getting mid-word cut off.
    dialog.setFixedWidth(PROGRESS_DIALOG_WIDTH)
    dialog.show()
    return dialog


def run_with_progress(
    parent: QWidget,
    items: Iterable[T],
    step: Callable[[T, int], None],
    label: str,
    threshold: int = 3,
    cancellable: bool = True,
    update_every: int = 1,
    label_for: Callable[[T], str] | None = None,
    on_cancel: Callable[[], None] | None = None,
    on_error: Callable[[T, Exception], None] | None = None,
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

    `on_cancel`: runs the moment Cancel is clicked (the dialog's
    `canceled` signal), before the current item has finished -- a step
    that runs a long external process (ffmpeg) in the background can
    hand its cancel event's `set` here so Cancel stops that process
    instead of only skipping the items not yet started.

    `on_error`: without it an exception from step() propagates and ENDS the whole run (the items after it are
    never processed). With it, the exception is handed to `on_error(item, exc)` and the loop carries on with
    the next item -- one bad file must not stop a batch (Save All used to stop silently at the first one).
    """
    items = list(items)
    dialog = None
    if len(items) >= threshold:
        dialog = _make_dialog(parent, label, len(items), cancellable)
        if cancellable and on_cancel is not None:
            dialog.canceled.connect(on_cancel)

    try:
        for index, item in enumerate(items):
            if dialog is not None and cancellable and dialog.wasCanceled():
                return False
            if dialog is not None and label_for is not None:
                dialog.setLabelText(_elide_label(label_for(item)))
            if on_error is None:
                step(item, index)
            else:
                try:
                    step(item, index)
                except Exception as exc:  # noqa: BLE001 - isolated per item on purpose
                    on_error(item, exc)
            if dialog is not None and (index % update_every == 0 or index == len(items) - 1):
                dialog.setValue(index + 1)
                QApplication.processEvents()
        return True
    finally:
        # Also on an exception from step(): a window-modal dialog left
        # open would block the whole window behind the crash message.
        if dialog is not None:
            _close_quietly(dialog)


class ProgressReporter:
    """The same threshold-gated, fixed-width, label-eliding dialog as
    run_with_progress(), for work that drives its own loop instead of
    handing this module one item at a time:

    - a core function taking `progress(done, total)`/`should_cancel()`
      callbacks (mp3's scan_service thread pools, its import
      conversion) -- pass `reporter.on_progress` and
      `reporter.should_cancel` straight through;
    - a QThread worker whose signals report per-file progress (video's
      transcodes) -- call set_label()/set_value() from the slots, and
      connect_cancel() to tell the worker to stop.

    mp3 and video had each hand-rolled these dialogs five times between
    them, missing the fixed width (so they jittered with per-file
    labels). Use as a context manager so the dialog always closes:

        with ProgressReporter(self, len(targets), "Checking...") as reporter:
            scan_fn(targets, progress=reporter.on_progress,
                    should_cancel=reporter.should_cancel)
    """

    def __init__(
        self,
        parent: QWidget,
        total: int,
        label: str,
        threshold: int = 3,
        cancellable: bool = True,
        title: str | None = None,
    ):
        self.total = total
        self._dialog: QProgressDialog | None = None
        if total >= threshold:
            self._dialog = _make_dialog(parent, label, total, cancellable, title)

    @property
    def dialog(self) -> QProgressDialog | None:
        """The underlying dialog, or None below the threshold."""
        return self._dialog

    def set_value(self, done: int, pump: bool = True) -> None:
        if self._dialog is not None:
            self._dialog.setValue(min(done, self.total))
            if pump:
                QApplication.processEvents()

    def set_label(self, text: str) -> None:
        if self._dialog is not None:
            self._dialog.setLabelText(_elide_label(text))

    def on_progress(self, done: int, _total: int | None = None) -> None:
        """A `progress(done, total)` callback for core functions."""
        self.set_value(done)

    def should_cancel(self) -> bool:
        """A `should_cancel()` callback for core functions."""
        return self._dialog is not None and self._dialog.wasCanceled()

    def was_canceled(self) -> bool:
        return self.should_cancel()

    def connect_cancel(self, slot: Callable[[], None]) -> None:
        """Runs `slot` when Cancel is clicked (e.g. to set a worker's
        cancel event). No-op below the threshold."""
        if self._dialog is not None:
            self._dialog.canceled.connect(slot)

    def close(self) -> None:
        if self._dialog is not None:
            self._dialog.setValue(self.total)
            _close_quietly(self._dialog)
            self._dialog = None

    def __enter__(self) -> "ProgressReporter":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


BUSY_SHOW_AFTER_MS = 300  # a call that finishes sooner never flashes a dialog
_NEVER_MS = 24 * 3600 * 1000  # Qt's own show timer is replaced by ours (see run_busy)


def run_busy(
    parent: QWidget,
    label: str,
    fn: Callable[..., T],
    *args: Any,
    cancellable: bool = False,
    title: str | None = None,
    on_cancel: Any = None,
    **kwargs: Any,
) -> T:
    """Runs ONE slow call (a folder walk, an ffprobe, a lookup, a conversion) on a worker thread under an
    indeterminate progress dialog, and returns its result as if it had been an ordinary blocking call. The
    window keeps painting, and the dialog (fixed width, like run_with_progress) appears only after
    BUSY_SHOW_AFTER_MS. An exception in `fn` is re-raised here. With `cancellable`, Cancel abandons the call
    and `on_cancel` is returned (the work already running is not interrupted: give long ffmpeg-style calls their
    own cancel event as well).

    The parent's window is disabled for the whole call (see _InputBlock), so nothing can be clicked, typed or
    started from a menu meanwhile, not even before the dialog has appeared. Because of that `fn` may be sure
    the rows/widgets it was handed are not changed by the user, but a CALLER must not open a modal dialog from
    inside `fn` (it runs off the GUI thread anyway).

    `fn` runs OFF the GUI thread: it must not touch widgets. Use run_with_progress() for loops over many items
    (it can show a real count and cancel between items); use this for the single long call."""
    with _InputBlock(parent) as block:
        dialog = QProgressDialog(label, "Cancel" if cancellable else None, 0, 0, parent)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        # Qt's own show timer starts in the constructor with 4000 ms and ignores setMinimumDuration() while the
        # dialog's value is still -1, so the dialog came up after ~4 s instead of 300 ms (2026-10-16 review).
        # The show is driven by our own single-shot timer instead.
        dialog.setMinimumDuration(_NEVER_MS)
        if title:
            dialog.setWindowTitle(title)
        dialog.setFixedWidth(PROGRESS_DIALOG_WIDTH)
        block.allow(dialog)
        running = [True]

        def show_dialog() -> None:
            if running[0]:
                block.reveal()
                dialog.show()

        show_timer = QTimer(dialog)
        show_timer.setSingleShot(True)
        show_timer.timeout.connect(show_dialog)
        show_timer.start(BUSY_SHOW_AFTER_MS)
        try:
            return call_in_background(fn, *args, cancel_signal=dialog.canceled if cancellable else None, **kwargs)
        except BackgroundCancelled:
            return on_cancel
        finally:
            running[0] = False
            show_timer.stop()
            _close_quietly(dialog)


def run_work_with_progress(
    parent: QWidget,
    label: str,
    work: Callable[[Callable[[int, int], None], threading.Event], T],
    total: int,
    cancellable: bool = True,
    title: str | None = None,
    poll_ms: int = 100,
) -> T:
    """Runs `work(progress, cancel)` on a worker thread under a progress dialog with a real count, and returns
    its result as if it had been an ordinary call.

    `progress(done, total)` may be called from the worker (it only stores the numbers; the dialog is updated
    from the GUI thread by a timer, so the worker never touches a widget). `cancel` is a threading.Event set
    when the user clicks Cancel: the work should notice it between steps and return what it has (the call does
    not return before the work does, so partial results are kept; a long single step delays the end of the
    dialog, which then reads "Cancelling..." and stays on screen, modal, until the work has returned). An
    exception in `work` is re-raised here.

    Unlike run_busy(), the dialog is shown at once (the caller uses it for work known to be slow).

    This is a thin adapter: the implementation is gui/work_progress.run_work_with_progress() (one dialog, one
    cancel/re-show/close sequence); this keeps the older (parent, label, work(progress, cancel), total) -> T
    signature that the apps call."""
    from redactor_common.gui import work_progress

    def adapted(progress: "work_progress.WorkProgress") -> T:
        return work(progress.report, progress.cancelled)

    result, _cancelled = work_progress.run_work_with_progress(
        parent, max(total, 1), label, adapted, cancellable=cancellable, title=title,
        show_after_ms=0, cancelling_text="Cancelling...", dynamic_total=True, refresh_ms=poll_ms,
    )
    return result

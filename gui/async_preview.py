"""
redactor_common/gui/async_preview.py

AsyncPreviewLoader: loads ONE preview image (the selected file's cover
or thumbnail) off the main thread, so changing the selection never
blocks on reading, generating or decoding it.

Found in two apps (2026-09-23 review): cbz read page 1 out of the
archive and did a full-resolution QPixmap.loadFromData() on the GUI
thread on every selection change (comic pages are often 3000x4500 px);
video ran ffmpeg on the GUI thread the first time each file was
selected, with no timeout. Both now hand this a `loader` callable that
runs on a worker thread and returns the image bytes, or a path to an
image file.

- Requests are debounced (`debounce_ms`), so holding an arrow key down
  through a table starts one load when it settles, not one per row.
- Only the most recent request is delivered: a slow load finishing
  after the selection has already moved on is dropped, never shown.
- Decoding goes through image_decode.decode_scaled(), so a huge image
  is downscaled while decoding rather than after.
- Deleting the owner (closing the window, quitting the app) while a load
  is still running never hangs; see _POOLS below.

Usage:
    self._preview = AsyncPreviewLoader(QSize(600, 900), parent=self)
    self._preview.image_ready.connect(self._on_preview_ready)
    ...
    self._preview.request(book, book.read_first_page_bytes)
    ...
    def _on_preview_ready(self, token, image: QImage):  # main thread
        self.label.set_original_pixmap(QPixmap.fromImage(image) if not image.isNull() else None)
"""

from __future__ import annotations

import atexit
import weakref
from pathlib import Path
from typing import Callable, Union

from PyQt6.QtCore import QObject, QRunnable, QSize, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtGui import QImage
from PyQt6 import sip

from redactor_common.gui.image_decode import decode_scaled

PreviewSource = Union[bytes, str, Path, None]
PreviewLoader = Callable[[], PreviewSource]


# The worker pools, deliberately NOT Qt children of their loader
# (2026-09-29). ~QThreadPool() blocks in waitForDone(), and a loader is
# destroyed with its parent -- from Python (the window's refcount hitting
# zero, or gc) with the GIL held. A load still running at that moment is
# Python code that needs the GIL to finish, so the two waited on each
# other forever: the app hung on exit if a thumbnail was still loading
# when the user quit, and pytest hung at exit after failing tests kept
# windows alive until its final gc. Pools live here instead and are only
# ever waited on from waitForDone() calls PyQt makes with the GIL
# released: shutdown(), and _drain_pools() at interpreter exit.
# Each pool is kept with a weak reference to its loader, so a pool whose
# loader is gone can be dropped once it's idle (destroying an idle pool
# doesn't wait on anything) -- and with the loader's _Signals, so the
# last reference to that main-thread QObject is never dropped by a
# finishing task on the worker thread.
_POOLS: list[tuple[QThreadPool, "_Signals", weakref.ref]] = []


def _register(loader: "AsyncPreviewLoader") -> tuple[QThreadPool, "_Signals"]:
    def in_use(pool: QThreadPool, owner: weakref.ref) -> bool:
        alive = owner()
        return (alive is not None and not sip.isdeleted(alive)) or pool.activeThreadCount() > 0

    _POOLS[:] = [entry for entry in _POOLS if in_use(entry[0], entry[2])]
    pool, signals = QThreadPool(), _Signals()
    _POOLS.append((pool, signals, weakref.ref(loader)))
    return pool, signals


def _drain_pools() -> None:
    for pool, _signals, _owner in _POOLS:
        pool.clear()
        pool.waitForDone()


atexit.register(_drain_pools)


class _Signals(QObject):
    done = pyqtSignal(int, object, QImage)  # generation, token, image


class _LoadTask(QRunnable):
    def __init__(self, generation: int, token: object, loader: PreviewLoader,
                 target_size: QSize | None, signals: _Signals):
        super().__init__()
        self._generation = generation
        self._token = token
        self._loader = loader
        self._target_size = target_size
        self._signals = signals

    def run(self) -> None:
        image = QImage()
        try:
            source = self._loader()
            if isinstance(source, (str, Path)):
                source = Path(source).read_bytes()
            image = decode_scaled(source, self._target_size)
        except Exception:  # noqa: BLE001 - a failed preview shows the placeholder, never crashes
            image = QImage()
        self._signals.done.emit(self._generation, self._token, image)


class AsyncPreviewLoader(QObject):
    """`image_ready(token, image)` fires on the main thread for the most
    recent request only (a null QImage if nothing could be loaded).
    `loading(token)` fires when a load actually starts, for callers that
    want to show a "Loading..." placeholder."""

    image_ready = pyqtSignal(object, QImage)
    loading = pyqtSignal(object)

    def __init__(self, target_size: QSize | None = None, debounce_ms: int = 100,
                 parent: QObject | None = None, max_threads: int = 2):
        super().__init__(parent)
        self._target_size = target_size
        self._generation = 0
        self._pending: tuple[object, PreviewLoader, QSize | None] | None = None
        self._pool, self._signals = _register(self)
        self._pool.setMaxThreadCount(max_threads)
        self._signals.done.connect(self._on_done)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(debounce_ms)
        self._timer.timeout.connect(self._start_pending)

    def request(self, token: object, loader: PreviewLoader,
                target_size: QSize | None = None) -> None:
        """Replaces any earlier request. `loader` runs on a worker
        thread and returns bytes, a path to an image file, or None."""
        self._generation += 1
        self._pending = (token, loader, target_size or self._target_size)
        self._timer.start()

    def cancel(self) -> None:
        """Drops the pending request and any load already running (its
        result is ignored when it finishes)."""
        self._generation += 1
        self._pending = None
        self._timer.stop()

    def shutdown(self, msecs: int = -1) -> bool:
        """Cancels (see cancel()), drops loads not yet started, and waits
        for the one already running. Never needed for correctness --
        a loader can be deleted at any time -- but lets a window or a
        test fixture finish its background work at a known point."""
        self.cancel()
        self._pool.clear()
        return self._pool.waitForDone(msecs)

    def wait_for_done(self, msecs: int = 5000) -> bool:
        """For tests: start any debounced request now and block until
        the worker pool is idle. Queued results are delivered on the
        next event-loop pass (QApplication.processEvents())."""
        if self._timer.isActive():
            self._timer.stop()
            self._start_pending()
        return self._pool.waitForDone(msecs)

    def _start_pending(self) -> None:
        if self._pending is None:
            return
        token, loader, target_size = self._pending
        self._pending = None
        self.loading.emit(token)
        self._pool.start(_LoadTask(self._generation, token, loader, target_size, self._signals))

    def _on_done(self, generation: int, token: object, image: QImage) -> None:
        if generation != self._generation:
            return  # superseded by a newer request
        self.image_ready.emit(token, image)

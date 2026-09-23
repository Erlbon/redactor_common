"""
redactor_common/gui/async_icon_cache.py

Caches a computed QIcon per item (keyed by the item itself, by
identity, via IdentityWeakDict below -- entries drop automatically once an item is no
longer referenced elsewhere, no manual cleanup needed), and decodes/
scales a cache MISS off the main thread via QThreadPool, so building a
table full of these icons never blocks on image decoding: a row shows
up immediately (blank, or its last-known cached icon), and the real
icon arrives independently whenever its own background decode
finishes -- updating ONLY that one cell, no rebuild, no re-layout, no
other side effect.

Promoted from epub, where every table rebuild re-decoded and re-scaled
every book's cover from raw bytes every single time, even though the
bytes hadn't changed since the last rebuild -- for a large library
(thousands of books), that's thousands of redundant JPEG/PNG decodes
on the UI thread on every Save/Undo/Delete/Refresh, which is what made
a genuinely large batch operation slow well beyond what the mere
per-item cost of everything else in a table rebuild would explain.

Thread-safety note: QImage (not QPixmap) is what actually gets
decoded/scaled off the main thread in the worker task -- QPixmap
construction only happens back on the main thread, inside the
icon_ready slot, since QPixmap (unlike QImage) isn't reliably safe to
touch off the GUI thread on every platform.

Usage:
    self._icon_cache = AsyncIconCache(ICON_SIZE, parent=self)
    self._icon_cache.icon_ready.connect(self._on_icon_ready)

    def _apply_icon(self, item, model_obj):
        image_bytes = model_obj.cover_bytes  # or whatever holds the raw image
        if not image_bytes:
            item.setIcon(QIcon())
            return
        cached = self._icon_cache.get_cached_icon(model_obj, image_bytes)
        if cached is not None:
            item.setIcon(cached)
            return
        item.setIcon(QIcon())  # placeholder until the background decode finishes
        self._icon_cache.request(model_obj, image_bytes, image_bytes)

    # A project that doesn't keep image bytes in memory (cbz reads each
    # cover from its archive on demand) passes a `loader` instead, run on
    # the worker thread, and a small `source` value identifying the
    # image version, e.g. (path, mtime) -- compared with ==, not `is`:
    #     self._icon_cache.request(book, (book.path, mtime),
    #                              loader=book.read_first_page_bytes)

    def _on_icon_ready(self, model_obj, icon):
        # Find model_obj's CURRENT row, if it still has one -- robust to
        # sorting/rebuilds/removals that happened while decoding was in
        # flight (a plain row number captured at request time can't be
        # trusted; Qt's own column-sort reorders rows without telling
        # any of this project's own code). Sets ONLY the icon.
        for row in range(self.table.rowCount()):
            item = self.table.item(row, SOME_COL)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) is model_obj:
                item.setIcon(icon)
                break
"""

from __future__ import annotations

import weakref
from typing import Callable

from PyQt6.QtCore import QObject, QRunnable, QSize, QThreadPool, pyqtSignal
from PyQt6.QtGui import QIcon, QImage, QPixmap

from redactor_common.gui.image_decode import decode_scaled

ImageLoader = Callable[[], "bytes | None"]


class IdentityWeakDict:
    """A weak-keyed dict keyed by object IDENTITY, not hash/equality --
    so an unhashable model object (a dataclass with the default eq=True,
    like cbz's CbzBook) can be a key, and two equal-but-distinct objects
    never share an entry. An entry disappears once its key object is
    garbage-collected, same as weakref.WeakKeyDictionary."""

    def __init__(self):
        self._data: dict[int, tuple[weakref.ref, object]] = {}

    def _ref(self, key: object) -> weakref.ref:
        key_id = id(key)
        return weakref.ref(key, lambda _r, key_id=key_id: self._data.pop(key_id, None))

    def get(self, key: object, default=None):
        entry = self._data.get(id(key))
        if entry is None or entry[0]() is not key:
            return default
        return entry[1]

    def __contains__(self, key: object) -> bool:
        return self.get(key, _MISSING) is not _MISSING

    def __getitem__(self, key: object):
        value = self.get(key, _MISSING)
        if value is _MISSING:
            raise KeyError(key)
        return value

    def __setitem__(self, key: object, value: object) -> None:
        self._data[id(key)] = (self._ref(key), value)

    def __delitem__(self, key: object) -> None:
        if key not in self:
            raise KeyError(key)
        del self._data[id(key)]

    def pop(self, key: object, default=None):
        if key not in self:
            return default
        return self._data.pop(id(key))[1]

    def __len__(self) -> int:
        return sum(1 for ref, _v in self._data.values() if ref() is not None)


_MISSING = object()


class _DecodeSignals(QObject):
    ready = pyqtSignal(object, object, QImage)  # key, source, scaled_image


class _DecodeTask(QRunnable):
    def __init__(
        self,
        key: object,
        source: object,
        image_bytes: bytes | None,
        loader: ImageLoader | None,
        icon_size: QSize,
        signals: _DecodeSignals,
    ):
        super().__init__()
        self._key = key
        self._source = source
        self._image_bytes = image_bytes
        self._loader = loader
        self._icon_size = icon_size
        self._signals = signals

    def run(self) -> None:
        image_bytes = self._image_bytes
        if image_bytes is None and self._loader is not None:
            try:
                image_bytes = self._loader()
            except Exception:  # noqa: BLE001 - a bad file shows a blank icon, never crashes the pool
                image_bytes = None
        self._signals.ready.emit(self._key, self._source, decode_scaled(image_bytes, self._icon_size))


def _same_source(cached_source: object, source: object) -> bool:
    """Identity for raw bytes (never re-compare potentially large image
    data on every rebuild), equality for a small version key such as
    (path, mtime)."""
    if cached_source is source:
        return True
    if isinstance(source, (bytes, bytearray)):
        return False
    try:
        return bool(cached_source == source)
    except Exception:  # noqa: BLE001 - an odd __eq__ just means "changed"
        return False


class AsyncIconCache(QObject):
    """`icon_ready(key, icon)` fires on the main thread for every
    request() call, whether it was a blank-image shortcut (fires
    synchronously, immediately) or needed a background decode (fires
    later, whenever that finishes) -- callers that only care about the
    miss case can call get_cached_icon() first and skip request()
    entirely on a hit, as the module docstring's example does.

    A request for a (key, source) already being decoded is dropped
    rather than queued twice -- epub's lazy loading re-requests every
    visible row after each scroll, which used to queue duplicate decodes
    for rows still in flight."""

    icon_ready = pyqtSignal(object, QIcon)  # (key, icon)

    def __init__(self, icon_size: QSize, parent=None):
        super().__init__(parent)
        self._icon_size = icon_size
        self._cache = IdentityWeakDict()
        self._in_flight = IdentityWeakDict()
        self._signals = _DecodeSignals()
        self._signals.ready.connect(self._on_decoded)

    @property
    def icon_size(self) -> QSize:
        return self._icon_size

    def get_cached_icon(self, key: object, source: object) -> QIcon | None:
        """Returns the cached icon for `key` if `source` -- whatever
        identifies "has this item's image changed": the raw image bytes
        themselves (compared by identity, `is`), or a small version key
        such as (path, mtime) (compared with ==) -- still matches what it
        was last computed from. None on a cache miss; the caller should
        show a placeholder and call request()."""
        cached = self._cache.get(key)
        if cached is not None and _same_source(cached[0], source):
            return cached[1]
        return None

    def is_pending(self, key: object, source: object) -> bool:
        pending = self._in_flight.get(key)
        return pending is not None and _same_source(pending[0], source)

    def request(
        self,
        key: object,
        source: object,
        image_bytes: bytes | None = None,
        loader: ImageLoader | None = None,
    ) -> None:
        """Kicks off a background decode for a cache miss. Pass either
        `image_bytes` (already in memory) or `loader` (called on the
        worker thread to fetch them, e.g. reading a cover out of an
        archive). With neither, resolves immediately to a blank icon --
        no thread-pool round trip for the common "nothing to show" case.
        `key` must be weakly referenceable (a plain model object is
        fine, hashable or not; a bare str/int/tuple is not)."""
        if not image_bytes and loader is None:
            icon = QIcon()
            self._cache[key] = (source, icon)
            self._in_flight.pop(key, None)
            self.icon_ready.emit(key, icon)
            return
        if self.is_pending(key, source):
            return
        self._in_flight[key] = (source,)
        QThreadPool.globalInstance().start(
            _DecodeTask(key, source, image_bytes or None, loader, self._icon_size, self._signals)
        )

    def _on_decoded(self, key: object, source: object, image: QImage) -> None:
        pending = self._in_flight.get(key)
        if pending is not None and _same_source(pending[0], source):
            del self._in_flight[key]
        icon = QIcon(QPixmap.fromImage(image)) if not image.isNull() else QIcon()
        self._cache[key] = (source, icon)
        self.icon_ready.emit(key, icon)

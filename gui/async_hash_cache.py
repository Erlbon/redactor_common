"""
redactor_common/gui/async_hash_cache.py

Caches a computed SHA-256 hex digest per item (keyed by the item
itself, by identity (IdentityWeakDict) -- entries drop automatically once an
item is no longer referenced elsewhere, no manual cleanup needed), and
hashes a cache MISS off the main thread via QThreadPool, so a table
full of these never blocks on cryptographic hashing: a row shows up
immediately (its hash-dependent cell left in a neutral state until the
real value arrives), and the real digest arrives independently
whenever its own background hash finishes -- updating ONLY that one
cell, no rebuild, no re-layout, no other side effect.

Same shape as async_icon_cache.py (companion module, same promotion
history) -- split into its own module rather than folded into that one
since not every consumer of a cached icon also wants a hash, and vice
versa, and the two have no shared state.

Promoted from epub's Junk Cover column: identifying a book's cover as
"junk" (shared byte-for-byte with an already-flagged one) means
SHA-256-hashing the full cover image, once per book, every table
rebuild -- for a large library (many thousands of books) with real
cover art, that's several real seconds of pure hashing on the UI
thread, discovered profiling a reported "Updating list is still slow"
regression after the equally-real O(n^2) row-lookup bug in the same
area had already been fixed. Caching by cover_bytes identity (see
EpubBook.cover_hash) only helps a REPEAT rebuild with the same covers,
not the first one -- moving the hash itself off-thread is what
actually keeps the first rebuild of a huge library responsive too.

Usage:
    self._hash_cache = AsyncHashCache(parent=self)
    self._hash_cache.hash_ready.connect(self._on_cover_hash_ready)

    def _update_junk_cell(self, item, model_obj):
        data = model_obj.cover_bytes
        if not data:
            item.setText("")
            return
        cached = self._hash_cache.get_cached_hash(model_obj, data)
        if cached is not None:
            item.setText("Junk" if cached in self._junk_hashes else "")
            return
        item.setText("")  # neutral until the background hash finishes
        self._hash_cache.request(model_obj, data, data)

    def _on_cover_hash_ready(self, model_obj, digest):
        # Find model_obj's CURRENT row, same "re-derive, don't trust a
        # captured row number" reasoning as async_icon_cache.py.
        for row in range(self.table.rowCount()):
            item = self.table.item(row, SOME_COL)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) is model_obj:
                item.setText("Junk" if digest in self._junk_hashes else "")
                break
"""

from __future__ import annotations

import hashlib
from redactor_common.gui.async_icon_cache import IdentityWeakDict

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal


class _HashSignals(QObject):
    ready = pyqtSignal(object, object, str)  # key, source, digest ("" for no data)


class _HashTask(QRunnable):
    def __init__(self, key: object, source: object, data: bytes, signals: _HashSignals):
        super().__init__()
        self._key = key
        self._source = source
        self._data = data
        self._signals = signals

    def run(self) -> None:
        digest = hashlib.sha256(self._data).hexdigest()
        self._signals.ready.emit(self._key, self._source, digest)


class AsyncHashCache(QObject):
    """`hash_ready(key, digest)` fires on the main thread for every
    request() call, whether it was a cache hit (fires synchronously,
    immediately) or needed a background hash (fires later, whenever
    that finishes). `digest` is "" for a blank/no-data key -- callers
    that only care about the miss case can just as easily call
    get_cached_hash() first and skip request() entirely on a hit, as
    the module docstring's example does."""

    hash_ready = pyqtSignal(object, str)  # (key, digest)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cache = IdentityWeakDict()
        self._signals = _HashSignals()
        self._signals.ready.connect(self._on_hashed)

    def get_cached_hash(self, key: object, source: object) -> str | None:
        """Returns the cached digest for `key` if `source` -- whatever
        identifies "has this item's data changed", e.g. the raw bytes
        themselves, compared by identity (`is`), not content -- still
        matches what it was last computed from. None on a cache miss;
        the caller should leave the dependent cell in a neutral state
        and call request()."""
        cached = self._cache.get(key)
        if cached is not None and cached[0] is source:
            return cached[1]
        return None

    def request(self, key: object, source: object, data: bytes | None) -> None:
        """Kicks off a background hash for a cache miss (or resolves
        immediately, synchronously, to "" for blank/no-data -- no
        thread-pool round-trip for the common "nothing to hash" case).
        `key` must be weakly-referenceable (a plain model object is
        fine; a bare str/int/tuple is not)."""
        if not data:
            self._cache[key] = (source, "")
            self.hash_ready.emit(key, "")
            return
        QThreadPool.globalInstance().start(_HashTask(key, source, data, self._signals))

    def _on_hashed(self, key: object, source: object, digest: str) -> None:
        self._cache[key] = (source, digest)
        self.hash_ready.emit(key, digest)

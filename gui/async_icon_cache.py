"""
redactor_common/gui/async_icon_cache.py

Caches a computed QIcon per item (keyed by the item itself, via a
WeakKeyDictionary -- entries drop automatically once an item is no
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

from weakref import WeakKeyDictionary

from PyQt6.QtCore import QBuffer, QIODevice, QObject, QRunnable, QSize, Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import QIcon, QImage, QImageReader, QPixmap


class _DecodeSignals(QObject):
    ready = pyqtSignal(object, object, QImage)  # key, source, scaled_image


class _DecodeTask(QRunnable):
    def __init__(self, key: object, source: object, image_bytes: bytes, icon_size: QSize, signals: _DecodeSignals):
        super().__init__()
        self._key = key
        self._source = source
        self._image_bytes = image_bytes
        self._icon_size = icon_size
        self._signals = signals

    def run(self) -> None:
        # QImageReader.setScaledSize() lets the format's own decoder
        # downscale WHILE decoding (JPEG in particular supports fast
        # DCT-domain downscaling) instead of decoding at full resolution
        # and only then scaling that -- measured 2.67x faster for a
        # real ~600x900 JPEG cover down to a 48x64 icon (4.38ms ->
        # 1.64ms per cover), which matters at the scale this runs at:
        # once per book, every table rebuild. setAutoTransform(True)
        # applies EXIF orientation the same way QImage.fromData() does
        # by default, so a photographed/scanned cover with rotation
        # metadata still comes out upright.
        buffer = QBuffer()
        buffer.setData(self._image_bytes)
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
        reader.setAutoTransform(True)
        original_size = reader.size()
        if original_size.isValid() and not original_size.isEmpty():
            reader.setScaledSize(
                original_size.scaled(self._icon_size, Qt.AspectRatioMode.KeepAspectRatio)
            )
            image = reader.read()
        else:
            # A handful of formats (or corrupt/unusual data) don't
            # report a valid size upfront, so setScaledSize() has
            # nothing to scale against -- fall back to the slower but
            # more tolerant full-decode-then-scale path rather than
            # silently producing a blank icon for a file that's
            # actually fine.
            image = QImage()
        if image.isNull():
            image = QImage.fromData(self._image_bytes)
            if not image.isNull():
                image = image.scaled(
                    self._icon_size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
        self._signals.ready.emit(self._key, self._source, image)


class AsyncIconCache(QObject):
    """`icon_ready(key, icon)` fires on the main thread for every
    request() call, whether it was a cache hit (fires synchronously,
    immediately) or needed a background decode (fires later, whenever
    that finishes) -- callers that only care about the miss case can
    just as easily call get_cached_icon() first and skip request()
    entirely on a hit, as the module docstring's example does."""

    icon_ready = pyqtSignal(object, QIcon)  # (key, icon)

    def __init__(self, icon_size: QSize, parent=None):
        super().__init__(parent)
        self._icon_size = icon_size
        self._cache: WeakKeyDictionary = WeakKeyDictionary()
        self._signals = _DecodeSignals()
        self._signals.ready.connect(self._on_decoded)

    def get_cached_icon(self, key: object, source: object) -> QIcon | None:
        """Returns the cached icon for `key` if `source` -- whatever
        identifies "has this item's image changed", e.g. the raw image
        bytes themselves, compared by identity (`is`), not content --
        still matches what it was last computed from. None on a cache
        miss; the caller should show a placeholder and call request()."""
        cached = self._cache.get(key)
        if cached is not None and cached[0] is source:
            return cached[1]
        return None

    def request(self, key: object, source: object, image_bytes: bytes | None) -> None:
        """Kicks off a background decode for a cache miss (or resolves
        immediately, synchronously, for a blank/no-image key -- no
        thread-pool round-trip for the common "nothing to show" case).
        `key` must be weakly-referenceable (a plain model object is
        fine; a bare str/int/tuple is not)."""
        if not image_bytes:
            icon = QIcon()
            self._cache[key] = (source, icon)
            self.icon_ready.emit(key, icon)
            return
        QThreadPool.globalInstance().start(
            _DecodeTask(key, source, image_bytes, self._icon_size, self._signals)
        )

    def _on_decoded(self, key: object, source: object, image: QImage) -> None:
        icon = QIcon(QPixmap.fromImage(image)) if not image.isNull() else QIcon()
        self._cache[key] = (source, icon)
        self.icon_ready.emit(key, icon)

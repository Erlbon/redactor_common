"""
redactor_common/gui/image_decode.py

decode_scaled(): decode image bytes straight to a target size. Split out
of async_icon_cache.py so the single-image previews (cbz's cover panel,
video's thumbnail) get the same fast path as the table icons.

QImageReader.setScaledSize() lets the format's own decoder downscale
WHILE decoding (JPEG supports fast DCT-domain downscaling) instead of
decoding at full resolution and only then scaling -- measured 2.67x
faster for a ~600x900 JPEG cover (2026-09-17), and far more for a
3000x4500 comic page. setAutoTransform(True) applies EXIF orientation,
so a rotated scan still comes out upright.

Safe to call from a worker thread: QImage (unlike QPixmap) is not tied
to the GUI thread. Convert to QPixmap only on the main thread.
"""

from __future__ import annotations

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt
from PyQt6.QtGui import QImage, QImageReader


def decode_scaled(image_bytes: bytes | None, target_size: QSize | None) -> QImage:
    """Decodes `image_bytes` to fit within `target_size` (aspect ratio
    kept; never upscaled). `target_size=None` decodes at full size.
    Returns a null QImage for empty or undecodable data, never raises."""
    if not image_bytes:
        return QImage()
    buffer = QBuffer()
    buffer.setData(QByteArray(image_bytes))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    reader.setAutoTransform(True)
    original_size = reader.size()
    if target_size is not None and original_size.isValid() and not original_size.isEmpty():
        if original_size.width() > target_size.width() or original_size.height() > target_size.height():
            reader.setScaledSize(
                original_size.scaled(target_size, Qt.AspectRatioMode.KeepAspectRatio)
            )
    image = reader.read()
    if image.isNull():
        # A handful of formats (or unusual data) don't report a size up
        # front -- fall back to the slower but more tolerant path rather
        # than showing a blank image for a file that's actually fine.
        image = QImage.fromData(image_bytes)
        if not image.isNull() and target_size is not None and (
            image.width() > target_size.width() or image.height() > target_size.height()
        ):
            image = image.scaled(
                target_size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
    return image

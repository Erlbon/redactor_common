"""
redactor_common/gui/image_label.py

Promoted from epub's tag_panel.py -- a QLabel that keeps hold of its
source pixmap and rescales it to fit whatever space it's given
(preserving aspect ratio) every time it's resized. Plain QLabel.
setPixmap() shows a pixmap at a fixed size and never rescales it again
on its own; this is what lets an image preview grow or shrink to fill
the available space as its containing panel is resized, instead of
staying locked to whatever size it first loaded at.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QLabel


class AspectRatioImageLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._original_pixmap: QPixmap | None = None

    def set_original_pixmap(self, pixmap: QPixmap | None) -> None:
        self._original_pixmap = pixmap if pixmap and not pixmap.isNull() else None
        self._rescale()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override signature
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        if self._original_pixmap is None:
            super().setPixmap(QPixmap())
            return
        scaled = self._original_pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        super().setPixmap(scaled)

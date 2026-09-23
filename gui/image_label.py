"""
redactor_common/gui/image_label.py

Promoted from epub's tag_panel.py -- a QLabel that keeps hold of its
source pixmap and rescales it to fit whatever space it's given
(preserving aspect ratio) every time it's resized. Plain QLabel.
setPixmap() shows a pixmap at a fixed size and never rescales it again
on its own; this is what lets an image preview grow or shrink to fill
the available space as its containing panel is resized, instead of
staying locked to whatever size it first loaded at.

Its size hints deliberately ignore the pixmap: a plain QLabel reports
its current pixmap's size as its sizeHint()/minimumSizeHint(), so once
an image has been scaled UP to fill a big pane, that pane can no longer
be dragged smaller again (the label insists on its current size) --
"grows but never shrinks". Callers wanting a floor set it explicitly
with setMinimumSize(). See image_pane.py for the resizable panel these
normally live in.
"""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QLabel, QSizePolicy

DEFAULT_SIZE_HINT = QSize(200, 200)


class AspectRatioImageLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._original_pixmap: QPixmap | None = None
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override signature
        return DEFAULT_SIZE_HINT

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override signature
        return QSize(1, 1)

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

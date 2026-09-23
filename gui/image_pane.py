"""
redactor_common/gui/image_pane.py

The resizable image area every Redactor side panel has -- epub's and
mp3's cover, cbz's first-page cover, video's thumbnail preview -- built
once instead of four times (2026-09-23; epub, video and cbz each had
their own copy of the same splitter, and mp3's cover started out as a
fixed-height box).

- ImagePreviewBox: a titled group holding an AspectRatioImageLabel that
  fills whatever space it gets (keeping the aspect ratio, rescaling as
  it's resized), plus room for a caption and buttons underneath. Holds
  a message instead ("No cover", "Loading...") when there's no image.
- ImagePanelSplitter: a vertical splitter with the panel's fields on top
  and the ImagePreviewBox below, and a draggable divider between them,
  so the image can be made as big (or small) as wanted. Either pane can
  be dragged all the way closed.

Two details every copy had to rediscover:
- The fields are wrapped in a scroll area, so dragging the divider up
  scrolls them instead of squashing them.
- The image box is wrapped in a frameless scroll area too. Not for
  scrolling: a QGroupBox's minimum width includes its TITLE's width, and
  a splitter pane can't go below its minimum -- cbz found that floor
  (~254 px for "Cover (First Page)") silently stopped its side panel
  from collapsing. A scroll area's own minimum stays small whatever it
  contains.

Usage:
    self.cover = ImagePreviewBox("Cover", placeholder="No cover")
    self.cover.add_widget(caption_label)
    self.cover.add_layout(button_row)
    splitter = ImagePanelSplitter(fields_widget, self.cover)
    outer.addWidget(splitter, 1)
    ...
    self.cover.show_pixmap(pixmap)  /  self.cover.show_message("No cover")
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import QGroupBox, QLayout, QScrollArea, QSplitter, QVBoxLayout, QWidget

from redactor_common.gui.image_label import AspectRatioImageLabel

DEFAULT_SPLIT = (600, 300)  # a starting hint only; the stretch factors share any extra space
IMAGE_STYLE = "border: 1px solid palette(mid); border-radius: 4px; color: gray;"


class ImagePreviewBox(QGroupBox):
    def __init__(
        self,
        title: str,
        placeholder: str = "",
        minimum_size: tuple[int, int] = (60, 60),
        parent: QWidget | None = None,
    ):
        super().__init__(title, parent)
        self._layout = QVBoxLayout(self)
        self.image_label = AspectRatioImageLabel()
        self.image_label.setMinimumSize(*minimum_size)
        self.image_label.setStyleSheet(IMAGE_STYLE)
        self._layout.addWidget(self.image_label, 1)
        self.show_message(placeholder)

    def add_widget(self, widget: QWidget) -> None:
        """A caption, status label, ... below the image (not stretched)."""
        self._layout.addWidget(widget)

    def add_layout(self, layout: QLayout) -> None:
        """A button row, ... below the image."""
        self._layout.addLayout(layout)

    def show_message(self, text: str) -> None:
        self.image_label.set_original_pixmap(None)
        self.image_label.setText(text)

    def show_pixmap(self, pixmap: QPixmap | None, empty_text: str = "No image") -> None:
        if pixmap is None or pixmap.isNull():
            self.show_message(empty_text)
            return
        self.image_label.setText("")
        self.image_label.set_original_pixmap(pixmap)

    def show_image(self, image: QImage | None, empty_text: str = "No image") -> None:
        """From a QImage (e.g. decoded off the GUI thread by
        async_preview.AsyncPreviewLoader) -- converted to a pixmap here,
        on the GUI thread, where that's allowed."""
        self.show_pixmap(QPixmap.fromImage(image) if image is not None and not image.isNull() else None, empty_text)

    def has_image(self) -> bool:
        return self.image_label._original_pixmap is not None


def _frameless_scroll(widget: QWidget) -> QScrollArea:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    scroll.setWidget(widget)
    return scroll


class ImagePanelSplitter(QSplitter):
    """Fields on top, image below, draggable divider between. `content`
    is wrapped in a scroll area unless it already is one."""

    def __init__(
        self,
        content: QWidget,
        image_box: QWidget,
        initial_sizes: tuple[int, int] = DEFAULT_SPLIT,
        parent: QWidget | None = None,
    ):
        super().__init__(Qt.Orientation.Vertical, parent)
        self.content_container = content if isinstance(content, QScrollArea) else _frameless_scroll(content)
        self.image_container = _frameless_scroll(image_box)
        self.addWidget(self.content_container)
        self.addWidget(self.image_container)
        self.setStretchFactor(0, 1)
        self.setStretchFactor(1, 1)
        self.setSizes(list(initial_sizes))

    def set_image_visible(self, visible: bool) -> None:
        """Hide/show the whole image pane (the scroll wrapper, not just
        the box, so no empty viewport is left taking up space) -- e.g.
        cbz hides its single-file cover while editing several files."""
        self.image_container.setVisible(visible)

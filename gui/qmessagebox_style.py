"""
redactor_common/gui/qmessagebox_style.py

QMessageBox sizes itself to fit its text, but a long line with no
natural wrap point (a path, an error string, a URL) can stretch it far
wider than the screen. One app-level stylesheet rule fixes every call
site at once -- see epub project v29, where this was originally
discovered fixing 51 separate Calibre error dialogs in one shot.

The cap has a side effect this module also handles: when a box's
buttons make it WIDER than the capped text, the text's grid column gets
the spare width and Qt places the (narrower) label at the far end of
it -- the text then sits in a strip far right of the icon (seen on
Linux in videoredactor's missing-tools box, 2026-09-29). So the rule
caps only the text labels (not the icon's), and a small event filter
keeps the text beside the icon whenever a box is shown: the spare width
goes to the text's column, the labels hug its left edge and widen to
fill it (up to the cap), so the text isn't squeezed into a narrow,
clipped strip either.

Usage, once at startup (in main.py, right after creating QApplication):
    from redactor_common.gui.qmessagebox_style import apply_message_box_style
    app = QApplication(sys.argv)
    apply_message_box_style(app)
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, Qt, QTimer
from PyQt6.QtWidgets import QApplication, QLabel, QMessageBox

DEFAULT_MAX_WIDTH_PX = 480

# Qt's own object names for a QMessageBox's text labels.
_TEXT_LABEL_NAMES = ("qt_msgbox_label", "qt_msgbox_informativelabel")
_TEXT_LABELS = ", ".join(f"QMessageBox QLabel#{name}" for name in _TEXT_LABEL_NAMES)


def _text_labels(box: QMessageBox) -> list[QLabel]:
    return [label for name in _TEXT_LABEL_NAMES if (label := box.findChild(QLabel, name)) is not None]


class _KeepTextBesideIcon(QObject):
    """On every QMessageBox show: spare width goes to the text's column,
    the text labels hug its left edge, and -- once the box has sized
    itself around its buttons -- they widen to fill it (up to the cap)
    so the text isn't wrapped into a narrow strip and clipped."""

    def __init__(self, parent, max_width_px: int):
        super().__init__(parent)
        self._max_width_px = max_width_px

    def eventFilter(self, watched, event):  # noqa: N802 -- Qt's name
        if event.type() == QEvent.Type.Show and isinstance(watched, QMessageBox):
            grid = watched.layout()
            labels = _text_labels(watched)
            if labels and hasattr(grid, "getItemPosition"):
                _row, column, _rows, _columns = grid.getItemPosition(grid.indexOf(labels[0]))
                grid.setColumnStretch(column, 1)
                for label in labels:
                    grid.setAlignment(label, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
                QTimer.singleShot(0, lambda box=watched: self._fill(box))
        return False

    def _fill(self, box: QMessageBox) -> None:
        try:
            right_edge = box.width() - box.layout().contentsMargins().right()
        except RuntimeError:  # the box was already closed and deleted
            return
        changed = False
        for label in _text_labels(box):
            if not label.isVisible():
                continue
            width = max(label.width(), min(self._max_width_px, right_edge - label.x()))
            # A left-pinned label isn't given its wrapped height by the grid
            # (Qt skips height-for-width for aligned items): set both.
            height = label.heightForWidth(width)
            if width != label.minimumWidth() or height > label.height():
                label.setMinimumWidth(width)
                label.setMinimumHeight(max(height, 0))
                changed = True
        if changed:
            box.layout().activate()
            box.adjustSize()


def apply_message_box_style(app: QApplication, max_width_px: int = DEFAULT_MAX_WIDTH_PX) -> None:
    existing = app.styleSheet()
    rule = f"{_TEXT_LABELS} {{ max-width: {max_width_px}px; }}"
    app.setStyleSheet(f"{existing}\n{rule}" if existing else rule)
    if not hasattr(app, "_redactor_msgbox_filter"):
        app._redactor_msgbox_filter = _KeepTextBesideIcon(app, max_width_px)
        app.installEventFilter(app._redactor_msgbox_filter)

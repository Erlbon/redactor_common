"""
redactor_common/gui/remembered_layout.py

A dialog that remembers its own size, and a results-over-detail QSplitter that remembers where the owner put the
handle (owner request 2026-10-17: the search result boxes must be resizable against each other).

    splitter = results_detail_splitter(table, detail_box)          # vertical, visible handle, 60/40 to start
    layout.addWidget(splitter, 1)
    remember_layout(dialog, "scene_search", get_setting_fn, set_setting_fn, splitter)

The two setting callables are the app's own (section, key[, value]) accessors, so the values land in the app's
settings file. Keys written: "<name>_size" ("820x600") and "<name>_split" ("60,40", the shares of the splitter).
Garbage in a setting is ignored. The layout is saved when the dialog closes (accepted, rejected or closed).
"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QSplitter, QWidget

DEFAULT_SHARES = (60, 40)
SECTION = "dialog_layout"


def results_detail_splitter(results: QWidget, detail: QWidget,
                            shares: tuple[int, int] = DEFAULT_SHARES) -> QSplitter:
    """A vertical splitter, `results` over `detail`, with a handle wide enough to see and grab, neither pane
    collapsible to nothing, sized `shares` (60/40) until a remembered layout replaces it."""
    detail.setMaximumHeight(16777215)  # a fixed cap would defeat the splitter
    splitter = QSplitter(Qt.Orientation.Vertical)
    splitter.setChildrenCollapsible(False)
    splitter.setHandleWidth(8)
    splitter.setStyleSheet("QSplitter::handle { background: palette(mid); margin: 2px 40px; border-radius: 2px; }")
    splitter.setToolTip("Drag the bar to give the results or the details more room.")
    splitter.addWidget(results)
    splitter.addWidget(detail)
    splitter.setStretchFactor(0, shares[0])
    splitter.setStretchFactor(1, shares[1])
    splitter.setSizes(list(shares))  # shares until it is laid out; Qt scales them to the real height
    return splitter


def _parse_ints(text: str, count: int, sep: str) -> Optional[list[int]]:
    try:
        values = [int(part) for part in str(text).split(sep)]
    except (ValueError, TypeError):
        return None
    return values if len(values) == count and all(v > 0 for v in values) else None


def remember_layout(dialog: QDialog, name: str, get: Callable[[str, str, str], str],
                    put: Callable[[str, str, str], None], splitter: Optional[QSplitter] = None) -> None:
    """Restore the dialog's size (and the splitter's shares) from the settings now, and save both when it closes."""
    size = _parse_ints(get(SECTION, f"{name}_size", ""), 2, "x")
    if size is not None:
        dialog.resize(size[0], size[1])
    if splitter is not None:
        shares = _parse_ints(get(SECTION, f"{name}_split", ""), 2, ",")
        if shares is not None:
            splitter.setSizes(shares)

    def save(*_args) -> None:
        put(SECTION, f"{name}_size", f"{dialog.width()}x{dialog.height()}")
        if splitter is not None:
            sizes = splitter.sizes()
            if len(sizes) == 2 and all(s > 0 for s in sizes):
                put(SECTION, f"{name}_split", f"{sizes[0]},{sizes[1]}")

    dialog.finished.connect(save)

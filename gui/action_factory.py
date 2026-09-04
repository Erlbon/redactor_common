"""
redactor_common/gui/action_factory.py

Small helper so menu items and toolbar buttons that need the same
action can share a single QAction instance -- keeps enabled state,
text, tooltips etc. automatically in sync between the two, rather than
needing to duplicate and separately maintain them.

Lifted from the epub project's gui/main_window.py::_make_action(),
which the video project never had (it builds QAction objects by hand
at every call site instead).
"""

from __future__ import annotations

from typing import Callable, Iterable

from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import QWidget


def make_action(
    parent: QWidget,
    text: str,
    slot: Callable[[], None],
    shortcut: str | QKeySequence.StandardKey | None = None,
    shortcuts: Iterable[str] | None = None,
    tooltip: str | None = None,
    checkable: bool = False,
) -> QAction:
    """Build a QAction wired to `slot`, with an optional shortcut (or,
    for an action that should respond to more than one key sequence,
    `shortcuts` instead). `parent` is the QAction's Qt parent (typically
    the window that owns it) and doesn't need to match `slot`'s owner.
    """
    act = QAction(text, parent)
    if shortcuts:
        act.setShortcuts([QKeySequence(s) for s in shortcuts])
    elif shortcut:
        act.setShortcut(QKeySequence(shortcut))
    if tooltip:
        act.setToolTip(tooltip)
    if checkable:
        act.setCheckable(True)
    act.triggered.connect(slot)
    return act

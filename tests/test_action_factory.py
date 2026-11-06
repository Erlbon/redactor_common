"""make_action(): a non-checkable action calls its slot with no arguments.

PyQt6 hands QAction.triggered's `checked` bool to any slot that can take it, so
a slot like `def add_links(self, text=None)` got text=False and crashed (Accipio
File > Add Links, 2026-11-06). Checkable actions still receive `checked`.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMainWindow  # noqa: E402

from redactor_common.gui.action_factory import make_action  # noqa: E402
from redactor_common.gui.menu_builder import MenuAction, populate_menu  # noqa: E402

_app = QApplication.instance() or QApplication([])


class _Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.calls = []

    def add_links(self, text=None):
        self.calls.append(text)

    def toggle(self, checked):
        self.calls.append(checked)


def test_menu_action_slot_with_optional_first_parameter_keeps_its_default():
    window = _Window()
    menu = window.menuBar().addMenu("&File")
    actions = {}
    populate_menu(window, menu, [MenuAction("add", "&Add Links...", window.add_links)], actions)
    actions["add"].trigger()
    assert window.calls == [None]


def test_plain_action_calls_slot_without_arguments():
    window = _Window()
    act = make_action(window, "Add", window.add_links)
    act.trigger()
    assert window.calls == [None]


def test_checkable_action_still_passes_checked():
    window = _Window()
    act = make_action(window, "Toggle", window.toggle, checkable=True)
    act.trigger()
    act.trigger()
    assert window.calls == [True, False]

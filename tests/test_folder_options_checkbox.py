"""gui/folder_options.add_include_subfolders_checkbox: the toolbar checkbox mirrors the File > Include Subfolders action."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QAction  # noqa: E402
from PyQt6.QtWidgets import QApplication, QCheckBox, QMainWindow, QToolBar  # noqa: E402

from redactor_common.gui.folder_options import add_include_subfolders_checkbox, attach_include_subfolders  # noqa: E402

_app = QApplication.instance() or QApplication([])


def _setup(initial: bool):
    window = QMainWindow()
    toolbar = QToolBar()
    window.addToolBar(toolbar)
    action = QAction("Include Subfolders", window)
    action.setCheckable(True)
    saved = []
    attach_include_subfolders(action, load=lambda: initial, save=saved.append)
    box = add_include_subfolders_checkbox(toolbar, action)
    window.show()
    return window, toolbar, action, box, saved


def test_checkbox_starts_with_the_saved_value_and_is_on_the_toolbar():
    for initial in (True, False):
        window, toolbar, action, box, saved = _setup(initial)
        assert box.isChecked() is initial and action.isChecked() is initial
        assert box in toolbar.findChildren(QCheckBox) and box.isVisible()
        assert saved == []  # loading the value does not save it back
        window.close()


def test_ticking_the_checkbox_updates_the_action_and_saves_once():
    window, _toolbar, action, box, saved = _setup(True)
    box.setChecked(False)
    assert action.isChecked() is False and saved == [False]
    box.setChecked(True)
    assert action.isChecked() is True and saved == [False, True]
    window.close()


def test_ticking_the_menu_action_updates_the_checkbox_and_saves_once():
    window, _toolbar, action, box, saved = _setup(True)
    action.setChecked(False)
    assert box.isChecked() is False and saved == [False]
    action.trigger()  # a click on the menu item toggles a checkable action
    assert box.isChecked() is True and saved == [False, True]
    window.close()


def test_checkbox_follows_the_actions_enabled_state():
    window, _toolbar, action, box, _saved = _setup(True)
    action.setEnabled(False)
    assert box.isEnabled() is False
    action.setEnabled(True)
    assert box.isEnabled() is True
    window.close()

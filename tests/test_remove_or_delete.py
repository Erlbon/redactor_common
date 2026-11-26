"""The Delete key in an app with Delete Files asks Remove from List / Delete Files / Cancel
(gui/remove_or_delete.py, 2026-11-26), and the menu skeleton gives Remove from List no shortcut there."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QKeySequence  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMainWindow, QMessageBox  # noqa: E402

from redactor_common.core import labels  # noqa: E402
from redactor_common.gui import standard_shortcuts as keys  # noqa: E402
from redactor_common.gui.menu_lint import lint_menu_bar  # noqa: E402
from redactor_common.gui.remove_or_delete import (  # noqa: E402
    DELETE,
    PREVIEW_LIMIT,
    REMOVE,
    ask_remove_or_delete,
    remove_or_delete_text,
)
from redactor_common.gui.standard_menus import (  # noqa: E402
    StandardMenuSpec,
    build_standard_menu_bar,
    get_action_registry,
    remove_or_delete_action,
    standard_file_items,
    standard_help_items,
    standard_tools_items,
)

_app = QApplication.instance() or QApplication([])


def _noop():
    pass


def _click(text_start):
    """An exec_box that clicks the button whose text starts with `text_start` (None = close with no click)."""
    def run(box: QMessageBox):
        if text_start is None:
            return
        for b in box.buttons():
            if b.text().replace("&", "").startswith(text_start):
                b.click()
                return
        raise AssertionError(f"no button {text_start!r} in {[b.text() for b in box.buttons()]}")
    return run


def test_each_button_gives_its_answer():
    assert ask_remove_or_delete(None, 2, exec_box=_click("Remove from List")) == REMOVE
    assert ask_remove_or_delete(None, 2, exec_box=_click("Delete Files")) == DELETE
    assert ask_remove_or_delete(None, 2, exec_box=_click("Cancel")) is None
    assert ask_remove_or_delete(None, 2, exec_box=_click(None)) is None


def test_remove_from_list_is_the_default_and_cancel_the_escape():
    seen = {}

    def look(box: QMessageBox):
        seen["default"] = box.defaultButton().text().replace("&", "")
        seen["escape"] = box.escapeButton()
        seen["cancel"] = box.button(QMessageBox.StandardButton.Cancel)
        seen["title"] = box.windowTitle()

    ask_remove_or_delete(None, 1, exec_box=look)
    assert seen["default"] == "Remove from List"
    assert seen["escape"] is seen["cancel"] is not None
    assert seen["title"] == "Remove or Delete?"


def test_text_counts_lists_names_and_says_what_each_choice_does():
    one = remove_or_delete_text(1, ["a.epub"], noun="book")
    assert "the 1 selected book?" in one and "- a.epub" in one
    assert "The files stay on disk" in one and "Recycle Bin" in one
    names = [f"{i}.epub" for i in range(PREVIEW_LIMIT + 3)]
    many = remove_or_delete_text(len(names), names)
    assert f"the {len(names)} selected files?" in many
    assert "...and 3 more" in many and f"- {PREVIEW_LIMIT}.epub" not in many


def _spec(delete_files):
    return StandardMenuSpec(
        file=standard_file_items(remove_from_list=_noop, delete_files=delete_files, exit_slot=_noop),
        tools=standard_tools_items(columns=_noop),
        help=standard_help_items("Test App", _noop, _noop, _noop),
        hidden=[remove_or_delete_action(_noop)] if delete_files else [],
    )


def test_with_delete_files_the_delete_key_asks_and_remove_from_list_has_no_shortcut():
    window = QMainWindow()
    build_standard_menu_bar(window, _spec(_noop))
    reg = get_action_registry(window)
    assert reg["remove_from_list"].shortcut().isEmpty()
    assert reg["delete_files"].shortcut() == QKeySequence(keys.DELETE_FILES) == QKeySequence("Shift+Delete")
    assert reg["remove_or_delete"].shortcut() == QKeySequence(keys.REMOVE_OR_DELETE) == QKeySequence("Delete")
    assert reg["remove_or_delete"].text() == labels.REMOVE_OR_DELETE
    assert reg.path_of("remove_or_delete") == ""  # in no menu, only the Command Palette
    assert reg["remove_or_delete"] in window.actions()  # so its shortcut fires
    assert not lint_menu_bar(window)


def test_without_delete_files_delete_stays_remove_from_list():
    window = QMainWindow()
    build_standard_menu_bar(window, _spec(None))
    reg = get_action_registry(window)
    assert reg["remove_from_list"].shortcut() == QKeySequence(keys.REMOVE_FROM_LIST) == QKeySequence("Delete")
    assert "remove_or_delete" not in reg and "delete_files" not in reg
    assert not lint_menu_bar(window)

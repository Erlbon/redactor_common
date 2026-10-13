"""gui/folder_options.py (File > Include Subfolders) and gui/progress.run_busy (one slow call under a progress dialog)."""

import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtGui import QAction  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMainWindow  # noqa: E402

from redactor_common.gui.folder_options import attach_include_subfolders, include_subfolders_slot  # noqa: E402
from redactor_common.gui.progress import run_busy  # noqa: E402
from redactor_common.gui.standard_menus import standard_file_items  # noqa: E402

_app = QApplication.instance() or QApplication([])


def test_include_subfolders_is_a_core_checkable_item_under_open_folder():
    items = standard_file_items(open_folder=lambda: None, include_subfolders=include_subfolders_slot())
    keys = [getattr(i, "key", None) for i in items]
    assert keys[:3] == ["open_files", "open_folder", "include_subfolders"]
    item = items[2]
    assert item.checkable and item.enabled and item.text == "Include Su&bfolders"
    greyed = standard_file_items()[2]  # an app that has not wired it yet: present but disabled
    assert greyed.key == "include_subfolders" and not greyed.enabled


def test_attach_ticks_from_the_saved_value_without_saving_it_back_then_saves_changes():
    saved = []
    action = QAction("x")
    action.setCheckable(True)
    attach_include_subfolders(action, load=lambda: True, save=saved.append)
    assert action.isChecked() and saved == []  # loading the value is not a change
    action.setChecked(False)
    action.setChecked(True)
    assert saved == [False, True]
    off = QAction("y")
    off.setCheckable(True)
    attach_include_subfolders(off, load=lambda: False, save=saved.append)
    assert not off.isChecked()


def test_run_busy_returns_the_result_runs_off_the_gui_thread_and_reraises():
    window = QMainWindow()
    main = threading.get_ident()
    seen = {}

    def work(a, b=0):
        seen["thread"] = threading.get_ident()
        return a + b

    assert run_busy(window, "Working...", work, 2, b=3) == 5
    assert seen["thread"] != main

    def boom():
        raise ValueError("bad folder")

    with pytest.raises(ValueError, match="bad folder"):
        run_busy(window, "Working...", boom)

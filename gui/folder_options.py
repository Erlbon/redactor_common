"""
redactor_common/gui/folder_options.py

File > Include Subfolders, the one folder-opening option every Redactor app shares. The menu item comes from
standard_file_items(include_subfolders=...); this module is the app-side half: set the item up from the saved
value and keep the saved value in step with the checkbox. The app stores the boolean wherever its settings live
(default: ON, which is how Open Folder always behaved) and passes `recursive=<value>` to its folder scans.

    standard_file_items(..., include_subfolders=include_subfolders_slot())
    ...
    attach_include_subfolders(actions["include_subfolders"], load=lambda: settings.include_subfolders,
                              save=lambda on: set_include_subfolders(on))
"""

from __future__ import annotations

from typing import Callable


def attach_include_subfolders(action, load: Callable[[], bool], save: Callable[[bool], None]) -> None:
    """After the menu is built: tick `action` from the saved value (without saving it back) and save every later
    change. Safe to call once per window."""
    action.blockSignals(True)
    action.setChecked(bool(load()))
    action.blockSignals(False)
    action.toggled.connect(lambda on: save(bool(on)))


def include_subfolders_slot() -> Callable[[], None]:
    """The no-op slot for standard_file_items(include_subfolders=...): the checkbox itself does the work through
    attach_include_subfolders(); the item only needs a slot to be enabled."""
    return lambda: None

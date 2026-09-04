"""
redactor_common/gui/context_menu.py

Shared right-click context menu for a project's main file table.
Compares the three projects' independently-built versions: epub had
the richest menu (many app-specific actions, plus a selection-fix and
two generic file actions) but no equivalent existed in mp3 or video at
all (mp3 had a bare-bones app-specific-only menu; video had none).
This promotes epub's generic pieces and lets every project layer its
own app-specific actions on top via the same declarative
MenuAction/Separator vocabulary menu_builder.py already uses.

Handles, generically, for any project:
  - Right-clicking outside the current selection replaces it with just
    the row under the cursor first (matches Explorer and most other
    apps, rather than acting on a selection the person can't see
    anymore).
  - Two generic file actions any item with a filesystem path supports:
    "Open Containing Folder" and "Copy Path".
  - Layering a project's own app-specific actions in via `extra_items`,
    given the resolved current selection.

Usage:
    from redactor_common.gui.context_menu import show_table_context_menu
    from redactor_common.gui.menu_builder import MenuAction, Separator

    def _show_table_context_menu(self, pos):
        show_table_context_menu(
            self, self.table, pos,
            get_selected_items=self._currently_selected_books,
            get_path=lambda book: book.path,
            extra_items=lambda books: [
                Separator(),
                MenuAction("rename", "Rename File...", lambda: self.rename_single_file(books[0])),
            ] if len(books) == 1 else [],
        )
"""

from __future__ import annotations

from typing import Any, Callable

from PyQt6.QtWidgets import QApplication, QMenu, QTableWidget, QWidget

from redactor_common.core.os_utils import reveal_in_file_manager
from redactor_common.gui.menu_builder import MenuAction, MenuItems, populate_menu


def show_table_context_menu(
    window: QWidget,
    table: QTableWidget,
    pos,
    get_selected_items: Callable[[], list],
    get_path: Callable[[Any], str],
    extra_items: Callable[[list], MenuItems] | None = None,
) -> None:
    """Full right-click flow for a project's main table. `window` is
    the main window (menu parent, and what make_action() attaches
    shortcuts to via populate_menu() -- see menu_builder.py).
    `get_selected_items`/`get_path` are this project's own accessors,
    same "generalized via accessor callables, not tied to one data
    model" pattern the rest of this package uses.
    """
    row = table.rowAt(pos.y())
    if row >= 0:
        selected_rows = {idx.row() for idx in table.selectedIndexes()}
        if row not in selected_rows:
            table.clearSelection()
            table.selectRow(row)

    items = get_selected_items()
    if not items:
        return

    menu_items: MenuItems = [
        MenuAction("open_containing_folder", "Open Containing Folder", lambda: _open_containing_folder(items, get_path)),
        MenuAction("copy_path", "Copy Path", lambda: _copy_paths(items, get_path)),
    ]
    if extra_items:
        menu_items.extend(extra_items(items))

    menu = QMenu(window)
    populate_menu(window, menu, menu_items)
    menu.exec(table.viewport().mapToGlobal(pos))


def _open_containing_folder(items: list, get_path: Callable[[Any], str]) -> None:
    if not items:
        return
    # Just the first item's folder -- opening a separate Explorer window
    # per selected item would be more annoying than helpful for a
    # multi-selection, and they're usually in the same folder.
    reveal_in_file_manager(get_path(items[0]))


def _copy_paths(items: list, get_path: Callable[[Any], str]) -> None:
    if not items:
        return
    QApplication.clipboard().setText("\n".join(str(get_path(item)) for item in items))

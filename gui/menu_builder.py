"""
redactor_common/gui/menu_builder.py

Declarative menu-bar construction so every "Redactor" project ends up
with the same top-level shape and the same mnemonics for the shared
menus, instead of each main_window.py hand-rolling addMenu/addAction
calls in a slightly different order with slightly different shortcuts.

Standard shape (per the shared UX spec): File, Import, Operations,
Settings, Help -- in that order, always with those mnemonics (&File
etc.). A project can insert its own extra menus (e.g. the epub tool's
"Kobo" menu) at a named position via `extra_menus`; anything project-
specific still gets its own place, it just doesn't get to reorder or
rename the five shared ones.

Usage:
    from redactor_common.gui.menu_builder import MenuSpec, MenuAction, Separator, build_menu_bar

    specs = {
        "File": [
            MenuAction("load_files", "&Load Files...", self.add_files_dialog, shortcut="Ctrl+O"),
            Separator(),
            MenuAction("save", "&Save", self.save_selected, shortcut="Ctrl+S"),
            Separator(),
            MenuAction("exit", "E&xit", self.close, shortcut=QKeySequence.StandardKey.Quit),
        ],
        "Import": [...],
        "Operations": [...],
        "Settings": [...],
        "Help": [
            MenuAction("about", "&About The Widget Redactor", self.open_about_dialog),
            MenuAction("changelog", "View &Changelog", self.open_changelog_dialog),
        ],
    }
    actions = build_menu_bar(self, specs, extra_menus=[("Kobo", 3, kobo_items)])
    # actions["save"] is the QAction, reusable on a toolbar
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import QMainWindow, QMenu

from redactor_common.gui.action_factory import make_action

STANDARD_MENU_ORDER = ["File", "Import", "Operations", "Settings", "Help"]
STANDARD_MNEMONICS = {
    "File": "&File",
    "Import": "&Import",
    "Operations": "&Operations",
    "Settings": "&Settings",
    "Help": "&Help",
}


@dataclass
class MenuAction:
    """One menu item. `key` is how the built QAction is looked up in the
    dict build_menu_bar() returns (e.g. for reuse on a toolbar) -- keep
    it a stable, unique-within-the-window identifier, not display text."""
    key: str
    text: str
    slot: Callable[[], None]
    shortcut: str | QKeySequence.StandardKey | None = None
    shortcuts: Iterable[str] | None = None
    tooltip: str | None = None
    checkable: bool = False


@dataclass
class Separator:
    pass


@dataclass
class Submenu:
    """A nested submenu within a top-level menu (rare -- most menus are flat)."""
    text: str
    items: list = field(default_factory=list)


MenuItems = list  # list[MenuAction | Separator | Submenu | QAction]


def populate_menu(
    window: QMainWindow, menu: QMenu, items: MenuItems, actions_out: dict[str, QAction] | None = None
) -> dict[str, QAction]:
    """Fills an already-created QMenu from a declarative items list --
    the same MenuAction/Separator/Submenu vocabulary build_menu_bar()
    uses for the menu bar. Public (not just an internal build_menu_bar
    helper) so a right-click context menu can be built from the exact
    same declarative shape -- see redactor_common.gui.context_menu and
    redactor_common.gui.column_menu.

    `window` is passed through to make_action() as the QAction's parent
    (needed for shortcuts to fire) -- for a context menu this is still
    the main window, not the menu itself.

    A raw QAction is also accepted directly (added as-is, not wrapped) --
    for reusing an action that already exists elsewhere (menu bar,
    toolbar) in a context menu too, so its enabled/checked state and
    shortcut stay in sync automatically rather than drifting from a
    second, separately-built copy.
    """
    if actions_out is None:
        actions_out = {}
    for item in items:
        if isinstance(item, QAction):
            menu.addAction(item)
        elif isinstance(item, Separator):
            menu.addSeparator()
        elif isinstance(item, Submenu):
            sub = menu.addMenu(item.text)
            populate_menu(window, sub, item.items, actions_out)
        elif isinstance(item, MenuAction):
            act = make_action(
                window, item.text, item.slot,
                shortcut=item.shortcut, shortcuts=item.shortcuts,
                tooltip=item.tooltip, checkable=item.checkable,
            )
            menu.addAction(act)
            actions_out[item.key] = act
        else:
            raise TypeError(f"Unrecognized menu item type: {item!r}")
    return actions_out


def build_menu_bar(
    window: QMainWindow,
    specs: dict[str, MenuItems],
    extra_menus: list[tuple[str, int, MenuItems]] | None = None,
) -> dict[str, QAction]:
    """Builds window.menuBar() from `specs`, a dict keyed by the five
    standard menu names (File/Import/Operations/Settings/Help -- all
    required, even if a project leaves one nearly empty, so the shape
    stays identical across projects). Returns a flat dict of
    key -> QAction covering every action added, for reuse on a toolbar
    or in context menus.

    `extra_menus` inserts project-specific menus (e.g. epub's "Kobo")
    as (menu_text, position, items) tuples, where `position` is the
    0-based index in the final menu bar to insert at -- e.g. position 3
    puts it between Operations and Settings. Project-specific menus
    never sit inside the five standard ones; they get their own slot.
    """
    missing = [name for name in STANDARD_MENU_ORDER if name not in specs]
    if missing:
        raise ValueError(
            f"build_menu_bar requires all five standard menus; missing: {missing}"
        )

    menu_bar = window.menuBar()
    actions: dict[str, QAction] = {}

    order = list(STANDARD_MENU_ORDER)
    for text, position, _items in (extra_menus or []):
        order.insert(position, text)

    extra_by_name = {text: items for text, _pos, items in (extra_menus or [])}

    for name in order:
        mnemonic_text = STANDARD_MNEMONICS.get(name, f"&{name}")
        menu = menu_bar.addMenu(mnemonic_text)
        items = specs.get(name) if name in specs else extra_by_name.get(name, [])
        populate_menu(window, menu, items, actions)

    return actions

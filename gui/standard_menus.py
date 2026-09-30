"""
redactor_common/gui/standard_menus.py

The standard menu SKELETON: every Redactor app's menu bar is
File, Edit, View, <app menus...>, Tools, Help (no Window menu: all four
are single-window apps), with the shared actions in the same places under
the same labels (core/labels.py) and shortcuts (gui/standard_shortcuts.py).
The approved design is in the family's menu-skeleton proposal; README
"Standard menu skeleton" has the short version and the migration order.

This module sits ALONGSIDE gui/menu_builder.build_menu_bar(), which keeps
building the old File/Import/Operations/Settings/Help shape until each app
migrates. It reuses menu_builder's MenuAction / Separator / Submenu
vocabulary, so an app's items lists look the same either way.

Usage:
    spec = StandardMenuSpec(
        file=standard_file_items(open_files=self.add_files, open_folder=self.add_folder,
                                 save=self.save_selected, ..., exit_slot=self.close),
        edit=standard_edit_items(undo=..., redo=..., apply=..., ...),
        view=standard_view_items(show_metadata_panel=..., refresh_list=...),
        app_menus=[AppMenu(labels.MENU_METADATA, [...]), AppMenu(labels.MENU_REPAIR, [...])],
        tools=standard_tools_items(api_keys=..., columns=..., genres=..., languages=...),
        help=standard_help_items("Comic Redactor", changelog_slot, credits_slot, about_slot),
    )
    menus = build_standard_menu_bar(self, spec)   # dict heading -> QMenu
    registry = get_action_registry(self)           # key -> QAction, for toolbar/palette/lint
    add_command_palette(self, registry)            # Ctrl+K

Heading order and the heading-count cap are enforced by lint (gui/menu_lint.
lint_menu_bar, called from each app's test suite), not at runtime: a user
never sees an exception for a cosmetic rule. Structural mistakes that can
only be a coding error (duplicate action key, an app menu named like a
standard one) do raise.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import QMainWindow, QMenu

from redactor_common.core import labels
from redactor_common.gui import standard_shortcuts as keys
from redactor_common.gui.action_factory import make_action
from redactor_common.gui.menu_builder import MenuAction, MenuItems, Separator, Submenu, populate_menu

# Action keys the skeleton tags with a platform role; every other action
# gets NoRole so macOS never moves a "Settings"-like item on its own.
KEY_ABOUT = "about"
KEY_PREFERENCES = "preferences"
KEY_EXIT = "exit"
KEY_COMMAND_PALETTE = "command_palette"
ROLE_BY_KEY = {
    KEY_ABOUT: QAction.MenuRole.AboutRole,
    KEY_PREFERENCES: QAction.MenuRole.PreferencesRole,
    KEY_EXIT: QAction.MenuRole.QuitRole,
}

# Headings the skeleton owns; an app menu may not reuse one of these names.
STANDARD_HEADINGS = ("File", "Edit", "View", "Tools", "Help")
PATH_SEPARATOR = " ▸ "


@dataclass
class AppMenu:
    """An app-specific top-level menu (Metadata, Repair, Send, Analyze, Media...)
    that sits between View and Tools. `title` is a labels.MENU_* constant or
    its own '&'-marked text."""
    title: str
    items: MenuItems = field(default_factory=list)


@dataclass
class StandardMenuSpec:
    """What one app puts under each heading. The heading order is fixed
    (File, Edit, View, *app_menus in the order given, Tools, Help); `hidden`
    are actions registered for the command palette (and their shortcuts)
    that appear in no menu."""
    file: MenuItems = field(default_factory=list)
    edit: MenuItems = field(default_factory=list)
    view: MenuItems = field(default_factory=list)
    app_menus: list[AppMenu] = field(default_factory=list)
    tools: MenuItems = field(default_factory=list)
    help: MenuItems = field(default_factory=list)
    hidden: list[MenuAction] = field(default_factory=list)

    def headings(self) -> list[tuple[str, MenuItems]]:
        """(title, items) in final menu-bar order."""
        return (
            [(labels.MENU_FILE, self.file), (labels.MENU_EDIT, self.edit), (labels.MENU_VIEW, self.view)]
            + [(m.title, m.items) for m in self.app_menus]
            + [(labels.MENU_TOOLS, self.tools), (labels.MENU_HELP, self.help)]
        )


# --- action registry ----------------------------------------------------------


@dataclass
class RegistryEntry:
    key: str
    action: QAction
    path: str  # 'Metadata ▸ Look Up'; '' for a hidden action


class ActionRegistry:
    """key -> QAction for every action the skeleton built, plus each one's
    menu path. Used by the command palette and by lint."""

    def __init__(self) -> None:
        self._actions: dict[str, QAction] = {}
        self._paths: dict[str, str] = {}

    def __contains__(self, key: object) -> bool:
        return key in self._actions

    def __getitem__(self, key: str) -> QAction:
        return self._actions[key]

    def __len__(self) -> int:
        return len(self._actions)

    def keys(self) -> list[str]:
        return list(self._actions)

    def path_of(self, key: str) -> str:
        return self._paths.get(key, "")

    def entries(self) -> list[RegistryEntry]:
        return [RegistryEntry(k, a, self._paths.get(k, "")) for k, a in self._actions.items()]


def get_action_registry(window: QMainWindow) -> ActionRegistry | None:
    """The registry build_standard_menu_bar() attached to `window`."""
    return getattr(window, "action_registry", None)


# --- builder ------------------------------------------------------------------


def _iter_keys(items: Iterable) -> Iterator[str]:
    for item in items:
        if isinstance(item, MenuAction):
            yield item.key
        elif isinstance(item, Submenu):
            yield from _iter_keys(item.items)


def _with_roles(items: Iterable) -> list:
    """Copies `items` with each MenuAction's role resolved: explicit role
    wins, then the key's standard role, else NoRole."""
    out: list = []
    for item in items:
        if isinstance(item, MenuAction):
            role = item.role
            if role is None:
                role = ROLE_BY_KEY.get(item.key, QAction.MenuRole.NoRole)
            out.append(dataclasses.replace(item, role=role))
        elif isinstance(item, Submenu):
            out.append(Submenu(item.text, _with_roles(item.items)))
        else:
            out.append(item)
    return out


def _walk_paths(menu: QMenu, trail: list[str]) -> Iterator[tuple[QAction, str]]:
    for act in menu.actions():
        if act.isSeparator():
            continue
        sub = act.menu()
        if sub is not None:
            yield from _walk_paths(sub, trail + [labels.plain_label(act.text())])
        else:
            yield act, PATH_SEPARATOR.join(trail)


def build_standard_menu_bar(window: QMainWindow, spec: StandardMenuSpec) -> dict[str, QMenu]:
    """Builds window.menuBar() in the skeleton order from `spec` and returns
    the top-level menus keyed by plain heading name ('File', 'Metadata'...).
    Also attaches an ActionRegistry as `window.action_registry` (see
    get_action_registry) and tags menu roles: AboutRole on 'about',
    PreferencesRole on 'preferences', QuitRole on 'exit', NoRole on the rest.

    Raises ValueError for coding mistakes only: a duplicate action key, or
    an app menu that reuses a standard heading name."""
    headings = spec.headings()
    plain = [labels.plain_label(title) for title, _ in headings]
    for m in spec.app_menus:
        if labels.plain_label(m.title) in STANDARD_HEADINGS:
            raise ValueError(f"app menu {m.title!r} reuses a standard heading name")
    if len(set(plain)) != len(plain):
        raise ValueError(f"duplicate top-level menu names: {plain}")

    all_keys = [k for _, items in headings for k in _iter_keys(items)] + [h.key for h in spec.hidden]
    dupes = sorted({k for k in all_keys if all_keys.count(k) > 1})
    if dupes:
        raise ValueError(f"duplicate action keys in menu spec: {dupes}")

    registry = ActionRegistry()
    menu_bar = window.menuBar()
    menus: dict[str, QMenu] = {}
    for (title, items), name in zip(headings, plain):
        menu = menu_bar.addMenu(title)
        populate_menu(window, menu, _with_roles(items), registry._actions)
        menus[name] = menu
        for act, path in _walk_paths(menu, [name]):
            for key, registered in registry._actions.items():
                if registered is act:
                    registry._paths[key] = path
                    break

    for hidden in _with_roles(spec.hidden):
        act = make_action(
            window, hidden.text, hidden.slot, shortcut=hidden.shortcut,
            shortcuts=hidden.shortcuts, tooltip=hidden.tooltip, checkable=hidden.checkable,
        )
        act.setMenuRole(hidden.role)
        act.setEnabled(hidden.enabled)
        if hidden.keywords:
            act.setData(hidden.keywords)
        window.addAction(act)  # a shortcut only fires if the window owns the action
        registry._actions[hidden.key] = act
        registry._paths[hidden.key] = ""

    window.action_registry = registry
    return menus


# --- helper constructors for the standard blocks ---------------------------------


def _noop() -> None:
    """Slot of a standard action the app has not implemented yet."""


def _groups(*groups: list) -> MenuItems:
    """Joins the non-empty groups with a Separator between them."""
    out: MenuItems = []
    for group in groups:
        if not group:
            continue
        if out:
            out.append(Separator())
        out.extend(group)
    return out


def _std(key: str, text: str, slot: Callable[[], None] | None, shortcut=None, *,
         shortcuts=None, tooltip: str | None = None, checkable: bool = False) -> MenuAction:
    """A standard MenuAction: a missing slot means 'the app has not built
    this yet' -> present but disabled (disable, never hide)."""
    return MenuAction(
        key, text, slot or _noop, shortcut=shortcut, shortcuts=shortcuts,
        tooltip=tooltip if slot else (tooltip or "Not available in this app yet"),
        checkable=checkable, enabled=slot is not None,
    )


def standard_file_items(
    *,
    open_files: Callable | None = None,
    open_folder: Callable | None = None,
    save: Callable | None = None,
    save_all: Callable | None = None,
    rename_file: Callable | None = None,
    undo_last_rename: Callable | None = None,
    rename_export_move: Callable | None = None,
    export_settings: Callable | None = None,
    import_settings: Callable | None = None,
    remove_from_list: Callable | None = None,
    clear_list: Callable | None = None,
    exit_slot: Callable | None = None,
    import_and_convert: Callable | None = None,
    save_as: Callable | None = None,
    delete_files: Callable | None = None,
    extra_open: Iterable = (),
    extra_save: Iterable = (),
    extra_rename: Iterable = (),
    extra_destructive: Iterable = (),
) -> MenuItems:
    """The File menu in the fixed group order: open | save | rename |
    settings | remove/clear/delete | Exit.

    Core actions (everything up to exit_slot) are always present; a None slot
    shows the action greyed out. Optional ones - import_and_convert,
    save_as, delete_files - are omitted when None (not every app has them).
    The extra_* lists append app-specific items to a group."""
    return _groups(
        [_std("open_files", labels.OPEN_FILES, open_files, keys.OPEN_FILES),
         _std("open_folder", labels.OPEN_FOLDER, open_folder, keys.OPEN_FOLDER)]
        + ([_std("import_convert", labels.IMPORT_AND_CONVERT, import_and_convert)] if import_and_convert else [])
        + list(extra_open),
        [_std("save", labels.SAVE, save, keys.SAVE)]
        + ([_std("save_as", labels.SAVE_AS, save_as, keys.SAVE_AS)] if save_as else [])
        + [_std("save_all", labels.SAVE_ALL, save_all, keys.SAVE_ALL)]
        + list(extra_save),
        [_std("rename_file", labels.RENAME_FILE, rename_file, keys.RENAME_SINGLE_FILE),
         _std("undo_last_rename", labels.UNDO_LAST_RENAME, undo_last_rename),
         _std("rename_export_move", labels.RENAME_EXPORT_MOVE, rename_export_move,
              keys.RENAME_EXPORT_MOVE)]
        + list(extra_rename),
        [_std("export_settings", labels.EXPORT_SETTINGS, export_settings,
              tooltip="Save portable settings to a file"),
         _std("import_settings", labels.IMPORT_SETTINGS, import_settings,
              tooltip="Load settings from a file exported by this program")],
        [_std("remove_from_list", labels.REMOVE_FROM_LIST, remove_from_list, keys.REMOVE_FROM_LIST),
         _std("clear_list", labels.CLEAR_LIST, clear_list)]
        + ([_std("delete_files", labels.DELETE_FILES, delete_files, keys.DELETE_FILES)] if delete_files else [])
        + list(extra_destructive),
        [MenuAction(KEY_EXIT, labels.EXIT, exit_slot or _noop, enabled=exit_slot is not None)],
    )


def standard_edit_items(
    *,
    undo: Callable | None = None,
    redo: Callable | None = None,
    apply: Callable | None = None,
    redact: Callable | None = None,
    edit_redact_recipe: Callable | None = None,
    search_replace: Callable | None = None,
    change_case: Callable | None = None,
    auto_number: Callable | None = None,
    filter_list: Callable | None = None,
    extra_text_tools: Iterable = (),
) -> MenuItems:
    """The Edit menu: undo/redo | Apply | Redact + recipe | text tools
    (Search and Replace, Change Case, Auto-Number, Filter List). Core
    actions are present-but-greyed when their slot is None; auto_number and
    filter_list are omitted when None. The Apply item starts as
    'Apply to Selected'; call set_apply_count() to keep 'Apply to N Selected'
    current."""
    return _groups(
        [_std("undo", labels.UNDO, undo, keys.UNDO), _std("redo", labels.REDO, redo, keys.REDO)],
        [_std("apply", labels.apply_to_selected(), apply, keys.APPLY)],
        [_std("redact", labels.REDACT, redact, keys.REDACT,
              tooltip="Run the Redact recipe on the selected files: fix what can be fixed automatically"),
         _std("redact_recipe", labels.EDIT_REDACT_RECIPE, edit_redact_recipe,
              tooltip="Choose and order the Redact steps")],
        [_std("search_replace", labels.SEARCH_REPLACE, search_replace, keys.SEARCH_REPLACE),
         _std("change_case", labels.CHANGE_CASE, change_case)]
        + ([_std("auto_number", labels.AUTO_NUMBER, auto_number)] if auto_number else [])
        + ([_std("filter_list", labels.FILTER_LIST, filter_list, keys.FILTER_LIST)] if filter_list else [])
        + list(extra_text_tools),
    )


def set_apply_count(action: QAction, count: int) -> None:
    """Refreshes the dynamic 'Apply to N Selected' text and disables the
    action at 0 (keeps the mnemonic)."""
    action.setText(labels.apply_to_selected(count))
    action.setEnabled(count > 0)


def standard_view_items(
    *,
    show_metadata_panel: Callable | None = None,
    zoom_in: Callable | None = None,
    zoom_out: Callable | None = None,
    reset_zoom: Callable | None = None,
    refresh_list: Callable | None = None,
    command_palette: Callable | None = None,
    extra_view: Iterable = (),
    refresh_shortcuts: Iterable[str] | None = None,
) -> MenuItems:
    """The View menu: Show Metadata Panel (checkable) | zoom | app extras
    (e.g. epub's Text Wrapping submenu) | Refresh List + Command Palette.

    `command_palette` is normally left None: add_command_palette() wires the
    real slot to this action. `refresh_shortcuts` overrides the default
    F5 + Ctrl+R (videoredactor passes ['F5'] because Ctrl+R is Remux there).
    Zoom shortcuts are Ctrl++ / Ctrl+-; an app that also keeps the zoom
    toolbar's own StandardKey-bound actions must drop one set to avoid an
    ambiguous shortcut (lint reports it)."""
    return _groups(
        [_std("show_metadata_panel", labels.SHOW_METADATA_PANEL, show_metadata_panel, checkable=True)],
        [_std("zoom_in", labels.ZOOM_IN, zoom_in, keys.ZOOM_IN),
         _std("zoom_out", labels.ZOOM_OUT, zoom_out, keys.ZOOM_OUT),
         _std("reset_zoom", labels.RESET_ZOOM, reset_zoom, keys.RESET_ZOOM)],
        list(extra_view),
        [_std("refresh_list", labels.REFRESH_LIST, refresh_list,
              shortcuts=list(refresh_shortcuts) if refresh_shortcuts is not None else keys.REFRESH_LIST),
         MenuAction(KEY_COMMAND_PALETTE, labels.COMMAND_PALETTE, command_palette or _noop,
                    shortcut=keys.COMMAND_PALETTE, keywords="search commands actions")],
    )


def standard_tools_items(
    *,
    preferences: Callable | None = None,
    api_keys: Callable | None = None,
    external_tools: Callable | None = None,
    app_settings: Iterable = (),
    columns: Callable | None = None,
    genres: Callable | None = None,
    languages: Callable | None = None,
) -> MenuItems:
    """The Tools menu (ALL settings live here): Preferences | API Keys,
    External Tools, app-specific settings entries | Columns / Genres /
    Languages. Every shared entry is omitted when its slot is None (epub has
    no API keys today; cbz has no Preferences dialog yet)."""
    return _groups(
        [MenuAction(KEY_PREFERENCES, labels.PREFERENCES, preferences, shortcut=keys.PREFERENCES)]
        if preferences else [],
        ([_std("api_keys", labels.API_KEYS, api_keys)] if api_keys else [])
        + ([_std("external_tools", labels.EXTERNAL_TOOLS, external_tools)] if external_tools else [])
        + list(app_settings),
        ([_std("columns", labels.COLUMNS, columns)] if columns else [])
        + ([_std("genres", labels.GENRES, genres)] if genres else [])
        + ([_std("languages", labels.LANGUAGES, languages)] if languages else []),
    )


def standard_help_items(
    app_name: str,
    changelog_slot: Callable | None,
    credits_slot: Callable | None,
    about_slot: Callable | None,
    extra: Iterable = (),
) -> MenuItems:
    """The Help menu: Changelog, Credits | About <app_name>. About carries
    AboutRole and NO shortcut (F1 is Help contents; `extra` may hold a
    User Guide entry bound to standard_shortcuts.HELP)."""
    return _groups(
        list(extra),
        [_std("changelog", labels.CHANGELOG, changelog_slot),
         _std("credits", labels.CREDITS, credits_slot)],
        [MenuAction(KEY_ABOUT, labels.about(app_name), about_slot or _noop, enabled=about_slot is not None)],
    )


def look_up_submenu(entries: Iterable[MenuAction]) -> Submenu:
    """The 'Look Up' submenu every app shows in Metadata (and the context
    menu): one MenuAction per online source ('Comic Vine…', 'MusicBrainz…')."""
    return Submenu(labels.LOOK_UP, list(entries))


# --- migration helper -----------------------------------------------------------


def with_aliases(action: QAction, *old_shortcuts: str) -> QAction:
    """Adds `old_shortcuts` as secondary shortcuts (QAction.setShortcuts) so a
    moved shortcut keeps working for one release: the primary stays
    action.shortcut(). Duplicates are ignored; returns `action`."""
    current = list(action.shortcuts())
    for text in old_shortcuts:
        seq = QKeySequence(text)
        if not seq.isEmpty() and seq not in current:
            current.append(seq)
    action.setShortcuts(current)
    return action

"""Tests for the standard menu skeleton: core/labels, gui/standard_menus,
gui/menu_lint (the lint apps reuse) and gui/command_palette."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtGui import QAction, QKeySequence  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMainWindow  # noqa: E402

from redactor_common.core import labels  # noqa: E402
from redactor_common.gui import standard_shortcuts as keys  # noqa: E402
from redactor_common.gui.command_palette import (  # noqa: E402
    CommandPalette,
    add_command_palette,
    collect_commands,
    filter_commands,
)
from redactor_common.gui.menu_builder import MenuAction, Separator, Submenu  # noqa: E402
from redactor_common.gui.menu_lint import MAX_HEADINGS, lint_menu_bar  # noqa: E402
from redactor_common.gui.standard_menus import (  # noqa: E402
    AppMenu,
    StandardMenuSpec,
    build_standard_menu_bar,
    get_action_registry,
    look_up_submenu,
    set_apply_count,
    standard_edit_items,
    standard_file_items,
    standard_help_items,
    standard_tools_items,
    standard_view_items,
    with_aliases,
)

_app = QApplication.instance() or QApplication([])


def _noop():
    pass


def make_spec(calls=None):
    calls = calls if calls is not None else []

    def rec(name):
        return lambda: calls.append(name)

    return StandardMenuSpec(
        file=standard_file_items(
            open_files=rec("open_files"), open_folder=rec("open_folder"), save=rec("save"),
            save_as=rec("save_as"), rename_file=rec("rename_file"), exit_slot=rec("exit")),
        edit=standard_edit_items(undo=rec("undo"), redo=rec("redo"), redact=rec("redact"),
                                 search_replace=rec("sr"), change_case=rec("cc"), auto_number=rec("an")),
        view=standard_view_items(show_metadata_panel=rec("panel"), refresh_list=rec("refresh")),
        app_menus=[
            AppMenu(labels.MENU_METADATA, [
                MenuAction("parse", labels.PARSE_FILENAME, rec("parse"), shortcut=keys.PARSE_FILENAME),
                Separator(),
                look_up_submenu([MenuAction("lu_a", "Comic &Vine…", rec("lu_a")),
                                 MenuAction("lu_b", "&Grand Comics Database…", rec("lu_b"))]),
            ]),
            AppMenu(labels.MENU_REPAIR, [MenuAction("validate", labels.VALIDATE_AND_FIX, rec("validate"))]),
        ],
        tools=standard_tools_items(api_keys=rec("api"), columns=rec("cols"), genres=rec("genres"),
                                   languages=rec("langs")),
        help=standard_help_items("Test App", rec("changelog"), rec("credits"), rec("about")),
        hidden=[MenuAction("hidden_cmd", "Secret &Thing", rec("hidden"), keywords="zebra")],
    )


@pytest.fixture
def built():
    window = QMainWindow()
    calls = []
    menus = build_standard_menu_bar(window, make_spec(calls))
    return window, menus, calls


def titles(menu):
    return [a.text() for a in menu.actions() if not a.isSeparator()]


# --- labels / mnemonic fix -----------------------------------------------------


def test_label_helpers():
    assert labels.strip_mnemonic("&Open Files…") == "Open Files…"
    assert labels.strip_mnemonic("Fish && Chips") == "Fish & Chips"
    assert labels.plain_label("Rename / &Export / Move…") == "Rename / Export / Move"
    assert labels.mnemonic_letter("Ex&port Settings…") == "p"
    assert labels.mnemonic_letter("No mnemonic") is None
    assert labels.mnemonic_letter("A && B") is None
    assert labels.about("Comic Redactor") == "&About Comic Redactor"
    assert labels.apply_to_selected(3) == "&Apply to 3 Selected"


def test_file_menu_mnemonics_are_unique():
    file_labels = [getattr(labels, n) for n in (
        "OPEN_FILES", "OPEN_FOLDER", "IMPORT_AND_CONVERT", "SAVE", "SAVE_AS", "SAVE_ALL", "RENAME_FILE",
        "UNDO_LAST_RENAME", "RENAME_EXPORT_MOVE", "EXPORT_SETTINGS", "IMPORT_SETTINGS",
        "REMOVE_FROM_LIST", "CLEAR_LIST", "DELETE_FILES", "EXIT")]
    letters = [labels.mnemonic_letter(t) for t in file_labels]
    assert None not in letters and len(set(letters)) == len(letters)


def test_settings_menu_actions_use_fixed_mnemonics():
    from redactor_common.gui.settings_bundle_dialogs import settings_menu_actions

    exp, imp = settings_menu_actions(_noop, _noop)
    assert exp.text == labels.EXPORT_SETTINGS == "Ex&port Settings…"
    assert imp.text == labels.IMPORT_SETTINGS == "&Import Settings…"
    # neither letter clashes with Rename / &Export / Move or Re&move from List
    clash = {labels.mnemonic_letter(labels.RENAME_EXPORT_MOVE), labels.mnemonic_letter(labels.REMOVE_FROM_LIST)}
    assert not clash & {labels.mnemonic_letter(exp.text), labels.mnemonic_letter(imp.text)}


# --- builder ---------------------------------------------------------------------


def test_heading_order_and_keys(built):
    window, menus, _ = built
    assert list(menus) == ["File", "Edit", "View", "Metadata", "Repair", "Tools", "Help"]
    bar = [a.text() for a in window.menuBar().actions()]
    assert bar == ["&File", "&Edit", "&View", "&Metadata", "&Repair", "&Tools", "&Help"]


def test_file_menu_group_order_and_optional_items(built):
    _, menus, _ = built
    assert titles(menus["File"]) == [
        labels.OPEN_FILES, labels.OPEN_FOLDER, labels.SAVE, labels.SAVE_AS, labels.SAVE_ALL,
        labels.RENAME_FILE, labels.UNDO_LAST_RENAME, labels.RENAME_EXPORT_MOVE,
        labels.EXPORT_SETTINGS, labels.IMPORT_SETTINGS, labels.REMOVE_FROM_LIST, labels.CLEAR_LIST,
        labels.EXIT]  # no Import and Convert / Delete Files: optional and not given
    seps = [i for i, a in enumerate(menus["File"].actions()) if a.isSeparator()]
    assert len(seps) == 5


def test_missing_core_slot_is_disabled_not_hidden(built):
    window, _, _ = built
    reg = get_action_registry(window)
    assert not reg["save_all"].isEnabled() and reg["save_all"].toolTip()
    assert reg["save"].isEnabled()
    assert "delete_files" not in reg and "auto_number" in reg and "filter_list" not in reg


def test_roles_tagged(built):
    window, _, _ = built
    reg = get_action_registry(window)
    assert reg["exit"].menuRole() == QAction.MenuRole.QuitRole
    assert reg["about"].menuRole() == QAction.MenuRole.AboutRole
    assert reg["save"].menuRole() == QAction.MenuRole.NoRole
    assert reg["api_keys"].menuRole() == QAction.MenuRole.NoRole  # 'Settings'-like, must not move on macOS
    assert reg["about"].shortcut().isEmpty()


def test_preferences_role_and_shortcut():
    window = QMainWindow()
    spec = make_spec()
    spec.tools = standard_tools_items(preferences=_noop, columns=_noop)
    build_standard_menu_bar(window, spec)
    pref = get_action_registry(window)["preferences"]
    assert pref.menuRole() == QAction.MenuRole.PreferencesRole
    assert pref.shortcut().toString() == keys.PREFERENCES


def test_standard_shortcuts_applied(built):
    window, _, _ = built
    reg = get_action_registry(window)
    assert reg["save_as"].shortcut().toString() == "Ctrl+Shift+S"
    assert reg["open_files"].shortcut().toString() == "Ctrl+O"
    assert reg["open_folder"].shortcut().toString() == "Ctrl+Shift+O"
    assert [s.toString() for s in reg["refresh_list"].shortcuts()] == ["F5", "Ctrl+R"]
    assert reg["command_palette"].shortcut().toString() == "Ctrl+K"
    assert reg["show_metadata_panel"].isCheckable()


def test_submenu_and_registry_paths(built):
    window, menus, calls = built
    reg = get_action_registry(window)
    assert reg.path_of("lu_a") == "Metadata ▸ Look Up"
    assert reg.path_of("save") == "File"
    assert reg.path_of("hidden_cmd") == ""
    assert "lu_b" in reg and len(reg) > 20
    reg["lu_a"].trigger()
    assert calls == ["lu_a"]


def test_builder_rejects_coding_errors():
    window = QMainWindow()
    spec = make_spec()
    spec.app_menus.append(AppMenu("&Tools", []))
    with pytest.raises(ValueError, match="standard heading"):
        build_standard_menu_bar(window, spec)
    spec = make_spec()
    spec.app_menus[0].items.append(MenuAction("save", "Dup", _noop))
    with pytest.raises(ValueError, match="duplicate action keys"):
        build_standard_menu_bar(QMainWindow(), spec)


def test_set_apply_count(built):
    window, _, _ = built
    apply = get_action_registry(window)["apply"]
    assert not apply.isEnabled() and apply.text() == "&Apply to Selected"
    apply.setEnabled(True)
    set_apply_count(apply, 0)
    assert apply.text() == "&Apply to 0 Selected" and not apply.isEnabled()
    set_apply_count(apply, 4)
    assert apply.text() == "&Apply to 4 Selected" and apply.isEnabled()


def test_with_aliases():
    act = QAction("x")
    act.setShortcut(QKeySequence("Ctrl+Shift+A"))
    with_aliases(act, "Ctrl+Shift+S", "Ctrl+Shift+A", "")
    assert [s.toString() for s in act.shortcuts()] == ["Ctrl+Shift+A", "Ctrl+Shift+S"]
    assert act.shortcut().toString() == "Ctrl+Shift+A"


def test_help_has_no_f1_on_about(built):
    window, _, _ = built
    about = get_action_registry(window)["about"]
    assert about.text() == "&About Test App"
    assert "F1" not in [s.toString() for s in about.shortcuts()]


def test_builder_keeps_old_menu_builder_working():
    """The pre-skeleton API is unchanged and MenuAction's new fields default off."""
    from redactor_common.gui.menu_builder import build_menu_bar

    window = QMainWindow()
    specs = {n: [] for n in ("File", "Import", "Operations", "Settings", "Help")}
    specs["File"] = [MenuAction("x", "&X", _noop, shortcut="Ctrl+X")]
    acts = build_menu_bar(window, specs)
    assert acts["x"].menuRole() == QAction.MenuRole.TextHeuristicRole and acts["x"].isEnabled()


# --- lint ------------------------------------------------------------------------


def test_lint_clean_for_builder_output(built):
    window, _, _ = built
    assert lint_menu_bar(window) == []
    assert lint_menu_bar(window.menuBar()) == []


def test_lint_clean_for_spec():
    assert lint_menu_bar(make_spec()) == []


def test_lint_heading_order_and_count():
    window = QMainWindow()
    bar = window.menuBar()
    for t in ("&Edit", "&File", "&View", "&Tools", "&Help"):
        bar.addMenu(t)
    problems = lint_menu_bar(window)
    assert any("menu #1 must be File" in p for p in problems)
    assert any("menu #2 must be Edit" in p for p in problems)

    window = QMainWindow()
    bar = window.menuBar()
    for t in ("&File", "&Edit", "&View", "&Metadata", "&Repair", "&Send", "&Analyze", "Me&dia", "&Tools", "&Help"):
        bar.addMenu(t)
    assert any(f"10 top-level menus; the cap is {MAX_HEADINGS}" in p for p in lint_menu_bar(window))

    window = QMainWindow()
    bar = window.menuBar()
    for t in ("&File", "&Edit", "&View", "&Help", "&Tools"):
        bar.addMenu(t)
    assert any("last two menus" in p for p in lint_menu_bar(window))


def _spec_with_file(items):
    spec = make_spec()
    spec.file = items
    return spec


def test_lint_duplicate_mnemonic():
    spec = _spec_with_file([
        MenuAction("a", "&Export Settings…", _noop), MenuAction("b", "Rename / &Export / Move…", _noop)])
    problems = lint_menu_bar(spec, check_canonical=False)
    assert any("File: mnemonic 'E' used by both" in p for p in problems)


def test_lint_duplicate_mnemonic_in_submenu_and_headings():
    spec = make_spec()
    spec.app_menus[0].items = [look_up_submenu([
        MenuAction("x1", "&Comic Vine…", _noop), MenuAction("x2", "&Calibre…", _noop)])]
    spec.app_menus.append(AppMenu("&Reports", []))  # R clashes with Repair
    problems = lint_menu_bar(spec)
    assert any("Metadata > Look Up: mnemonic 'C'" in p for p in problems)
    assert any("menu bar: mnemonic 'R'" in p for p in problems)


def test_lint_duplicate_shortcut():
    spec = _spec_with_file([
        MenuAction("a", "&Open Files…", _noop, shortcut="Ctrl+O"),
        MenuAction("b", "Open &Folder…", _noop, shortcuts=["Ctrl+Shift+O", "Ctrl+O"])])
    problems = lint_menu_bar(spec)
    assert any("shortcut Ctrl+O bound more than once" in p for p in problems)


def test_lint_platform_standard_shortcuts():
    spec = _spec_with_file([
        MenuAction("sa", "Save A&ll", _noop, shortcut="Ctrl+Shift+S"),  # video's old binding
        MenuAction("of", "Open &Folder…", _noop, shortcut="Ctrl+O"),
    ])
    spec.help = [MenuAction("about", "&About X", _noop, shortcut="F1")]
    problems = lint_menu_bar(spec)
    assert any("Ctrl+Shift+S is Save As" in p for p in problems)
    assert any("Ctrl+O is Open Files" in p for p in problems)
    assert any("F1 is Help contents" in p for p in problems)


def test_lint_canonical_labels():
    spec = _spec_with_file([
        MenuAction("a", "Open &Files…", _noop),  # right words, wrong mnemonic
        MenuAction("b", "&Load Files...", _noop),  # legacy name
        MenuAction("c", "E&xit Program", _noop),
    ])
    problems = lint_menu_bar(spec)
    assert any("canonical is '&Open Files…'" in p for p in problems)
    assert any("legacy label, use 'Open Files'" in p for p in problems)
    assert any("Exit Program" in p and "legacy" in p for p in problems)
    assert not any("canonical" in p or "legacy" in p for p in lint_menu_bar(spec, check_canonical=False))


def test_lint_rejects_unknown_target():
    with pytest.raises(TypeError):
        lint_menu_bar(object())


# --- shortcut constants -----------------------------------------------------------


def test_shortcut_constants():
    assert keys.OPEN_FILES == keys.LOAD_FILES == "Ctrl+O"
    assert keys.OPEN_FOLDER == keys.LOAD_FOLDER == "Ctrl+Shift+O"
    assert keys.SAVE_AS == "Ctrl+Shift+S" and keys.SAVE_ALL == "Ctrl+Shift+A"
    assert keys.COMMAND_PALETTE == "Ctrl+K" and keys.APPLY == "Ctrl+Return"
    assert keys.SEARCH_REPLACE == "Ctrl+H" and keys.RESET_ZOOM == "Ctrl+0"
    assert keys.REFRESH_LIST == ["F5", "Ctrl+R"] and keys.REDACT == "Ctrl+Shift+E"
    assert keys.RENAME_EXPORT_MOVE == keys.RENAME_EXPORT_BY_PATTERN == "Ctrl+E"
    assert keys.PARSE_FILENAME == "Ctrl+I"


# --- command palette ----------------------------------------------------------------


def test_collect_commands_paths_and_shortcuts(built):
    window, _, _ = built
    cmds = {c.title: c for c in collect_commands(window, get_action_registry(window))}
    assert cmds["Comic Vine"].path == "Metadata ▸ Look Up"
    assert cmds["Open Files"].path == "File" and cmds["Open Files"].shortcut
    assert cmds["Secret Thing"].path == ""  # hidden: registry only
    assert "Look Up" not in cmds  # submenu parents are not commands


def test_palette_filter_and_ranking(built):
    window, _, _ = built
    palette = CommandPalette(window, get_action_registry(window))
    palette.refresh()
    palette.set_filter("save")
    shown = [c.title for c in palette.visible_commands()]
    assert shown[:3] == ["Save", "Save As", "Save All"] and "Open Files" not in shown
    palette.set_filter("look vine")  # words match title + path
    assert [c.title for c in palette.visible_commands()] == ["Comic Vine"]
    palette.set_filter("zebra")  # keywords of a hidden action
    assert [c.title for c in palette.visible_commands()] == ["Secret Thing"]
    palette.set_filter("opfl")  # letters in order
    assert "Open Files" in [c.title for c in palette.visible_commands()]
    palette.set_filter("qqqq")
    assert palette.visible_commands() == []


def test_palette_trigger_and_disabled(built):
    window, _, calls = built
    palette = CommandPalette(window, get_action_registry(window))
    palette.refresh()
    palette.set_filter("save as")
    assert palette.trigger_current() is True
    assert calls == ["save_as"]
    # Save All has no slot: listed, greyed, not triggerable
    palette.refresh()
    palette.set_filter("save all")
    cmd = palette.visible_commands()[0]
    assert cmd.title == "Save All" and not cmd.enabled
    from PyQt6.QtCore import Qt

    assert not palette.list.item(0).flags() & Qt.ItemFlag.ItemIsEnabled
    palette.list.setCurrentRow(0)
    assert palette.trigger_current() is False and calls == ["save_as"]


def test_palette_enter_and_navigation(built):
    window, _, calls = built
    palette = CommandPalette(window, get_action_registry(window))
    palette.refresh()
    palette.set_filter("save")
    assert palette.list.currentRow() == 0
    palette._move(1)
    assert palette.list.currentRow() == 1  # Save As
    palette._move(1)  # Save All is disabled: skipped, stays put
    assert palette.list.currentRow() == 1
    palette.filter_edit.returnPressed.emit()
    assert calls == ["save_as"]


def test_add_command_palette_reuses_view_action(built):
    window, _, calls = built
    reg = get_action_registry(window)
    act = add_command_palette(window, reg)
    assert act is reg["command_palette"] and act.shortcut().toString() == "Ctrl+K"
    window.command_palette.refresh()
    assert "Command Palette" not in [c.title for c in window.command_palette._commands]
    act.trigger()
    assert window.command_palette.isVisible()
    window.command_palette.close()


def test_add_command_palette_without_menu_entry():
    window = QMainWindow()
    act = add_command_palette(window)
    assert act.shortcut().toString() == "Ctrl+K"
    assert act in window.actions()
    assert act.menuRole() == QAction.MenuRole.NoRole


def test_filter_commands_empty_query_keeps_menu_order(built):
    window, _, _ = built
    cmds = collect_commands(window)
    assert [c.title for c in filter_commands("", cmds)] == [c.title for c in cmds]

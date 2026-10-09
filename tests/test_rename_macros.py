"""Rename dialog macros: save/clear callbacks, and replaying a saved state on a dialog that is never shown."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from redactor_common.gui.rename_pattern_dialog import RenamePatternDialog  # noqa: E402

_app = QApplication.instance() or QApplication([])

PLACEHOLDERS = [("series", "Series"), ("number", "Number")]


def make(tmp_path, **kw):
    files = [tmp_path / "a.cbz", tmp_path / "b.cbz"]
    for f in files:
        f.write_bytes(b"x")
    vals = {str(files[0]): {"series": "Saga", "number": "1"}, str(files[1]): {"series": "Saga", "number": "2"}}
    items = [str(f) for f in files]
    return RenamePatternDialog(
        items, PLACEHOLDERS, lambda p: vals[p], lambda p: p,
        pattern_history=[], default_pattern="%series% %number%", zero_pad_field="number", **kw,
    ), items


def test_state_round_trips_and_plans_a_rename(tmp_path):
    dlg, items = make(tmp_path)
    dlg.apply_macro_state({"pattern": "%series% - %number%", "mode": "rename", "zero_pad": True, "zero_pad_width": 3})
    assert dlg.can_apply() and dlg.apply_problem() == ""
    assert [os.path.basename(new) for _i, _o, new in dlg.planned_renames()] == ["Saga - 001.cbz", "Saga - 002.cbz"]
    state = dlg.macro_state()
    assert state["pattern"] == "%series% - %number%" and state["mode"] == "rename" and state["zero_pad_width"] == 3


def test_export_without_a_folder_cannot_apply_and_with_one_can(tmp_path):
    dlg, _ = make(tmp_path)
    dlg.apply_macro_state({"pattern": "%series% %number%", "mode": "export"})
    assert not dlg.can_apply() and "export folder" in dlg.apply_problem()
    out = tmp_path / "out"
    out.mkdir()
    dlg.apply_macro_state({"pattern": "%series% %number%", "mode": "export", "export_folder": str(out)})
    assert dlg.can_apply() and dlg.is_export_mode()
    assert all(os.path.dirname(new) == str(out) for _i, _o, new in dlg.planned_renames())


def test_move_mode_plans_into_the_library_root(tmp_path):
    dlg, _ = make(tmp_path)
    root = tmp_path / "lib"
    root.mkdir()
    dlg.apply_macro_state({"pattern": "%series%/%number%", "mode": "move", "library_root": str(root)})
    assert dlg.is_move_mode() and dlg.can_apply()
    assert [m.new_path for m in dlg.planned_moves()][0].startswith(str(root))


def test_replaying_a_macro_does_not_write_the_remembered_choices(tmp_path):
    seen = []
    dlg, _ = make(tmp_path, ascii_only=False, on_ascii_only_changed=seen.append, on_zero_pad_changed=lambda *a: seen.append(a))
    dlg.apply_macro_state({"pattern": "%series%", "mode": "rename", "ascii": True, "zero_pad": True, "zero_pad_width": 4})
    assert seen == []
    assert dlg.ascii_only()


def test_the_save_menu_calls_back_per_slot_and_can_clear(tmp_path):
    saved, cleared = [], []
    labels = ["Rename: %series%", "", "", "", ""]
    dlg, _ = make(tmp_path, macro_labels=lambda: labels, on_save_macro=lambda n, s: saved.append((n, s["pattern"])),
                  on_clear_macro=cleared.append)
    dlg._fill_macro_menu()
    actions = dlg.macro_menu.actions()
    texts = [a.text() for a in actions if not a.isSeparator()]
    assert texts[0].startswith("Save as Macro 1") and "replaces" in texts[0]
    assert sum(t.startswith("Save as Macro") for t in texts) == 5
    assert any(t.startswith("Clear Macro 1") for t in texts) and not any(t.startswith("Clear Macro 2") for t in texts)
    actions[2].trigger()
    assert saved == [(2, "%series% %number%")]
    next(a for a in actions if a.text().startswith("Clear Macro 1")).trigger()
    assert cleared == [0]

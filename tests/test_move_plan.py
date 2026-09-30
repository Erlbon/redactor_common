"""Tests for core/move_plan.py, the RenameLog move extensions, and the
dialog's "Move into folders" mode + gui/move_runner.py."""

import errno
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from redactor_common.core import move_plan  # noqa: E402
from redactor_common.core.move_plan import (  # noqa: E402
    execute_move, plan_moves, prune_empty_dirs, render_relative_path,
)
from redactor_common.core.rename_log import RenameLog  # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv)

PATTERN = "%author%/%series%/%title%"


def _vals(**kw):
    return {"author": "", "series": "", "title": "", **kw}


# --- segment rendering ---

def test_segments_basic_and_empty_folder_dropped():
    assert render_relative_path(_vals(author="A", series="S", title="T"), PATTERN) == ["A", "S", "T"]
    assert render_relative_path(_vals(author="A", title="T"), PATTERN) == ["A", "T"]


def test_fallback_segment_only_when_asked():
    assert render_relative_path(_vals(author="A", title="T"), PATTERN, fallback_segment="Unknown") == ["A", "Unknown", "T"]


def test_slash_inside_value_does_not_make_a_folder():
    assert render_relative_path(_vals(author="AC/DC", series="x\\y", title="T"), PATTERN) == ["ACDC", "xy", "T"]


def test_dotdot_and_dots_cannot_escape():
    segs = render_relative_path(_vals(author="..", series=".", title="T"), PATTERN)
    assert segs == ["T"]
    segs = render_relative_path(_vals(author="../..", title="T"), "%author%/%title%")
    assert ".." not in segs and all("/" not in s and "\\" not in s for s in segs)
    # a literal ".." in the pattern is dropped too
    assert render_relative_path(_vals(title="T"), "../../%title%") == ["T"]


def test_empty_file_segment_falls_back_and_blank_pattern():
    assert render_relative_path(_vals(author="A"), PATTERN) == ["A", "untitled"]
    assert render_relative_path(_vals(), "") == ["untitled"]
    assert render_relative_path(_vals(author="A", title="T"), "//%author%\\\\%title%/") == ["A", "T"]


def test_reserved_and_trailing_dots_per_segment():
    assert render_relative_path(_vals(author="CON", series="Name. ", title="aux"), PATTERN) == ["_CON", "Name", "_aux"]


def test_long_segments_and_total_cap():
    long = "x" * 400
    segs = render_relative_path(_vals(author=long, series=long, title=long), PATTERN)
    assert all(len(s) <= 150 for s in segs)
    capped = render_relative_path(_vals(author=long, series=long, title=long), PATTERN, max_total=100)
    assert len("/".join(capped)) <= 100 and len(capped) == 3


def _link_dir(target, link):
    """Symlink, or a junction on Windows without symlink rights; skips if neither works."""
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":
        import subprocess
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        if done.returncode == 0:
            return
    pytest.skip("neither symlinks nor junctions available here")


# --- planning ---

def _plan(tmp_path, items, pattern=PATTERN, **kw):
    root = tmp_path / "lib"
    root.mkdir(exist_ok=True)
    return plan_moves(
        items, str(root), pattern, lambda it: it["v"], lambda it: it["path"], **kw,
    )


def _item(tmp_path, name, **v):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    path = src / name
    path.write_text(name)
    return {"path": str(path), "v": _vals(**v)}


def test_plan_numbers_collisions_in_batch_and_on_disk(tmp_path):
    a = _item(tmp_path, "a.epub", author="A", title="T")
    b = _item(tmp_path, "b.epub", author="A", title="T")
    existing = tmp_path / "lib" / "A"
    existing.mkdir(parents=True)
    (existing / "T.epub").write_text("x")
    plans = _plan(tmp_path, [a, b])
    names = [os.path.basename(p.new_path) for p in plans]
    assert names == ["T (2).epub", "T (3).epub"]
    assert plans[0].dirs_to_create == []


def test_plan_lists_dirs_to_create_outermost_first(tmp_path):
    plan = _plan(tmp_path, [_item(tmp_path, "a.epub", author="A", series="S", title="T")])[0]
    root = str(tmp_path / "lib")
    assert plan.dirs_to_create == [os.path.join(root, "A"), os.path.join(root, "A", "S")]
    assert plan.relative_path() == "A/S/T.epub" and not plan.blocking and not plan.is_noop


def test_plan_same_place_is_noop_and_copy_mode_numbers(tmp_path):
    lib = tmp_path / "lib" / "A"
    lib.mkdir(parents=True)
    f = lib / "T.epub"
    f.write_text("x")
    item = {"path": str(f), "v": _vals(author="A", title="T")}
    assert _plan(tmp_path, [item])[0].is_noop
    copy_plan = _plan(tmp_path, [item], copy=True)[0]
    assert os.path.basename(copy_plan.new_path) == "T (2).epub"


def test_plan_missing_root_blocks(tmp_path):
    item = _item(tmp_path, "a.epub", title="T")
    plans = plan_moves([item], str(tmp_path / "nope"), PATTERN, lambda i: i["v"], lambda i: i["path"])
    assert plans[0].blocking


def test_plan_path_too_long_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(move_plan, "MAX_PATH_LENGTH", 10)
    plan = _plan(tmp_path, [_item(tmp_path, "a.epub", title="T")])[0]
    assert plan.blocking and "longer" in plan.warning


def test_plan_escape_via_symlink_is_blocking(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "lib"
    root.mkdir()
    _link_dir(outside, root / "A")
    plan = _plan(tmp_path, [_item(tmp_path, "a.epub", author="A", title="T")])[0]
    assert plan.blocking and "outside" in plan.warning and plan.dirs_to_create == []


def test_resolves_inside_basics(tmp_path):
    root = str(tmp_path)
    assert move_plan.resolves_inside(root, os.path.join(root, "a", "b.txt"))
    assert not move_plan.resolves_inside(root, root)
    assert not move_plan.resolves_inside(root, os.path.join(root, "..", "x.txt"))


# --- execution ---

def test_same_volume_move_creates_and_reports_dirs(tmp_path):
    item = _item(tmp_path, "a.epub", author="A", series="S", title="T")
    plan = _plan(tmp_path, [item])[0]
    seen = []
    result = execute_move(plan.old_path, plan.new_path, on_created_dir=seen.append)
    assert os.path.isfile(plan.new_path) and not os.path.exists(plan.old_path)
    assert result.created_dirs == plan.dirs_to_create == seen and not result.cross_volume


def test_execute_never_overwrites_and_missing_source(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("a")
    b.write_text("b")
    with pytest.raises(FileExistsError):
        execute_move(str(a), str(b))
    assert b.read_text() == "b" and a.exists()
    with pytest.raises(FileNotFoundError):
        execute_move(str(tmp_path / "gone"), str(tmp_path / "c.txt"))


def _force_cross_volume(monkeypatch):
    """The direct rename of the ORIGINAL fails like a cross-device move;
    every other rename (the verified copy's temp -> final) is real."""
    from redactor_common.core import os_utils
    real = os_utils.rename_no_clobber

    def fake(src, dst):
        if not os.path.basename(src).endswith(".part"):
            raise OSError(errno.EXDEV, "cross-device link")
        real(src, dst)
    monkeypatch.setattr(move_plan, "rename_no_clobber", fake)


def test_cross_volume_copies_verifies_and_trashes(tmp_path, monkeypatch):
    src = tmp_path / "a.bin"
    src.write_bytes(b"hello" * 1000)
    dst = tmp_path / "new" / "a.bin"
    _force_cross_volume(monkeypatch)
    trashed = []
    result = execute_move(str(src), str(dst), trash=trashed.append)
    assert result.cross_volume and result.original_trashed and trashed == [str(src)]
    assert dst.read_bytes() == b"hello" * 1000
    assert [p for p in os.listdir(dst.parent) if p.endswith(".part")] == []


def test_cross_volume_keeps_original_when_trash_fails(tmp_path, monkeypatch):
    src = tmp_path / "a.bin"
    src.write_bytes(b"data")
    dst = tmp_path / "x" / "a.bin"
    _force_cross_volume(monkeypatch)

    def bad_trash(path):
        raise RuntimeError("no bin")
    result = execute_move(str(src), str(dst), trash=bad_trash)
    assert result.original_kept and not result.original_trashed and "kept" in result.warning
    assert src.exists() and dst.exists()


def test_cross_volume_verification_failure_changes_nothing(tmp_path, monkeypatch):
    src = tmp_path / "a.bin"
    src.write_bytes(b"data")
    dst = tmp_path / "x" / "a.bin"
    _force_cross_volume(monkeypatch)
    import redactor_common.core.scan_stamp as stamp
    calls = iter(["1-aaa", "1-bbb"])
    monkeypatch.setattr(stamp, "content_fingerprint", lambda p: next(calls))
    with pytest.raises(OSError):
        execute_move(str(src), str(dst), trash=lambda p: pytest.fail("must not trash"))
    assert src.exists() and not dst.exists() and os.listdir(dst.parent) == []


def test_copy_mode_only_copies(tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"data")
    dst = tmp_path / "d" / "b.bin"
    result = execute_move(str(src), str(dst), copy=True, trash=lambda p: pytest.fail("no trash in copy mode"))
    assert src.exists() and dst.read_bytes() == b"data" and result.created_dirs == [str(tmp_path / "d")]


# --- pruning ---

def test_prune_only_empty_never_root(tmp_path):
    root = tmp_path / "lib"
    (root / "A" / "S").mkdir(parents=True)
    (root / "B").mkdir()
    (root / "B" / "keep.txt").write_text("x")
    removed = prune_empty_dirs([str(root / "A" / "S"), str(root / "B"), str(root)], stop_at_root=str(root))
    assert sorted(removed) == sorted([str(root / "A" / "S"), str(root / "A")])
    assert root.is_dir() and (root / "B" / "keep.txt").exists()


def test_prune_without_root_does_not_climb_and_keeps_hidden_files(tmp_path):
    (tmp_path / "A" / "S").mkdir(parents=True)
    (tmp_path / "H").mkdir()
    (tmp_path / "H" / ".hidden").write_text("x")
    removed = prune_empty_dirs([str(tmp_path / "A" / "S"), str(tmp_path / "H")])
    assert removed == [str(tmp_path / "A" / "S")] and (tmp_path / "A").is_dir() and (tmp_path / "H").is_dir()


# --- undo ---

def test_undo_restores_moves_and_returns_created_dirs(tmp_path):
    log = RenameLog(str(tmp_path / "log.json"))
    item = _item(tmp_path, "a.epub", author="A", series="S", title="T")
    plan = _plan(tmp_path, [item])[0]
    res = execute_move(plan.old_path, plan.new_path)
    log.record("Move", [(plan.old_path, res.new_path)], created_dirs=res.created_dirs, root=plan.root)
    # the source folder got tidied away meanwhile
    os.rmdir(os.path.dirname(plan.old_path)) if not os.listdir(os.path.dirname(plan.old_path)) else None
    undone = log.undo_last()
    assert os.path.isfile(plan.old_path) and not undone.problems
    assert undone.created_dirs == list(reversed(res.created_dirs)) and undone.root == plan.root
    assert prune_empty_dirs(undone.created_dirs, stop_at_root=undone.root, climb=False)


def test_undo_reports_trashed_cross_volume_move(tmp_path):
    log = RenameLog(str(tmp_path / "log.json"))
    new = tmp_path / "lib" / "a.bin"
    new.parent.mkdir()
    new.write_text("x")
    old = str(tmp_path / "src" / "a.bin")
    log.record("Move", [(old, str(new))], trashed=[(old, str(new))])
    undone = log.undo_last()
    assert new.exists() and not undone.restored and "Recycle Bin" in undone.problems[0]


def test_old_log_entries_without_move_fields_still_load(tmp_path):
    import json
    path = tmp_path / "log.json"
    path.write_text(json.dumps([{"label": "x", "when": 1.0, "renames": [["a", "b"]]}]))
    batch = RenameLog(str(path)).last_batch()
    assert batch.created_dirs == [] and batch.trashed == [] and batch.root == ""


# --- dialog + runner ---

def _dialog(tmp_path, items, root="", on_root=None, **kw):
    from redactor_common.gui.rename_pattern_dialog import RenamePatternDialog
    return RenamePatternDialog(
        items, [("author", "Author")], lambda it: it["v"], lambda it: it["path"],
        pattern_history=[PATTERN], default_pattern=PATTERN,
        library_root=root, on_library_root_changed=on_root, **kw,
    )


def test_dialog_move_mode_preview_and_ok(tmp_path):
    item = _item(tmp_path, "a.epub", author="A", series="S", title="T")
    (tmp_path / "lib").mkdir()
    dialog = _dialog(tmp_path, [item], root=str(tmp_path / "lib"))
    assert not dialog.is_move_mode() and dialog.planned_moves() == []
    dialog.move_radio.setChecked(True)
    assert dialog.is_move_mode() and not dialog.is_export_mode()
    assert dialog.preview_table.item(0, 1).text() == "A/S/T.epub"
    assert dialog._ok_button.isEnabled() and dialog.library_root() == str(tmp_path / "lib")
    (_item_, old, new), = dialog.planned_renames()
    assert old == item["path"] and os.path.isabs(new) and dialog.planned_moves()[0].new_path == new


def test_dialog_move_mode_needs_root_and_blocks_escape(tmp_path):
    item = _item(tmp_path, "a.epub", author="A", title="T")
    dialog = _dialog(tmp_path, [item])
    dialog.move_radio.setChecked(True)
    assert not dialog._ok_button.isEnabled() and "library root" in dialog.warning_label.text()

    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "lib"
    root.mkdir()
    _link_dir(outside, root / "A")
    dialog = _dialog(tmp_path, [item], root=str(root))
    dialog.move_radio.setChecked(True)
    assert not dialog._ok_button.isEnabled() and "outside" in dialog.warning_label.text()


def test_dialog_rename_and_export_unchanged_by_separators(tmp_path):
    item = {"path": str(tmp_path / "a.epub"), "v": _vals(author="A", series="S", title="T")}
    dialog = _dialog(tmp_path, [item])
    (_i, _old, new), = dialog.planned_renames()
    assert os.path.dirname(new) == str(tmp_path) and os.path.basename(new) == "AST.epub"


def test_dialog_remembers_root_via_callback(tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QFileDialog
    seen = []
    dialog = _dialog(tmp_path, [_item(tmp_path, "a.epub", title="T")], on_root=seen.append)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(tmp_path)))
    dialog._choose_root()
    assert seen == [str(tmp_path)]


def test_run_planned_moves_end_to_end(tmp_path, monkeypatch):
    from redactor_common.gui.move_runner import run_planned_moves
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    asked = []
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.Yes),
    )
    good = _item(tmp_path, "a.epub", author="A", title="T")
    gone = _item(tmp_path, "b.epub", author="B", title="U")
    os.remove(gone["path"])
    plans = _plan(tmp_path, [good, gone])
    log = RenameLog(str(tmp_path / "log.json"))
    summary = run_planned_moves(None, plans, False, log, "Move into folders")
    assert len(summary.done) == 1 and len(summary.failed) == 1
    assert os.path.isfile(plans[0].new_path)
    assert log.last_batch().renames == [(good["path"], plans[0].new_path)]
    assert log.last_batch().created_dirs == [os.path.join(str(tmp_path / "lib"), "A")]
    assert len(asked) == 1 and summary.pruned == [os.path.dirname(good["path"])]  # src folder emptied
    assert os.path.isdir(tmp_path / "lib")

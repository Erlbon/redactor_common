"""Tests for core/rename_log.py and gui/rename_undo.py."""

import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from redactor_common.core.rename_log import RenameLog  # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv)


def _files(tmp_path, *names):
    for name in names:
        (tmp_path / name).write_text(name)
    return [str(tmp_path / n) for n in names]


def _rename(pairs):
    for old, new in pairs:
        os.rename(old, new)


def test_record_and_undo_the_last_batch(tmp_path):
    log = RenameLog(str(tmp_path / "log.json"))
    a, b = _files(tmp_path, "a.cbz", "b.cbz")
    first = [(a, str(tmp_path / "A 001.cbz"))]
    _rename(first)
    log.record("Rename File", first)
    second = [(first[0][1], str(tmp_path / "Alpha 001.cbz")), (b, str(tmp_path / "Beta 002.cbz"))]
    _rename(second)
    log.record("Rename by Pattern", second)

    assert log.last_batch().label == "Rename by Pattern" and len(log.batches()) == 2
    result = log.undo_last()
    assert sorted(os.listdir(tmp_path)) == ["A 001.cbz", "b.cbz", "log.json"]
    assert len(result.restored) == 2 and not result.problems
    log.undo_last()  # then the one before
    assert sorted(os.listdir(tmp_path)) == ["a.cbz", "b.cbz", "log.json"]
    assert log.undo_last() is None  # nothing left


def test_undo_never_overwrites_and_reports(tmp_path):
    log = RenameLog(str(tmp_path / "log.json"))
    a, b = _files(tmp_path, "a.mp3", "b.mp3")
    pairs = [(a, str(tmp_path / "x.mp3")), (b, str(tmp_path / "y.mp3"))]
    _rename(pairs)
    log.record("Rename by Pattern", pairs)
    os.remove(tmp_path / "y.mp3")  # gone since
    (tmp_path / "a.mp3").write_text("someone else's file")  # old name taken again
    result = log.undo_last()
    assert result.restored == [] and len(result.problems) == 2
    assert "no longer there" in result.problems[0] and "taken again" in result.problems[1]
    assert (tmp_path / "a.mp3").read_text() == "someone else's file"
    assert log.last_batch() is None  # the batch is used up


def test_empty_and_no_op_renames_are_not_recorded(tmp_path):
    log = RenameLog(str(tmp_path / "log.json"))
    log.record("Rename", [])
    log.record("Rename", [(str(tmp_path / "same.cbz"), str(tmp_path / "same.cbz"))])
    assert log.last_batch() is None and not (tmp_path / "log.json").exists()


def test_log_keeps_the_newest_batches_and_survives_a_bad_file(tmp_path):
    path = tmp_path / "log.json"
    log = RenameLog(str(path), max_batches=3)
    for i in range(5):
        log.record(f"R{i}", [(f"/x/{i}", f"/y/{i}")])
    assert [b.label for b in log.batches()] == ["R2", "R3", "R4"]
    path.write_text("{not json")
    assert log.batches() == [] and log.undo_last() is None
    path.write_text(json.dumps([{"label": "ok", "when": 1, "renames": [["a", "b"]]}, {"broken": True}]))
    assert [b.label for b in log.batches()] == ["ok"]


def test_undo_dialog_confirms_and_reports(tmp_path, monkeypatch):
    from redactor_common.gui import rename_undo

    log = RenameLog(str(tmp_path / "log.json"))
    (a,) = _files(tmp_path, "a.epub")
    new = str(tmp_path / "Author - Title.epub")
    os.rename(a, new)
    log.record("Rename by Pattern", [(a, new)])
    asked = []
    monkeypatch.setattr(rename_undo.QMessageBox, "question",
                        lambda *args, **kw: asked.append(args[2]) or QMessageBox.StandardButton.Yes)
    restored = []
    assert rename_undo.undo_last_rename(None, log, lambda n, o: restored.append((n, o))) == 1
    assert "Rename by Pattern (1 file(s)" in asked[0] and "Author - Title.epub" in asked[0]
    assert restored == [(new, a)] and os.path.exists(a)
    shown = []
    monkeypatch.setattr(rename_undo.QMessageBox, "information", lambda *args, **kw: shown.append(args[2]))
    assert rename_undo.undo_last_rename(None, log, lambda n, o: None) == 0 and "no rename to undo" in shown[0]


def test_single_file_rename_records_itself(tmp_path, monkeypatch):
    from redactor_common.gui import rename_single_file as module

    log = RenameLog(str(tmp_path / "log.json"))
    (a,) = _files(tmp_path, "old.cbz")
    monkeypatch.setattr(module.QInputDialog, "getText", lambda *args, **kw: ("new", True))
    paths = []
    assert module.rename_single_file(None, a, paths.append, log=log)
    assert log.last_batch().renames == [(a, str(tmp_path / "new.cbz"))]

"""Batch 4 (2026-10-17): a written note can be undone from Tools > History (created and refreshed), through the real
dialog, the real journal, the real note files. The Recycle Bin is NEVER touched: every trash is a fake that moves the
file into a folder inside tmp_path, or the real redactor_common.core.trash code with the drive declared unreliable."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from redactor_common.core import journal as j
from redactor_common.core import trash as trash_module
from redactor_common.core.notes import find_note, note_status, replace_note
from redactor_common.core.notes_model import (
    KIND_NOTE, KIND_NOTE_UPDATE, Confidence, Proposal, load_note_backup, make_note_undo_handler, prune_note_backups,
    record_note_entry, store_note_backup, undo_note_entry, write_proposal,
)
from tests.test_notes_stamp import Bin, GENERATOR, legacy, stamped


@dataclass
class _Target:
    folder: Path
    kind: str = "artist"
    note_kind: str = "artist"


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def setup(tmp_path):
    folder = tmp_path / "Ultravox"
    folder.mkdir()
    data = tmp_path / "appdata"
    data.mkdir()
    journal = j.Journal(str(data / "app_journal.jsonl"))
    bin_ = Bin(str(tmp_path / "bin"))

    def hook(path, text, replaced, old_bytes=None):
        return record_note_entry(journal, str(data), path, text, replaced, old_bytes)

    return folder, data, journal, bin_, hook


def _create(folder, hook, text="---\ntitle: Ultravox\n---\n\nBand text.\n"):
    p = Proposal(_Target(folder), Confidence.CONFIDENT, text, title="Ultravox")
    result = write_proposal(p, record=hook)
    assert result.ok
    return result.path


def _refresh(folder, hook, bin_, old_text):
    path = folder / "artist.md"
    path.write_text(old_text, encoding="utf-8", newline="\n")
    state = note_status(str(path))
    new = stamped("# Ultravox\n\nBetter text.")
    p = Proposal(_Target(folder), Confidence.CONFIDENT, new, title="Ultravox", old_state=state)
    result = write_proposal(p, record=hook, trash=bin_)
    assert result.ok and result.replaced
    return result.path


def _dialog(journal, handler):
    from redactor_common.gui.history_dialog import HistoryDialog

    return HistoryDialog(journal, lambda path, values: "", undo_handlers={KIND_NOTE: handler, KIND_NOTE_UPDATE: handler})


def _yes(monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a[2])))
    return warnings


def test_a_created_note_is_undone_from_the_history_dialog(qapp, setup, monkeypatch):
    folder, data, journal, bin_, hook = setup
    path = _create(folder, hook)
    entries = journal.entries()
    assert [e.kind for e in entries] == [KIND_NOTE] and entries[0].restorable is False  # as before: not a tag change
    dialog = _dialog(journal, make_note_undo_handler(journal, str(data), trash=bin_))
    dialog.table.selectRow(0)
    assert dialog.restore_button.isEnabled()  # the bug: it stayed disabled for every note entry
    _yes(monkeypatch)
    assert dialog.restore_selected() is True
    assert not os.path.exists(path) and len(bin_.calls) == 1  # Recycle Bin, never a permanent delete
    assert [e.status for e in journal.entries()] == [j.STATUS_RESTORED]
    dialog.table.selectRow(0)
    assert not dialog.restore_button.isEnabled()  # once only


def test_without_a_registered_handler_a_note_entry_stays_disabled(qapp, setup):
    from redactor_common.gui.history_dialog import HistoryDialog

    folder, data, journal, bin_, hook = setup
    _create(folder, hook)
    dialog = HistoryDialog(journal, lambda path, values: "")
    dialog.table.selectRow(0)
    assert not dialog.restore_button.isEnabled()


def test_an_edited_note_is_not_undone(qapp, setup, monkeypatch):
    folder, data, journal, bin_, hook = setup
    path = _create(folder, hook)
    Path(path).write_text("my own words\n", encoding="utf-8")
    dialog = _dialog(journal, make_note_undo_handler(journal, str(data), trash=bin_))
    dialog.table.selectRow(0)
    warnings = _yes(monkeypatch)
    assert dialog.restore_selected() is False
    assert "changed" in warnings[0] and os.path.exists(path) and bin_.calls == []


def test_a_refreshed_note_is_undone_and_the_older_text_is_back(qapp, setup, monkeypatch):
    folder, data, journal, bin_, hook = setup
    old_text = stamped(fmt=1)
    path = _refresh(folder, hook, bin_, old_text)
    entry = journal.entries()[0]
    assert entry.kind == KIND_NOTE_UPDATE
    backup = os.path.join(data, "notes_backup", f"{entry.id}.md")
    assert os.path.isfile(backup) and Path(backup).read_bytes() == old_text.encode("utf-8")
    assert "Better text." in Path(path).read_text(encoding="utf-8")
    dialog = _dialog(journal, make_note_undo_handler(journal, str(data), trash=bin_))
    dialog.table.selectRow(0)
    _yes(monkeypatch)
    assert dialog.restore_selected() is True
    assert Path(path).read_bytes() == old_text.encode("utf-8")  # the folder has a note again, the older one
    assert note_status(path).kind == "outdated-untouched"
    assert len(bin_.calls) == 2  # the older note (at the refresh) and the refreshed one (at the undo)
    assert not os.path.exists(backup)
    assert [f for f in os.listdir(folder) if f.startswith(".")] == []


def test_a_refresh_undo_without_the_backup_is_refused_and_keeps_the_note(setup):
    folder, data, journal, bin_, hook = setup
    path = _refresh(folder, hook, bin_, stamped(fmt=1))
    entry = journal.entries()[0]
    os.unlink(os.path.join(data, "notes_backup", f"{entry.id}.md"))
    ok, message = make_note_undo_handler(journal, str(data), trash=bin_)(entry)
    assert ok is False and "not kept" in message
    assert os.path.exists(path) and len(bin_.calls) == 1  # only the refresh's own move; the refreshed note stays


def test_an_edited_refreshed_note_is_left_alone(setup):
    folder, data, journal, bin_, hook = setup
    path = _refresh(folder, hook, bin_, stamped(fmt=1))
    Path(path).write_text("edited\n", encoding="utf-8")
    ok, message = make_note_undo_handler(journal, str(data), trash=bin_)(journal.entries()[0])
    assert not ok and "changed" in message and Path(path).read_text(encoding="utf-8") == "edited\n"


def test_nothing_is_stored_when_the_hook_is_never_called_or_old_bytes_missing(setup):
    folder, data, journal, bin_, hook = setup
    _refresh(folder, lambda path, text, replaced, old_bytes=None: None, bin_, stamped(fmt=1))
    assert not os.path.exists(os.path.join(data, "notes_backup"))  # History off: the app's hook records nothing


def test_an_undo_on_a_drive_without_a_recycle_bin_is_refused_with_the_hint(setup, monkeypatch):
    folder, data, journal, bin_, hook = setup
    path = _create(folder, hook)
    monkeypatch.setattr(trash_module, "trash_is_reliable", lambda p, drive_type=None: False)  # a fake UNC share
    monkeypatch.setattr("send2trash.send2trash", lambda p: pytest.fail("the real Recycle Bin was touched"), raising=False)
    ok, message = make_note_undo_handler(journal, str(data))(journal.entries()[0])
    assert not ok and "network drive" in message and "backup folder" in message
    assert os.path.exists(path)


def test_an_undo_on_a_network_drive_uses_the_backup_folder_when_one_is_set(setup, tmp_path, monkeypatch):
    folder, data, journal, bin_, hook = setup
    path = _create(folder, hook)
    monkeypatch.setattr(trash_module, "trash_is_reliable", lambda p, drive_type=None: False)
    fallback = tmp_path / "backups"
    ok, message = make_note_undo_handler(journal, str(data), fallback_folder=lambda: str(fallback))(journal.entries()[0])
    assert ok, message
    assert not os.path.exists(path)
    assert [p.name for p in fallback.rglob("artist.md")] == ["artist.md"]


def test_replace_note_through_the_real_retrying_trash_on_a_fake_unc_is_refused(tmp_path, monkeypatch):
    folder = tmp_path / "Ultravox"
    folder.mkdir()
    path = folder / "artist.md"
    path.write_text(stamped(fmt=1), encoding="utf-8", newline="\n")
    monkeypatch.setattr(trash_module, "trash_is_reliable", lambda p, drive_type=None: False)
    monkeypatch.setattr("send2trash.send2trash", lambda p: pytest.fail("the real Recycle Bin was touched"), raising=False)
    result = replace_note(folder, "artist", stamped("# Ultravox\n\nBetter."), expect=note_status(str(path)))
    assert result.status == "error" and "Recycle Bin" in result.message
    assert "On a network drive there is no Recycle Bin: set a backup folder in Preferences" in result.message
    assert "Band text." in path.read_text(encoding="utf-8")  # the old note is kept, the new one not written


def test_replace_note_through_the_real_trash_code_with_a_backup_folder_works_on_a_fake_unc(tmp_path, monkeypatch):
    folder = tmp_path / "Ultravox"
    folder.mkdir()
    path = folder / "artist.md"
    path.write_text(stamped(fmt=1), encoding="utf-8", newline="\n")
    monkeypatch.setattr(trash_module, "trash_is_reliable", lambda p, drive_type=None: False)
    fallback = str(tmp_path / "backups")
    mover = trash_module.trash_with_retries(lambda p: trash_module.move_to_trash(p, fallback))
    result = replace_note(folder, "artist", stamped("# Ultravox\n\nBetter."), expect=note_status(str(path)), trash=mover)
    assert result.ok and "Better." in path.read_text(encoding="utf-8")
    assert len(list(Path(fallback).rglob("artist.md"))) == 1


def test_old_backups_are_pruned_with_the_journal_retention(tmp_path):
    now = 10_000_000.0
    store_note_backup(tmp_path, "aaaa", b"old", max_days=90, clock=lambda: now)
    os.utime(tmp_path / "notes_backup" / "aaaa.md", (now - 91 * 86400, now - 91 * 86400))
    store_note_backup(tmp_path, "bbbb", b"new", max_days=90, clock=lambda: now)
    assert load_note_backup(tmp_path, "aaaa") is None and load_note_backup(tmp_path, "bbbb") == b"new"
    assert prune_note_backups(tmp_path, 90, clock=lambda: now) == 0
    assert load_note_backup(tmp_path, "../x") is None  # an id is never a path

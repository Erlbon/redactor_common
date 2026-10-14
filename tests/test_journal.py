"""core/journal.py (the operations journal) and gui/history_dialog.py."""

import json
import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from redactor_common.core import journal as j

DAY = j.SECONDS_PER_DAY


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def make(tmp_path, **kwargs):
    clock = Clock()
    return j.Journal(str(tmp_path / "app_journal.jsonl"), clock=clock, **kwargs), clock


def test_diff_fields_reports_only_real_changes_as_text():
    old = {"title": "Pilot ", "year": 2020, "genre": None, "same": "x"}
    new = {"title": "Pilot", "year": 2021, "genre": "Drama", "same": "x", "added": 5}
    assert j.diff_fields(old, new) == {"year": ("2020", "2021"), "genre": ("", "Drama"), "added": ("", "5")}


def test_a_save_is_announced_then_finished_and_read_back_newest_first(tmp_path):
    journal, clock = make(tmp_path)
    first = journal.begin("C:/lib/a.mp3", j.KIND_SAVE, {"title": ("Old", "New")})
    clock.now += 5
    journal.finish(first)
    second = journal.record("C:/lib/b.mp3", j.KIND_REDACT, {"genre": ("", "Rock")}, cover_changed=True, note="recipe run")
    entries = journal.entries()
    assert [e.id for e in entries] == [second, first]
    assert entries[1].status == j.STATUS_OK and entries[1].changes == {"title": ("Old", "New")}
    assert entries[0].cover_changed and entries[0].note == "recipe run" and entries[0].kind == j.KIND_REDACT
    # the file is plain UTF-8 JSON lines, one per event
    lines = open(journal.path, encoding="utf-8").read().splitlines()
    assert [json.loads(l)["t"] for l in lines] == ["op", "done", "op", "done"]


def test_nothing_to_record_returns_none_and_writes_nothing(tmp_path):
    journal, _ = make(tmp_path)
    assert journal.begin("a.mp3", j.KIND_SAVE, {}) is None
    journal.finish(None)
    assert not os.path.exists(journal.path)
    assert journal.begin("a.mp3", j.KIND_SAVE, {}, cover_changed=True) is not None  # a cover change alone is recorded


def test_an_interrupted_save_stays_pending_and_a_failed_one_says_why(tmp_path):
    journal, _ = make(tmp_path)
    crashed = journal.begin("a.mp3", j.KIND_SAVE, {"title": ("a", "b")})
    failed = journal.begin("b.mp3", j.KIND_SAVE, {"title": ("a", "b")})
    journal.finish(failed, ok=False, error="disk full")
    by_id = {e.id: e for e in journal.entries()}
    assert by_id[crashed].status == j.STATUS_PENDING and by_id[crashed].restorable  # may or may not have happened
    assert by_id[failed].status == j.STATUS_FAILED and by_id[failed].error == "disk full" and not by_id[failed].restorable


def test_a_half_written_last_line_after_a_crash_is_skipped(tmp_path):
    journal, _ = make(tmp_path)
    journal.record("a.mp3", j.KIND_SAVE, {"title": ("a", "b")})
    with open(journal.path, "a", encoding="utf-8") as f:
        f.write('{"t":"op","id":"zzz","ts":1,"path":"b.mp3","ki')  # cut off mid-line
    assert len(journal.entries()) == 1


def test_non_ascii_values_and_paths_round_trip(tmp_path):
    journal, _ = make(tmp_path)
    journal.record("C:/Bøker/Amélie – 日本.mkv", j.KIND_SAVE, {"title": ("Æ", "Ø å")})
    entry = journal.entries()[0]
    assert entry.path == "C:/Bøker/Amélie – 日本.mkv" and entry.changes["title"] == ("Æ", "Ø å")


def test_filtering_by_path_and_text(tmp_path):
    journal, _ = make(tmp_path)
    journal.record("C:/lib/Show/a.mkv", j.KIND_SAVE, {"title": ("1", "2")})
    journal.record("C:/lib/Other/b.mkv", j.KIND_SAVE, {"genre": ("", "x")})
    assert [os.path.basename(e.path) for e in journal.entries(text="show")] == ["a.mkv"]
    assert [os.path.basename(e.path) for e in journal.entries(text="GENRE")] == ["b.mkv"]  # a field name matches too
    assert len(journal.entries(path="C:/lib/Other/b.mkv")) == 1
    assert len(journal.entries(limit=1)) == 1


def test_values_too_long_to_keep_whole_are_never_restored(tmp_path):
    journal, _ = make(tmp_path)
    huge = "x" * (j.MAX_VALUE_CHARS + 10)
    journal.record("a.epub", j.KIND_SAVE, {"description": (huge, "short"), "title": ("A", "B")})
    entry = journal.entries()[0]
    assert entry.truncated == ["description"] and len(entry.changes["description"][0]) == j.MAX_VALUE_CHARS
    values, skipped = j.Journal.restore_values(entry)
    assert values == {"title": "A"} and skipped == ["description"] and entry.restorable
    only_huge = journal.record("b.epub", j.KIND_SAVE, {"description": (huge, "s")})
    assert not [e for e in journal.entries() if e.id == only_huge][0].restorable


def test_reversed_changes_and_mark_restored(tmp_path):
    journal, _ = make(tmp_path)
    original = journal.record("a.mp3", j.KIND_SAVE, {"title": ("Old", "New"), "year": ("1", "2")})
    entry = journal.entries()[0]
    assert j.Journal.reversed_changes(entry, ["title"]) == {"title": ("New", "Old")}
    restore = journal.record("a.mp3", j.KIND_RESTORE, j.Journal.reversed_changes(entry, ["title", "year"]))
    journal.mark_restored(original, restore)
    by_id = {e.id: e for e in journal.entries()}
    assert by_id[original].status == j.STATUS_RESTORED and by_id[original].restored_by == restore
    assert not by_id[original].restorable and by_id[restore].restorable  # a restore can be restored in turn


def test_retention_drops_old_and_excess_operations_with_their_status_lines(tmp_path):
    journal, clock = make(tmp_path, max_lines=3, max_days=10)
    old = journal.record("old.mp3", j.KIND_SAVE, {"t": ("a", "b")})
    clock.now += 20 * DAY
    ids = [journal.record(f"f{n}.mp3", j.KIND_SAVE, {"t": ("a", str(n))}) for n in range(5)]
    dropped = journal.prune()
    kept = [e.id for e in journal.entries()]
    assert dropped == 3 and kept == list(reversed(ids[2:]))
    assert old not in kept
    raw = [json.loads(l) for l in open(journal.path, encoding="utf-8").read().splitlines()]
    assert {l["id"] for l in raw} == set(ids[2:])  # done lines of dropped operations went with them


def test_the_journal_is_pruned_when_first_written_to_in_a_session(tmp_path):
    first, clock = make(tmp_path, max_days=1)
    first.record("old.mp3", j.KIND_SAVE, {"t": ("a", "b")})
    clock.now += 5 * DAY
    again = j.Journal(first.path, max_days=1, clock=clock)  # a new session
    again.record("new.mp3", j.KIND_SAVE, {"t": ("a", "b")})
    assert [os.path.basename(e.path) for e in again.entries()] == ["new.mp3"]


def test_concurrent_writers_never_interleave_lines(tmp_path):
    journal, _ = make(tmp_path)

    def work(n):
        for i in range(40):
            journal.record(f"{n}-{i}.mp3", j.KIND_REDACT, {"t": ("a", str(i))})

    threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    lines = open(journal.path, encoding="utf-8").read().splitlines()
    assert len(lines) == 6 * 40 * 2 and all(json.loads(l) for l in lines)
    assert len(journal.entries()) == 240


# -- the dialog ------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_the_history_dialog_lists_shows_details_and_restores(qapp, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    from redactor_common.gui.history_dialog import HistoryDialog

    target = tmp_path / "a.mp3"
    target.write_text("x")
    journal, _ = make(tmp_path)
    original = journal.record(str(target), j.KIND_SAVE, {"title": ("Old", "New")}, cover_changed=True)
    written = []

    def apply_restore(path, values):
        written.append((path, dict(values)))
        return ""

    dialog = HistoryDialog(journal, apply_restore)
    assert len(dialog.entries) == 1 and dialog.table.item(0, 3).text() == "title + cover"
    dialog.table.selectRow(0)
    text = dialog.detail.toPlainText()
    assert "was:  Old" in text and "now:  New" in text and "cover: changed" in text
    assert dialog.restore_button.isEnabled()

    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    assert dialog.restore_selected() is True
    assert written == [(str(target), {"title": "Old"})]
    kinds = [(e.kind, e.status) for e in journal.entries()]
    assert (j.KIND_RESTORE, j.STATUS_OK) in kinds and (j.KIND_SAVE, j.STATUS_RESTORED) in kinds  # journaled + marked
    assert dialog.table.rowCount() == 2


def test_a_failed_restore_changes_nothing_and_a_missing_file_is_refused(qapp, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    from redactor_common.gui.history_dialog import HistoryDialog

    target = tmp_path / "a.mp3"
    target.write_text("x")
    journal, _ = make(tmp_path)
    journal.record(str(target), j.KIND_SAVE, {"title": ("Old", "New")})
    warnings = []
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a[2])))
    dialog = HistoryDialog(journal, lambda path, values: "the tags did not stick")
    dialog.table.selectRow(0)
    assert dialog.restore_selected() is False and "did not stick" in warnings[0]
    assert len(journal.entries()) == 1  # nothing was journaled for a restore that did not happen
    target.unlink()
    assert dialog.restore_selected() is False and "not there any more" in warnings[1]


def test_tools_menu_history_item_is_optional_and_labelled():
    from redactor_common.core import labels
    from redactor_common.gui.standard_menus import standard_tools_items

    assert not [i for i in standard_tools_items(preferences=lambda: None) if getattr(i, "key", None) == "history"]
    item = [i for i in standard_tools_items(history=lambda: None) if getattr(i, "key", None) == "history"][0]
    assert item.text == labels.HISTORY == "&History…" and item.enabled


def test_fsync_only_when_durable(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "fsync", lambda fd: calls.append(fd))
    quick = j.Journal(str(tmp_path / "q.jsonl"))
    quick.record("a.mp3", j.KIND_SAVE, {"t": ("a", "b")})
    assert calls == []  # a Save All of thousands of files must not pay an fsync per line
    safe = j.Journal(str(tmp_path / "d.jsonl"), durable=True)
    safe.record("a.mp3", j.KIND_SAVE, {"t": ("a", "b")})
    assert len(calls) == 2  # the op line and the done line

"""Review of 2026-11-27, batch 1 (files at risk): cards 1-14, plus 39 and 41 that sat in the same code."""

import errno
import hashlib
import os
import sqlite3
import stat
import sys
import threading
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox, QProgressDialog, QWidget  # noqa: E402

from redactor_common.core import move_plan  # noqa: E402

_app = QApplication.instance() or QApplication([])


def _force_cross_volume(monkeypatch):
    real = move_plan.rename_no_clobber

    def fake(src, dst):
        if not os.path.basename(src).endswith(".part"):
            raise OSError(errno.EXDEV, "cross-device link")
        real(src, dst)
    monkeypatch.setattr(move_plan, "rename_no_clobber", fake)


# --- card 1: a cross-volume move compares every byte before the original goes ---

def test_files_identical_sees_a_difference_in_the_middle(tmp_path, monkeypatch):
    monkeypatch.setattr(move_plan, "COPY_CHUNK", 1024)
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    data = bytearray(os.urandom(300 * 1024))
    a.write_bytes(bytes(data))
    data[150 * 1024] ^= 0xFF  # outside the three 64 KB samples the old check read
    b.write_bytes(bytes(data))
    assert not move_plan.files_identical(str(a), str(b))
    b.write_bytes(a.read_bytes())
    assert move_plan.files_identical(str(a), str(b))


def test_a_damaged_copy_is_refused_and_the_original_is_not_trashed(tmp_path, monkeypatch):
    _force_cross_volume(monkeypatch)
    src = tmp_path / "big.mkv"
    src.write_bytes(os.urandom(400 * 1024))
    real_copy = move_plan._copy_chunked

    def damaging_copy(source, target, should_cancel):
        real_copy(source, target, should_cancel)
        with open(target, "r+b") as handle:
            handle.seek(200 * 1024)
            handle.write(b"\x00\x01\x02")
    monkeypatch.setattr(move_plan, "_copy_chunked", damaging_copy)
    dst = tmp_path / "other" / "big.mkv"
    with pytest.raises(OSError, match="didn't verify"):
        move_plan.execute_move(str(src), str(dst), trash=lambda p: pytest.fail("must not trash"))
    assert src.exists() and not dst.exists() and os.listdir(dst.parent) == []


def test_cancel_during_a_cross_volume_copy_leaves_nothing_behind(tmp_path, monkeypatch):
    _force_cross_volume(monkeypatch)
    src = tmp_path / "a.bin"
    src.write_bytes(b"x" * 10)
    dst = tmp_path / "d" / "a.bin"
    with pytest.raises(move_plan.MoveCancelled):
        move_plan.execute_move(str(src), str(dst), trash=lambda p: pytest.fail("no trash"), should_cancel=lambda: True)
    assert src.exists() and os.listdir(dst.parent) == []


# --- card 2: the fallback folder never piles up copies ---

def test_fallback_move_across_drives_removes_its_copy_when_the_original_is_locked(tmp_path, monkeypatch):
    from redactor_common.core import trash

    real = trash._rename_no_clobber

    def cross(src, dst):
        if not os.path.basename(src).endswith(".part"):
            raise OSError(errno.EXDEV, "cross-device link")
        real(src, dst)
    monkeypatch.setattr(trash, "_rename_no_clobber", cross)
    real_unlink = os.unlink
    original = tmp_path / "src" / "video.mkv"
    original.parent.mkdir()
    original.write_bytes(b"v" * 1000)

    def locked_unlink(path, *a, **k):
        if os.path.abspath(path) == os.path.abspath(str(original)):
            raise PermissionError(32, "in use")
        return real_unlink(path, *a, **k)
    monkeypatch.setattr(trash.os, "unlink", locked_unlink)
    folder = tmp_path / "quarantine"
    mover = trash.trash_with_retries(lambda p: trash.move_to_fallback_folder(p, str(folder)), attempts=4,
                                     sleep=lambda s: None)
    with pytest.raises(trash.TrashError):
        mover(str(original))
    left = [os.path.join(root, f) for root, _dirs, files in os.walk(folder) for f in files]
    assert left == [] and original.exists()


def test_fallback_move_on_the_same_drive_is_one_rename(tmp_path):
    from redactor_common.core import trash

    original = tmp_path / "a.txt"
    original.write_text("hi")
    moved = trash.move_to_fallback_folder(str(original), str(tmp_path / "q"))
    assert not original.exists() and open(moved).read() == "hi"


# --- card 3: Cancel during a threaded Redact keeps the window blocked until the file in hand is done ---

def test_threaded_redact_cancel_keeps_the_dialog_up_until_the_file_is_done():
    from redactor_common.core.pipeline import Recipe, Step, StepResult
    from redactor_common.gui.redact_dialog import run_redact

    class Ctx:
        def __init__(self, item):
            self.item = item
            self.step_options = {}

    started = threading.Event()
    done = []

    class Slow(Step):
        key = "slow"
        label = "Slow"

        def run(self, ctx):
            started.set()
            threading.Event().wait(0.8)
            done.append(ctx.item)
            return StepResult.applied("x")

    window = QWidget()
    seen = {}

    def press_cancel():
        if not started.is_set():
            QTimer.singleShot(50, press_cancel)
            return
        dialogs = [d for d in window.findChildren(QProgressDialog) if d.isVisible()]
        dialogs[0].canceled.emit()
        QTimer.singleShot(150, look)

    def look():
        visible = [d for d in window.findChildren(QProgressDialog) if d.isVisible()]
        seen["visible"] = bool(visible)
        seen["label"] = visible[0].labelText() if visible else ""
        seen["enabled"] = window.isEnabled()

    hooked = []
    QTimer.singleShot(100, press_cancel)
    steps = [Slow()]
    report = run_redact(window, ["a", "b", "c"], Recipe.default_for(steps), steps, Ctx, show_results=False,
                        on_cancel=lambda: hooked.append(1))
    assert done == ["a"]  # the file in hand finished; the others never started
    assert report.cancelled and report.not_processed == 2
    assert seen == {"visible": True, "label": seen["label"], "enabled": False} and seen["label"].startswith("Cancelling")
    assert hooked == [1]


# --- card 4: Move into Folders works on a worker thread ---

def test_move_runner_moves_off_the_gui_thread(tmp_path, monkeypatch):
    from redactor_common.gui import move_runner

    threads = []
    real = move_runner.execute_move

    def spy(*a, **k):
        threads.append(threading.current_thread())
        return real(*a, **k)
    monkeypatch.setattr(move_runner, "execute_move", spy)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No))
    src = tmp_path / "a.txt"
    src.write_text("x")
    plan = move_plan.PlannedMove(item="a", old_path=str(src), new_path=str(tmp_path / "A" / "a.txt"),
                                 root=str(tmp_path))
    summary = move_runner.run_planned_moves(None, [plan], False, None, "Move")
    assert len(summary.done) == 1 and threads and threads[0] is not threading.main_thread()


# --- card 5: notes in a folder whose name ends in a dot ---

@pytest.mark.skipif(sys.platform != "win32", reason="the trailing-dot trap is Windows-only")
def test_note_undo_works_in_a_folder_ending_in_a_dot(tmp_path):
    from redactor_common.core.long_paths import literal
    from redactor_common.core.notes_model import KIND_NOTE, undo_note_entry

    folder = os.path.join(str(tmp_path), "R.E.M.")
    os.mkdir(literal(folder))
    path = os.path.join(folder, "album.md")
    with open(literal(path), "wb") as handle:
        handle.write(b"# note")
    entry = types.SimpleNamespace(kind=KIND_NOTE, status="", id="1", path=path,
                                  note="sha256:" + hashlib.sha256(b"# note").hexdigest())
    marked = []
    journal = types.SimpleNamespace(mark_restored=lambda *a: marked.append(a))
    problem = undo_note_entry(entry, journal, trash=lambda p: os.remove(literal(p)))
    assert problem == "" and not os.path.exists(literal(path)) and marked


# --- card 6: one physical file is one member ---

@pytest.mark.skipif(sys.platform != "win32", reason="case folding is a Windows (and default macOS) trait")
def test_duplicates_key_folds_spellings_of_one_file(tmp_path):
    from redactor_common.gui.duplicates_dialog import _file_key

    target = tmp_path / "Song.mp3"
    target.write_bytes(b"x")
    assert _file_key(str(target)) == _file_key(str(target).upper())


def test_duplicates_key_sees_through_a_symlink(tmp_path):
    from redactor_common.gui.duplicates_dialog import _file_key

    target = tmp_path / "real.mp3"
    target.write_bytes(b"x")
    link = tmp_path / "link.mp3"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    assert _file_key(str(link)) == _file_key(str(target))


# --- cards 7 and 8: the undo log ---

def test_a_growing_batch_is_on_disk_before_the_run_ends(tmp_path):
    from redactor_common.core.rename_log import RenameLog

    log = RenameLog(str(tmp_path / "log.json"))
    log.record("older", [("x", "y")])
    recorder = log.open_batch("Move", root="r", flush_every=0)
    recorder.add("a", "b", created_dirs=["d"])
    on_disk = RenameLog(str(tmp_path / "log.json")).last_batch()
    assert on_disk.renames == [("a", "b")] and on_disk.created_dirs == ["d"]
    recorder.add("c", "d2", trashed=True)
    assert recorder.close()
    batches = log.batches()
    assert [b.label for b in batches] == ["older", "Move"]  # replaced in place, not appended twice
    assert batches[-1].renames == [("a", "b"), ("c", "d2")] and batches[-1].trashed == [("c", "d2")]


def test_a_locked_undo_log_is_not_written_over(tmp_path, monkeypatch):
    from redactor_common.core import rename_log

    log = rename_log.RenameLog(str(tmp_path / "log.json"))
    log.record("first", [("a", "b")])
    before = (tmp_path / "log.json").read_bytes()

    def locked(*a, **k):
        raise PermissionError(32, "in use")
    monkeypatch.setattr(rename_log, "retry_on_lock", locked)
    assert log.record("second", [("c", "d")]) is False
    assert (tmp_path / "log.json").read_bytes() == before
    assert log.undo_last().problems  # says it could not read the log, instead of "nothing to undo"


def test_a_damaged_undo_log_is_set_aside(tmp_path):
    from redactor_common.core.rename_log import RenameLog

    path = tmp_path / "log.json"
    path.write_text("{not json", encoding="utf-8")
    log = RenameLog(str(path))
    assert log.record("x", [("a", "b")]) is True
    assert (tmp_path / "log.json.damaged").exists() and len(log.batches()) == 1


# --- card 9: the "not duplicates" list ---

def test_dismissals_survive_a_locked_first_read(tmp_path, monkeypatch):
    from redactor_common.core import duplicates

    path = tmp_path / "dismissed.json"
    first = duplicates.JsonDismissStore(str(path))
    first.dismiss(["a", "b"])
    real = duplicates.retry_on_lock
    calls = {"n": 0}

    def locked_once(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(32, "in use")
        return real(*a, **k)
    monkeypatch.setattr(duplicates, "retry_on_lock", locked_once)
    store = duplicates.JsonDismissStore(str(path))
    store.dismiss(["c", "d"])
    fresh = duplicates.JsonDismissStore(str(path))
    assert fresh.is_dismissed(["a", "b"]) and fresh.is_dismissed(["c", "d"])


# --- card 10: audio_write's leftovers are recognised ---

def test_audio_write_temp_names_are_app_temp_names():
    from redactor_common.core.temp_names import is_app_temp_name

    assert is_app_temp_name(".redactor-ab12cd.tmp.mp3")
    assert is_app_temp_name(".folderredactor-x.tmp")
    assert is_app_temp_name(".mp3redactor-x.tmp")
    assert not is_app_temp_name("redactor-notes.mp3")


# --- card 11: rewrite_archive does not replace an unrelated file ---

def test_rewrite_archive_refuses_an_existing_other_target(tmp_path):
    import zipfile

    from redactor_common.core.zip_rewrite import RewriteError, RewritePlan, rewrite_archive

    src = tmp_path / "a.cbr.zip"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("1.jpg", b"img")
    dst = tmp_path / "a.cbz"
    dst.write_bytes(b"someone else's comic")
    with pytest.raises(RewriteError, match="already exists"):
        rewrite_archive(str(src), str(dst), RewritePlan())
    assert dst.read_bytes() == b"someone else's comic"
    rewrite_archive(str(src), str(dst), RewritePlan(), overwrite=True)  # a Save As the user confirmed
    assert zipfile.ZipFile(dst).namelist() == ["1.jpg"]


# --- card 12: a save keeps the read-only flag ---

def test_commit_in_place_keeps_a_read_only_flag(tmp_path):
    from redactor_common.core.pipeline import commit_in_place

    original = tmp_path / "book.cbz"
    original.write_bytes(b"old")
    os.chmod(original, stat.S_IREAD)
    new = tmp_path / "book.tmp"
    new.write_bytes(b"new content")
    try:
        commit_in_place(str(original), str(new), trash=lambda p: (os.chmod(p, stat.S_IWRITE | stat.S_IREAD),
                                                                  os.remove(p)))
        assert original.read_bytes() == b"new content"
        assert not os.stat(original).st_mode & stat.S_IWRITE
    finally:
        os.chmod(original, stat.S_IWRITE | stat.S_IREAD)


def test_swap_in_keeps_a_read_only_flag(tmp_path):
    from redactor_common.core.zip_rewrite import swap_in

    original = tmp_path / "song.flac"
    original.write_bytes(b"old")
    os.chmod(original, stat.S_IREAD)
    new = tmp_path / "song.tmp"
    new.write_bytes(b"new")
    try:
        swap_in(str(new), str(original), lambda p: (os.chmod(p, stat.S_IWRITE | stat.S_IREAD), os.remove(p)))
        assert original.read_bytes() == b"new" and not os.stat(original).st_mode & stat.S_IWRITE
    finally:
        os.chmod(original, stat.S_IWRITE | stat.S_IREAD)


# --- card 13: a finished database build is kept when it cannot replace the old one ---

def test_a_failed_final_rename_keeps_the_build(tmp_path, monkeypatch):
    from redactor_common.core import dump_import

    def refuse(*a, **k):
        raise PermissionError(32, "in use")
    monkeypatch.setattr(dump_import, "replace_with_retry", refuse)
    dest = tmp_path / "gcd.db"
    dest.write_bytes(b"old database")
    with pytest.raises(dump_import.DumpImportError, match="kept as"):
        with dump_import.SqliteBuilder(str(dest), {"t": ["id integer"]}) as out:
            out.add("t", (1,))
            out.finish()
    assert dest.read_bytes() == b"old database"
    con = sqlite3.connect(str(dest) + ".partial")
    assert con.execute("select count(*) from t").fetchone() == (1,)
    con.close()


def test_a_failing_enter_leaves_no_partial_file(tmp_path):
    from redactor_common.core.dump_import import SqliteBuilder

    with pytest.raises(sqlite3.Error):
        with SqliteBuilder(str(tmp_path / "x.db"), {"t": ["id integer", "bad bad bad ("]}):
            pass
    assert not (tmp_path / "x.db.partial").exists()


# --- card 14: a busy cache is not deleted ---

def test_a_locked_cache_is_kept(tmp_path):
    from redactor_common.core.notes_cache import ResponseCache

    cache = ResponseCache(tmp_path / "cache.sqlite")
    cache.put("k", b'{"a": 1}')

    def busy(_db):
        raise sqlite3.OperationalError("database is locked")
    assert cache._guard(busy, default="d") == "d"
    assert (tmp_path / "cache.sqlite").exists() and cache.get("k") is not None


def test_a_damaged_cache_is_still_dropped(tmp_path):
    from redactor_common.core.notes_cache import _is_damaged

    assert _is_damaged(sqlite3.DatabaseError("file is not a database"))
    assert not _is_damaged(sqlite3.OperationalError("database is locked"))


# --- card 39: the report does not list unsaved changes as made ---

def test_a_skipped_file_lists_its_changes_as_not_saved():
    from redactor_common.core.pipeline import Recipe, Step, StepResult, run_recipe_on_item

    class Ctx:
        def __init__(self, item):
            self.item = item
            self.step_options = {}

    class Change(Step):
        key = "change"
        label = "Change"

        def run(self, ctx):
            return StepResult.applied("changed title")

    class Skip(Step):
        key = "skip"
        label = "Skip"

        def run(self, ctx):
            return StepResult.skipped("unsaved edits")

    steps = [Change(), Skip()]
    entry = run_recipe_on_item("f1", Recipe.default_for(steps).resolve(steps), 0.9, Ctx,
                               finalize=lambda ctx, report: pytest.fail("no save for a skipped file"))
    assert entry.applied == [] and any("changed title" in c for c in entry.not_saved)


# --- card 41: a change of capitals only is done ---

@pytest.mark.skipif(sys.platform != "win32", reason="needs a filesystem that ignores case")
def test_a_case_only_move_renames_the_file(tmp_path):
    src = tmp_path / "the beatles.mp3"
    src.write_text("x")
    new = str(tmp_path / "The Beatles.mp3")
    plan = move_plan.PlannedMove(item=None, old_path=str(src), new_path=new, root=str(tmp_path))
    assert not plan.is_noop
    result = move_plan.execute_move(plan.old_path, plan.new_path)
    assert os.listdir(tmp_path) == ["The Beatles.mp3"] and result.new_path == new and not result.warning


@pytest.mark.skipif(sys.platform != "win32", reason="needs a filesystem that ignores case")
def test_a_case_only_folder_change_says_the_folder_kept_its_spelling(tmp_path):
    (tmp_path / "abba").mkdir()
    src = tmp_path / "abba" / "song.mp3"
    src.write_text("x")
    result = move_plan.execute_move(str(src), str(tmp_path / "ABBA" / "song.mp3"))
    assert src.exists() and "still spelled" in result.warning

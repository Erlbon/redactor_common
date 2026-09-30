"""Regression tests for the 2026-09-30 code review fixes."""

import os
import sqlite3
import sys
import threading
import urllib.error

import pytest

from redactor_common.core import crash_log, lookup_client, rename_pattern
from redactor_common.core.local_db import LocalDatabase, LocalDatabaseError, NameIndex, normalize_words
from redactor_common.core.os_utils import rename_no_clobber
from redactor_common.core.rename_log import RenameLog


# -- unique_path / rename planning ----------------------------------------------------------------

def test_unique_path_treats_own_path_as_free(tmp_path):
    own = tmp_path / "Name.cbz"
    own.write_bytes(b"x")
    assert rename_pattern.unique_path(str(tmp_path), "Name", ".cbz", set(), own_path=str(own)) == str(own)
    # without own_path it's a collision, as before
    assert rename_pattern.unique_path(str(tmp_path), "Name", ".cbz", set()) == str(tmp_path / "Name (2).cbz")


def test_unique_path_own_path_still_respects_taken(tmp_path):
    own = tmp_path / "Name.cbz"
    own.write_bytes(b"x")
    taken = {os.path.normcase(os.path.abspath(own))}
    assert rename_pattern.unique_path(str(tmp_path), "Name", ".cbz", taken, own_path=str(own)) == str(tmp_path / "Name (2).cbz")


def test_rename_dialog_keeps_already_conforming_names(tmp_path):
    from redactor_common.gui.rename_pattern_dialog import RenamePatternDialog

    (tmp_path / "Same.cbz").write_bytes(b"x")
    items = [str(tmp_path / "Same.cbz")]
    dialog = RenamePatternDialog(
        items, [("title", "Title")], lambda item: {"title": "Same"}, lambda item: item,
        pattern_history=["%title%"], default_pattern="%title%",
    )
    assert [new for _i, _old, new in dialog.planned_renames()] == items


@pytest.mark.skipif(os.path.normcase("A") != "a", reason="case-insensitive filesystems only")
def test_case_only_rename_really_renames(tmp_path):
    path = tmp_path / "a.cbz"
    path.write_bytes(b"x")
    new = rename_pattern.rename_file_on_disk(str(path), "A")
    assert os.path.basename(new) == "A.cbz"
    assert "A.cbz" in os.listdir(tmp_path)


def test_rename_file_on_disk_refuses_existing_target(tmp_path):
    (tmp_path / "a.cbz").write_bytes(b"1")
    (tmp_path / "b.cbz").write_bytes(b"2")
    with pytest.raises(FileExistsError):
        rename_pattern.rename_file_on_disk(str(tmp_path / "a.cbz"), "b")
    assert (tmp_path / "b.cbz").read_bytes() == b"2"


def test_rename_no_clobber_never_overwrites(tmp_path):
    (tmp_path / "a").write_bytes(b"1")
    (tmp_path / "b").write_bytes(b"2")
    with pytest.raises(FileExistsError):
        rename_no_clobber(str(tmp_path / "a"), str(tmp_path / "b"))
    assert (tmp_path / "a").read_bytes() == b"1" and (tmp_path / "b").read_bytes() == b"2"
    rename_no_clobber(str(tmp_path / "a"), str(tmp_path / "c"))
    assert not (tmp_path / "a").exists() and (tmp_path / "c").read_bytes() == b"1"


@pytest.mark.skipif(os.path.normcase("A") != "a", reason="case-insensitive filesystems only")
def test_rename_log_records_and_undoes_case_only_rename(tmp_path):
    path = tmp_path / "a.cbz"
    path.write_bytes(b"x")
    new = rename_pattern.rename_file_on_disk(str(path), "A")
    log = RenameLog(str(tmp_path / "log.json"))
    log.record("case", [(str(path), new)])
    assert log.last_batch() is not None
    result = log.undo_last()
    assert result.restored and "a.cbz" in os.listdir(tmp_path)


def test_render_filename_truncation_leaves_no_trailing_dot_or_separator():
    limit = rename_pattern.MAX_FILENAME_LENGTH
    for tail in (". y", " - y", " . y"):
        name = rename_pattern.render_filename({"t": "x" * (limit - 1 - (len(tail) - 2))}, "%t%" + tail)
        assert len(name) <= limit
        assert name == name.rstrip(" .-") or name.rstrip(" .-") == name.rstrip()
        assert not name.endswith((".", " "))
    name = rename_pattern.render_filename({"t": "x" * (limit - 1)}, "%t%. y")
    assert not name.endswith(".")


# -- local_db -------------------------------------------------------------------------------------

@pytest.mark.parametrize("folder", ["Comics #1", "100%25 done", "a b"])
def test_local_database_opens_paths_with_uri_special_characters(tmp_path, folder):
    directory = tmp_path / folder
    directory.mkdir()
    path = directory / "db.sqlite"
    con = sqlite3.connect(path)
    con.execute("create table series (id integer)")
    con.commit()
    con.close()
    db = LocalDatabase(str(path), required_tables=["series"])
    assert db.query("select count(*) from series") == [(0,)]
    db.close()


def test_local_database_closes_connection_on_unreadable_file(tmp_path):
    path = tmp_path / "junk.sqlite"
    path.write_bytes(b"this is not a database" * 100)
    with pytest.raises(LocalDatabaseError):
        LocalDatabase(str(path), required_tables=["series"])
    path.unlink()  # would fail on Windows if the connection were leaked


def test_normalize_words_keeps_non_ascii_letters():
    assert normalize_words("Amélie") == "amelie"
    assert normalize_words("Война и мир") == "война и мир"
    assert normalize_words("進撃の巨人") != ""
    assert normalize_words("G.I. Joe: A Real American Hero") == normalize_words("G.I. Joe - A Real American Hero")


def test_name_index_matches_non_ascii_and_survives_quotes():
    index = NameIndex([(1, "Amélie"), (2, "Война и мир"), (3, "Plain")])
    assert index.match("amelie") == [1]
    assert index.match("война") == [2]
    assert NameIndex([(1, 'say "hi"')], normalize=lambda s: s.lower()).match('"hi"') in ([1], [])


# -- lookup_client --------------------------------------------------------------------------------

def test_default_fetch_rejects_non_http_urls(tmp_path):
    target = tmp_path / "secret.txt"
    target.write_text("secret")
    fetch = lookup_client.make_default_fetch("test")
    for url in (target.as_uri(), "ftp://example.com/x"):
        with pytest.raises(lookup_client.LookupError):
            lookup_client.fetch_bytes(url, fetch)


def test_fetch_helpers_translate_truncated_reads_and_bad_urls():
    import http.client

    def truncated(_url):
        raise http.client.IncompleteRead(b"abc")

    def bad_url(_url):
        raise ValueError("unknown url type")

    for fn in (truncated, bad_url):
        with pytest.raises(lookup_client.LookupError):
            lookup_client.fetch_json("http://x", fn)
        with pytest.raises(lookup_client.LookupError):
            lookup_client.fetch_bytes("http://x", fn)


# -- crash hooks ----------------------------------------------------------------------------------

def test_crash_install_also_logs_worker_thread_exceptions(tmp_path, monkeypatch):
    log = tmp_path / "crash.log"
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    monkeypatch.setattr(crash_log, "_faulthandler_file", None)
    crash_log.install(log, enable_faulthandler=False)

    def boom():
        raise RuntimeError("worker exploded")

    thread = threading.Thread(target=boom)
    thread.start()
    thread.join()
    assert "worker exploded" in log.read_text(encoding="utf-8")


# -- progress -------------------------------------------------------------------------------------

def test_run_with_progress_closes_dialog_when_a_step_raises(monkeypatch):
    from redactor_common.gui import progress

    class FakeDialog:
        closed = False

        def wasCanceled(self):
            return False

        def setLabelText(self, _text):
            pass

        def setValue(self, _value):
            pass

        def close(self):
            FakeDialog.closed = True

    monkeypatch.setattr(progress, "_make_dialog", lambda *a, **k: FakeDialog())

    def step(item, _index):
        raise RuntimeError("corrupt book")

    with pytest.raises(RuntimeError):
        progress.run_with_progress(None, [1, 2, 3], step, "Working...")
    assert FakeDialog.closed


# -- open in default app --------------------------------------------------------------------------

def test_open_with_default_app_launches_existing_file_only(tmp_path, monkeypatch):
    from redactor_common.core import os_utils

    launched = []
    monkeypatch.setattr(os_utils.subprocess, "Popen", lambda args: launched.append(args))
    monkeypatch.setattr(os_utils.sys, "platform", "linux")
    f = tmp_path / "book.cbz"
    f.write_bytes(b"x")
    assert os_utils.open_with_default_app(str(f)) is True
    assert launched == [["xdg-open", str(f)]]
    assert os_utils.open_with_default_app(str(tmp_path / "gone.cbz")) is False
    assert os_utils.open_with_default_app("") is False


def test_open_with_default_app_reports_missing_handler(tmp_path, monkeypatch):
    from redactor_common.core import os_utils

    def boom(_args):
        raise FileNotFoundError("xdg-open")

    monkeypatch.setattr(os_utils.subprocess, "Popen", boom)
    monkeypatch.setattr(os_utils.sys, "platform", "linux")
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    assert os_utils.open_with_default_app(str(f)) is False


# -- every module imports -------------------------------------------------------------------------

def test_every_module_imports():
    """A syntax error in a module no other test touches (the context menu
    once shipped broken that way) must fail here, not in the apps."""
    import importlib
    import pkgutil

    import redactor_common

    failures = []
    for info in pkgutil.walk_packages(redactor_common.__path__, "redactor_common."):
        try:
            importlib.import_module(info.name)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{info.name}: {exc!r}")
    assert not failures, failures

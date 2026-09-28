"""Tests for core/local_db.py and gui/local_db_settings_dialog.py."""

import os
import sqlite3

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from redactor_common.core.local_db import (  # noqa: E402
    SQLITE_VARIABLE_CHUNK,
    LocalDatabase,
    LocalDatabaseError,
    NameIndex,
    normalize_words,
    open_cached,
    year_gap,
)

# Module level: an unreferenced QApplication is garbage-collected at once.
_app = QApplication.instance() or QApplication([])


class MyError(LocalDatabaseError):
    pass


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "source.db"
    con = sqlite3.connect(path)
    con.execute("create table titles (id integer primary key, name text)")
    con.execute("create table extra (id integer primary key)")
    con.executemany("insert into titles values (?, ?)", [
        (1, "G.I. Joe: A Real American Hero"),
        (2, "Marvel Mangaverse: Ghostlocke"),
        (3, "Batman"),
        (4, "Batman & Robin"),
    ])
    con.commit()
    con.close()
    return str(path)


def test_normalize_words_ignores_punctuation_and_ampersand():
    assert normalize_words("G.I. Joe - A Real American Hero") == normalize_words("G.I. Joe: A Real American Hero")
    assert normalize_words("Batman & Robin") == "batman and robin"


def test_year_gap():
    assert year_gap(2018, "2016") == 2
    assert year_gap("", 2016) == 9999
    assert year_gap("20xx", "2016") == 9999


def test_name_index_matches_all_words_in_fuller_titles():
    index = NameIndex([(1, "Marvel Mangaverse: Ghostlocke"), (2, "Ghost Rider")])
    assert index.match("Mangaverse - Ghostlocke") == [1]
    assert sorted(index.match("ghost")) == [2]  # whole words only
    assert index.match("") == []


def test_opens_read_only_and_checks_tables(db_path, tmp_path):
    db = LocalDatabase(db_path, {"titles"}, error_cls=MyError)
    assert db.query_one("select name from titles where id = ?", (3,)) == ("Batman",)
    with pytest.raises(MyError, match="Database query failed"):
        db.query("insert into titles values (9, 'x')")  # read-only
    with pytest.raises(MyError, match="doesn't look like a test dump"):
        LocalDatabase(db_path, {"titles", "missing_table"}, kind="a test dump", error_cls=MyError)
    with pytest.raises(MyError, match="not found"):
        LocalDatabase(str(tmp_path / "nope.db"), error_cls=MyError)
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not sqlite at all" * 100)
    with pytest.raises(MyError, match="Not a readable SQLite"):
        LocalDatabase(str(garbage), {"titles"}, error_cls=MyError)


def test_cached_name_index(db_path):
    db = LocalDatabase(db_path, {"titles"})
    index = db.name_index("titles", "select id, name from titles")
    assert db.name_index("titles", "ignored -- already built") is index
    assert index.match("G.I. Joe - A Real American Hero") == [1]
    assert sorted(index.match("Batman")) == [3, 4]


def test_query_in_chunks_past_the_variable_limit(db_path):
    db = LocalDatabase(db_path, {"titles"})
    ids = list(range(1, SQLITE_VARIABLE_CHUNK * 2 + 10))
    rows = db.query_in("select id from titles where id in ({ids}) and name != ? order by id", ids, after=["x"])
    assert [r[0] for r in rows] == [1, 2, 3, 4]


def test_open_cached_reuses_per_factory_and_path(db_path):
    first = open_cached(db_path, lambda p: LocalDatabase(p, {"titles"}))
    factory = lambda p: LocalDatabase(p, {"titles"})  # noqa: E731
    assert open_cached(db_path, factory) is open_cached(db_path, factory)
    assert open_cached(db_path, factory) is not first  # a different factory -> its own


def test_settings_dialog_check_and_save(db_path):
    from redactor_common.gui.local_db_settings_dialog import LocalDatabaseSettingsDialog

    saved = []

    def check(path):
        db = LocalDatabase(path, {"titles"}, kind="a test dump", error_cls=MyError)
        return f"{db.query_one('select count(*) from titles')[0]} titles"

    dialog = LocalDatabaseSettingsDialog(
        title="Test DB", instructions_html="Get it <a href='https://example.org'>here</a>.",
        path=db_path, check=check, save=saved.append, error_types=(MyError,),
    )
    assert dialog.check_result() == (True, "4 titles")
    dialog.path_edit.setText("C:/does/not/exist.db")
    ok, message = dialog.check_result()
    assert not ok and "not found" in message
    dialog.accept()
    assert saved == ["C:/does/not/exist.db"]


def test_forget_cached_closes_and_reopens(tmp_path):
    from redactor_common.core.local_db import forget_cached

    path = tmp_path / "f.db"
    con = sqlite3.connect(path)
    con.execute("create table t (x)")
    con.commit()
    con.close()
    first = open_cached(str(path), LocalDatabase)
    forget_cached(str(path))
    with pytest.raises(sqlite3.ProgrammingError):
        first._con.execute("select 1")  # closed
    assert open_cached(str(path), LocalDatabase) is not first
    forget_cached(str(path))
    forget_cached("")  # nothing cached: no-op

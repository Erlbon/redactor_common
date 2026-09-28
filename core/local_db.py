"""
redactor_common/core/local_db.py

Read-only access to a large LOCAL metadata database -- the user's own
downloaded copy of a source's data (the Grand Comics Database's SQLite
dump for cbzredactor; later a converted ComicRack library, a Calibre
library's metadata.db, ...), queried instead of an online API:
milliseconds per lookup, offline, no rate limit. The app never bundles
or writes to such a file; each app supplies its own queries and field
mapping on top of this.

What's shared, learned on GCD's 6.7 GB dump (2026-09-28):

- LocalDatabase opens the file READ-ONLY (SQLite URI mode=ro), checks
  it has the tables the app needs, and turns every failure into one
  error type with a message a user can act on.
- NameIndex: an in-memory SQLite FTS5 index over one "names" query
  (series titles, book titles, ...), built on first use -- ~1.5 s for
  230k names -- and kept for the session. Matching is "all these words
  appear", punctuation-insensitive (normalize_words), so a scene-style
  "G.I. Joe - A Real American Hero" finds "G.I. Joe: A Real American
  Hero" and "Mangaverse - Ghostlocke" finds "Marvel Mangaverse:
  Ghostlocke".
- query_in() splits a long id list into chunks under SQLite's
  bound-variable limit (a common word can match thousands of names).
- Index steering: dumps often ship without SQLite's index statistics
  (no sqlite_stat1), and then SQLite may pick a useless index -- on
  GCD it chose "deleted" over "story_id" and a credits query took 35 s
  instead of 8 ms. Write "+column" (unary plus) for low-selectivity
  filter columns to keep them out of index selection. See the GCD
  queries in cbzredactor's core/gcd_local.py for worked examples.
- One lock around the connection: lookups run on worker threads
  (redactor_common.gui.background_call) one at a time, so a single
  connection opened with check_same_thread=False is enough.
- open_cached(): one opened database per file per session, so the name
  index is only ever built once.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from typing import Callable, Iterable, Optional, Type, TypeVar

SQLITE_VARIABLE_CHUNK = 5000  # well under SQLite's bound-variable limit (32766)


class LocalDatabaseError(Exception):
    """The local database file is missing, unreadable, or not the kind
    expected. Apps subclass it (e.g. GcdLocalError) and pass the subclass
    as `error_cls`, so their callers keep catching their own type."""


def normalize_words(name: str) -> str:
    """Lower-case letters and digits only, words separated by single
    spaces; "&" counts as "and". "G.I. Joe - A Real American Hero" and
    "G.I. Joe: A Real American Hero" normalize alike."""
    text = (name or "").casefold().replace("&", " and ")
    return re.sub(r"[^0-9a-z]+", " ", text).strip()


def year_gap(a, b) -> int:
    """|a - b| for two year-ish values (ints or digit strings); 9999 when
    either isn't a number -- for ranking "closest year first"."""
    a, b = str(a or ""), str(b or "")
    return abs(int(a) - int(b)) if a.isdigit() and b.isdigit() else 9999


class NameIndex:
    """In-memory full-text index over (id, name) pairs. match(text)
    returns the ids whose normalized name contains every word of
    normalize_words(text), in no particular order -- rank them yourself."""

    def __init__(self, rows: Iterable[tuple[object, str]], normalize: Callable[[str], str] = normalize_words):
        self._normalize = normalize
        self._con = sqlite3.connect(":memory:", check_same_thread=False)
        self._con.execute("create virtual table names using fts5(norm, key unindexed, tokenize='unicode61')")
        self._con.executemany(
            "insert into names(norm, key) values (?, ?)",
            ((normalize(name), key) for key, name in rows),
        )
        self._con.commit()

    def match(self, text: str) -> list:
        words = self._normalize(text).split()
        if not words:
            return []
        query = " ".join(f'"{word}"' for word in words)
        return [row[0] for row in self._con.execute("select key from names where names match ?", (query,))]

    def close(self) -> None:
        self._con.close()


E = TypeVar("E", bound=LocalDatabaseError)


class LocalDatabase:
    """A read-only SQLite file with the tables an app expects.

    `required_tables`: checked on open; a file missing any of them is
    rejected with a clear message naming `kind` ("a GCD SQLite dump").
    `error_cls`: the LocalDatabaseError subclass raised for every problem.
    """

    def __init__(
        self,
        path: str,
        required_tables: Iterable[str] = (),
        kind: str = "the expected database",
        error_cls: Type[LocalDatabaseError] = LocalDatabaseError,
    ):
        self.error_cls = error_cls
        if not path or not os.path.isfile(path):
            raise error_cls(f"Database file not found: {path or '(not set)'}")
        self.path = path
        try:
            self._con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
            tables = {r[0] for r in self._con.execute("select name from sqlite_master where type='table'")}
        except sqlite3.DatabaseError as exc:
            raise error_cls(f"Not a readable SQLite database: {exc}") from exc
        missing = set(required_tables) - tables
        if missing:
            self._con.close()
            raise error_cls(f"This doesn't look like {kind} (missing tables: {', '.join(sorted(missing))}).")
        self.lock = threading.RLock()
        self._name_indexes: dict[str, NameIndex] = {}

    def query(self, sql: str, params: Iterable = ()) -> list[tuple]:
        with self.lock:
            try:
                return self._con.execute(sql, tuple(params)).fetchall()
            except sqlite3.DatabaseError as exc:
                raise self.error_cls(f"Database query failed: {exc}") from exc

    def query_one(self, sql: str, params: Iterable = ()) -> Optional[tuple]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def query_in(self, sql: str, ids: list, before: Iterable = (), after: Iterable = ()) -> list[tuple]:
        """Runs `sql` -- which contains one "{ids}" placeholder for an
        IN (...) list -- over `ids` in chunks, concatenating the rows.
        `before`/`after`: bound parameters that come before/after the id
        list in the statement."""
        rows: list[tuple] = []
        before, after = list(before), list(after)
        for start in range(0, len(ids), SQLITE_VARIABLE_CHUNK):
            chunk = ids[start:start + SQLITE_VARIABLE_CHUNK]
            rows += self.query(sql.replace("{ids}", ",".join("?" * len(chunk))), [*before, *chunk, *after])
        return rows

    def name_index(self, key: str, names_sql: str, normalize: Callable[[str], str] = normalize_words) -> NameIndex:
        """The session's NameIndex for `names_sql` (a query returning
        (id, name) rows), built on first use and cached under `key`."""
        with self.lock:
            index = self._name_indexes.get(key)
            if index is None:
                index = NameIndex(self._con.execute(names_sql), normalize)
                self._name_indexes[key] = index
            return index

    def close(self) -> None:
        with self.lock:
            for index in self._name_indexes.values():
                index.close()
            self._name_indexes.clear()
            self._con.close()


_open: dict[tuple[object, str], LocalDatabase] = {}
_open_lock = threading.Lock()

D = TypeVar("D", bound=LocalDatabase)


def open_cached(path: str, factory: Callable[[str], D]) -> D:
    """One opened database per (factory, file) for the whole session, so
    a name index is only built once. `factory(path)` opens it (e.g. the
    app's LocalDatabase subclass)."""
    key = (factory, os.path.normcase(os.path.abspath(path)) if path else "")
    with _open_lock:
        db = _open.get(key)
        if db is None:
            db = factory(path)
            _open[key] = db
        return db


def forget_cached(path: str) -> None:
    """Closes and forgets every open_cached() database for `path` -- before
    the file is rebuilt or replaced (Windows won't replace an open file),
    and so the next lookup opens the new one."""
    wanted = os.path.normcase(os.path.abspath(path)) if path else ""
    with _open_lock:
        for key in [k for k in _open if k[1] == wanted]:
            _open.pop(key).close()

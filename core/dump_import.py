"""
redactor_common/core/dump_import.py

Builds a compact, indexed SQLite file from a big metadata DUMP in some
other format -- a ComicRack library's ComicDb.xml first; later
MusicBrainz's PostgreSQL tables, Open Library's TSV+JSON files, ... --
so apps can query it through core/local_db.py like the GCD dump.

Three layers; each app supplies the middle part (its "recipe": which
records to keep and how they map to its tables):

1. open_dump(): opens the file for streaming -- plain, .gz, .bz2, .xz,
   or a .zip holding one file (as a user naturally copies a big XML
   between machines) -- without unpacking it to disk, and reports how
   far through it is for the progress bar.
2. Readers turn the stream into records. iter_xml_records() yields one
   element per record (e.g. ComicRack's <Book>) and frees each one
   after use, so a 437 MB / 234k-book library streams through in ~8 s
   with flat memory.
3. SqliteBuilder writes the tables: bulk-load settings, rows in
   batches, indexes only after the load, ANALYZE at the end (so SQLite
   picks good indexes -- the GCD dump ships without statistics, which
   cost a 35 s query), and an import-info table (source, date, row
   counts) so an app can tell the user what the file was built from.
   It writes to "<dest>.partial" and renames at the very end: a cancel
   or a crash never leaves a half-built database under the real name.

Cancelling: every reader takes `cancelled` (a no-argument callable,
e.g. threading.Event.is_set) and raises ImportCancelled when it turns
true, so the worker thread really stops -- see
gui/dump_import_runner.py for the progress dialog around all this.
"""

from __future__ import annotations

import bz2
import datetime
import gzip
import io
import lzma
import os
import sqlite3
import zipfile
from contextlib import contextmanager
from typing import Callable, Iterable, Iterator, Optional
from xml.etree import ElementTree as ET

ProgressFn = Callable[[float], None]
CancelledFn = Callable[[], bool]

INFO_TABLE = "redactor_import_info"


class DumpImportError(Exception):
    """The dump can't be read or doesn't have the expected shape."""


class ImportCancelled(Exception):
    """The user cancelled the import (the partial output was removed)."""


class _CountingReader(io.RawIOBase):
    """A read-only binary stream that counts the bytes read through it."""

    def __init__(self, raw):
        self._raw = raw
        self.count = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        data = self._raw.read(len(buffer))
        n = len(data)
        buffer[:n] = data
        self.count += n
        return n


class DumpStream:
    """An opened dump: `stream` (binary, decompressed) and `fraction()`,
    how far through the file reading has got (0..1)."""

    def __init__(self, stream, counter: _CountingReader, total: int):
        self.stream = stream
        self._counter = counter
        self._total = max(total, 1)

    def fraction(self) -> float:
        return min(self._counter.count / self._total, 1.0)


@contextmanager
def open_dump(path: str, member_suffix: str = "") -> Iterator[DumpStream]:
    """Opens `path` for streaming, decompressing by its extension. A .zip
    must hold one file (or one ending in `member_suffix`, e.g. ".xml").
    Progress counts compressed bytes for gz/bz2/xz (their uncompressed
    size isn't known up front) and uncompressed bytes for zip members."""
    if not path or not os.path.isfile(path):
        raise DumpImportError(f"File not found: {path or '(not set)'}")
    lower = path.lower()
    try:
        if lower.endswith(".zip"):
            with zipfile.ZipFile(path) as archive:
                members = [m for m in archive.infolist() if not m.is_dir()]
                if member_suffix:
                    members = [m for m in members if m.filename.lower().endswith(member_suffix.lower())]
                if len(members) != 1:
                    raise DumpImportError(
                        f"Expected one{' ' + member_suffix if member_suffix else ''} file inside "
                        f"{os.path.basename(path)}, found {len(members)}."
                    )
                with archive.open(members[0]) as raw:
                    counter = _CountingReader(raw)
                    yield DumpStream(io.BufferedReader(counter, 1 << 20), counter, members[0].file_size)
            return
        with open(path, "rb") as raw:
            counter = _CountingReader(raw)
            buffered = io.BufferedReader(counter, 1 << 20)
            total = os.path.getsize(path)
            if lower.endswith(".gz"):
                yield DumpStream(gzip.GzipFile(fileobj=buffered), counter, total)
            elif lower.endswith(".bz2"):
                yield DumpStream(bz2.BZ2File(buffered), counter, total)
            elif lower.endswith(".xz"):
                yield DumpStream(lzma.LZMAFile(buffered), counter, total)
            else:
                yield DumpStream(buffered, counter, total)
    except (OSError, zipfile.BadZipFile, EOFError, lzma.LZMAError) as exc:
        raise DumpImportError(f"Couldn't read {os.path.basename(path)}: {exc}") from exc


def iter_xml_records(
    dump: DumpStream,
    tag: str,
    progress: Optional[ProgressFn] = None,
    cancelled: Optional[CancelledFn] = None,
    check_every: int = 500,
) -> Iterator[ET.Element]:
    """Yields every <`tag`> element of the XML stream, complete with its
    children, then frees it -- read what you need inside the loop body.
    Namespaces are ignored for matching (ComicRack's file declares some
    it doesn't use on its elements). Calls `progress(fraction)` and checks
    `cancelled()` every `check_every` records."""
    stack: list[ET.Element] = []
    seen = 0
    try:
        for event, element in ET.iterparse(dump.stream, events=("start", "end")):
            if event == "start":
                stack.append(element)
                continue
            stack.pop()
            if element.tag.rsplit("}", 1)[-1] != tag:
                continue
            yield element
            # Free the record and drop it from its parent, so neither its
            # content nor 234k empty shells pile up in memory.
            element.clear()
            if stack:
                stack[-1].remove(element)
            seen += 1
            if seen % check_every == 0:
                if cancelled and cancelled():
                    raise ImportCancelled()
                if progress:
                    progress(dump.fraction())
    except ET.ParseError as exc:
        raise DumpImportError(f"The XML is damaged or incomplete: {exc}") from exc
    if progress:
        progress(1.0)


def split_list(text: Optional[str], separator: str = ",") -> list[str]:
    """"a, b,, c " -> ["a", "b", "c"], order kept, duplicates dropped."""
    return list(dict.fromkeys(p.strip() for p in (text or "").split(separator) if p.strip()))


class SqliteBuilder:
    """Writes a new SQLite database at `dest` (replacing any old file
    there only once the build has fully succeeded).

    `tables`: {table: ["id integer primary key", "name text", ...]} --
    add() takes each row as a tuple in that column order, and a row of the
    wrong length fails loudly (a recipe out of step with its schema).
    `indexes`: CREATE INDEX statements, run after the load.

        with SqliteBuilder(dest, tables, indexes) as out:
            out.add("series", (1, "Batman"))
            out.finish({"source": "ComicRack"})
    """

    def __init__(self, dest: str, tables: dict[str, list[str]], indexes: Iterable[str] = (), batch: int = 5000):
        self.dest = dest
        self.partial = dest + ".partial"
        self._tables = tables
        self._widths = {name: len(columns) for name, columns in tables.items()}
        self._indexes = list(indexes)
        self._batch = batch
        self._pending: dict[str, list[tuple]] = {name: [] for name in tables}
        self.counts: dict[str, int] = {name: 0 for name in tables}
        self._con: Optional[sqlite3.Connection] = None
        self._finished = False

    def __enter__(self) -> "SqliteBuilder":
        folder = os.path.dirname(os.path.abspath(self.dest))
        os.makedirs(folder, exist_ok=True)
        if os.path.exists(self.partial):
            os.remove(self.partial)
        self._con = sqlite3.connect(self.partial)
        # Bulk-load settings: a crash mid-build just means rebuilding,
        # and the file isn't under its real name until finish().
        for pragma in ("journal_mode = OFF", "synchronous = OFF", "locking_mode = EXCLUSIVE", "cache_size = -200000"):
            self._con.execute(f"pragma {pragma}")
        for name, columns in self._tables.items():
            self._con.execute(f"create table {name} ({', '.join(columns)})")
        return self

    def add(self, table: str, row: tuple) -> None:
        if len(row) != self._widths[table]:
            raise DumpImportError(f"{table}: expected {self._widths[table]} values, got {len(row)}: {row!r}")
        pending = self._pending[table]
        pending.append(row)
        if len(pending) >= self._batch:
            self._flush(table)

    def _flush(self, table: str) -> None:
        rows = self._pending[table]
        if rows:
            marks = ",".join("?" * self._widths[table])
            self._con.executemany(f"insert into {table} values ({marks})", rows)
            self.counts[table] += len(rows)
            rows.clear()

    def finish(self, info: Optional[dict] = None) -> dict[str, int]:
        """Flushes, indexes, analyzes, records `info` plus the build date
        and row counts in the info table, and moves the file into place.
        Returns the row counts."""
        for table in self._tables:
            self._flush(table)
        for statement in self._indexes:
            self._con.execute(statement)
        details = dict(info or {})
        details["built"] = datetime.datetime.now().isoformat(timespec="seconds")
        details.update({f"rows.{table}": str(count) for table, count in self.counts.items()})
        self._con.execute(f"create table {INFO_TABLE} (key text primary key, value text)")
        self._con.executemany(f"insert into {INFO_TABLE} values (?, ?)", [(k, str(v)) for k, v in details.items()])
        self._con.commit()
        self._con.execute("analyze")
        self._con.commit()
        self._con.close()
        self._con = None
        try:
            os.replace(self.partial, self.dest)
        except OSError as exc:
            raise DumpImportError(
                f"Couldn't replace {os.path.basename(self.dest)} -- is it open in another program? ({exc})"
            ) from exc
        self._finished = True
        return dict(self.counts)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._finished:
            return
        if self._con is not None:
            self._con.close()
            self._con = None
        if os.path.exists(self.partial):
            os.remove(self.partial)


def read_import_info(con: sqlite3.Connection) -> dict[str, str]:
    """The info table of a database built by SqliteBuilder; {} for any
    other database (e.g. GCD's own dump)."""
    try:
        return dict(con.execute(f"select key, value from {INFO_TABLE}").fetchall())
    except sqlite3.DatabaseError:
        return {}

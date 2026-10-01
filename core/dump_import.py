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
   with flat memory. iter_tsv_records() / iter_jsonl_records() do the
   same for line formats (Open Library's tab-separated + JSON files,
   JSON-lines dumps): bad lines are skipped and counted, but a changed
   format fails loudly. iter_pgcopy_records() reads PostgreSQL COPY text
   files (MusicBrainz's mbdump/<table>), and iter_tar_members() streams
   the tar archives that hold them, member by member, without unpacking;
   check_schema_sequence() guards against a dump with a changed schema
   (column lists: core/musicbrainz_schema.py).
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
import json
import lzma
import os
import re
import sqlite3
import tarfile
import zipfile
import zlib
from contextlib import closing, contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator, Optional, Sequence, Union
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
        # Optional no-argument callable run after every read: iter_tar_members
        # uses it to cancel / report progress even while tarfile is skipping
        # a multi-GB member nobody asked for.
        self.hook: Optional[Callable[[], None]] = None

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        data = self._raw.read(len(buffer))
        n = len(data)
        buffer[:n] = data
        self.count += n
        if self.hook:
            self.hook()
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


@dataclass
class ReadStats:
    """What a line reader saw: pass one in (`stats=`) to read the totals
    afterwards. `bad` lines were skipped (broken JSON, too few columns,
    over-long); `first_error` describes the first of them."""

    lines: int = 0      # non-blank lines read
    records: int = 0    # records yielded
    blank: int = 0
    bad: int = 0
    first_error: str = ""
    replaced: int = 0   # invalid UTF-8 bytes replaced with U+FFFD (iter_pgcopy_records)


# Called for every skipped line: (line number, reason, the line's first 200 chars).
BadLineFn = Callable[[int, str, str], None]

DEFAULT_MAX_LINE_BYTES = 64 << 20  # Open Library's biggest records are a few MB
SNIFF_COLUMN_LINES = 50            # a wrong column count on these -> format changed
SNIFF_JSON_LINES = 1000            # too many unparsable records in these -> format changed
SNIFF_BAD_FRACTION = 0.2
SNIFF_MIN_SAMPLE = 10


class _LineSource:
    """Shared engine of the line readers: bounded readline (a runaway line
    can't eat memory), CRLF/blank handling, cancel + progress, the
    skip-and-count policy and the "format has changed" tripwire."""

    def __init__(self, source, kind, *, cancelled, progress, on_bad_line, stats, max_line_bytes, check_every=500,
                 keep_blank=False):
        self.keep_blank = keep_blank  # yield empty lines (a COPY row can be one empty column)
        self.dump = source if isinstance(source, DumpStream) else None
        self.stream = source.stream if self.dump else source
        self.kind = kind
        self.cancelled = cancelled
        self.progress = progress
        self.on_bad_line = on_bad_line
        self.stats = stats if stats is not None else ReadStats()
        self.max_line = max_line_bytes
        self.check_every = check_every
        self.number = 0
        self.sniffed = 0        # non-blank lines inspected by the tripwire
        self.sniff_bad = 0      # of those, how many were bad
        self.shape_bad = 0      # ... of the first SNIFF_COLUMN_LINES, wrong column count

    def lines(self) -> Iterator[tuple[int, Optional[bytes]]]:
        """(line number, content without the line ending); content is None for
        an over-long line (already skipped past)."""
        readline = self.stream.readline
        limit = self.max_line
        while True:
            try:
                raw = readline(limit + 1)
            except (OSError, EOFError, lzma.LZMAError, zlib.error, tarfile.TarError) as exc:
                raise DumpImportError(f"The {self.kind} is damaged or incomplete: {exc}") from exc
            if not raw:
                break
            self.number += 1
            if self.number % self.check_every == 0:
                if self.cancelled and self.cancelled():
                    raise ImportCancelled()
                if self.progress and self.dump:
                    self.progress(self.dump.fraction())
            if isinstance(raw, str):
                raw = raw.encode("utf-8", "replace")
            if len(raw) > limit and not raw.endswith(b"\n"):
                while True:  # drain the rest of the line in chunks
                    rest = readline(1 << 20)
                    if not rest or (rest.endswith(b"\n") if isinstance(rest, bytes) else rest.endswith("\n")):
                        break
                yield self.number, None
                continue
            raw = raw.rstrip(b"\r\n")
            if self.keep_blank:
                # Only a truly empty line is "blank" to the caller; "\t" is a row.
                yield self.number, raw
                continue
            if not raw.strip():
                self.stats.blank += 1
                continue
            yield self.number, raw
        if self.progress:
            self.progress(1.0)
        self._check_sample(final=True)

    def bad(self, number: int, reason: str, raw: Optional[bytes], shape: bool = False) -> None:
        stats = self.stats
        stats.lines += 1
        stats.bad += 1
        self.sniffed += 1
        self.sniff_bad += 1
        if shape and self.sniffed <= SNIFF_COLUMN_LINES:
            self.shape_bad += 1
        if not stats.first_error:
            stats.first_error = f"line {number}: {reason}"
        if self.on_bad_line:
            preview = "" if raw is None else raw[:200].decode("utf-8", "replace")
            self.on_bad_line(number, reason, preview)
        self._check_sample()

    def good(self) -> None:
        self.stats.lines += 1
        self.stats.records += 1
        self.sniffed += 1
        if self.sniffed == SNIFF_COLUMN_LINES or self.sniffed == SNIFF_JSON_LINES:
            self._check_sample()

    def _check_sample(self, final: bool = False) -> None:
        """Fails loudly when the start of the file doesn't look like the
        format the recipe expects (a changed dump), instead of quietly
        producing an empty database."""
        if self.sniffed == 0 or self.sniffed > SNIFF_JSON_LINES:
            return
        size = min(self.sniffed, SNIFF_COLUMN_LINES)
        if self.shape_bad * 2 > size and (final or self.sniffed >= SNIFF_COLUMN_LINES or self.shape_bad >= SNIFF_MIN_SAMPLE):
            raise DumpImportError(
                f"This doesn't look like the expected {self.kind}: the first lines don't have the right "
                f"columns ({self.stats.first_error}). The dump format may have changed."
            )
        if final and self.sniff_bad == self.sniffed:  # nothing at all was readable
            raise DumpImportError(
                f"This doesn't look like the expected {self.kind}: none of its {self.sniffed} lines "
                f"could be read ({self.stats.first_error}). The dump format may have changed."
            )
        enough = self.sniffed >= SNIFF_JSON_LINES or final or self.sniff_bad == self.sniffed
        if enough and self.sniffed >= SNIFF_MIN_SAMPLE and self.sniff_bad > SNIFF_BAD_FRACTION * self.sniffed:
            raise DumpImportError(
                f"This doesn't look like the expected {self.kind}: {self.sniff_bad} of the first "
                f"{self.sniffed} lines couldn't be read ({self.stats.first_error}). "
                f"The dump format may have changed."
            )


def _loads(raw: bytes) -> Any:
    return json.loads(raw)


def iter_tsv_records(
    stream,
    columns: Optional[Sequence[str]],
    *,
    delimiter: str = "\t",
    json_columns: Sequence[Union[str, int]] = (),
    min_columns: Optional[int] = None,
    progress: Optional[ProgressFn] = None,
    cancelled: Optional[CancelledFn] = None,
    on_bad_line: Optional[BadLineFn] = None,
    stats: Optional[ReadStats] = None,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
    header: Union[bool, str] = False,
    null: Optional[str] = None,
) -> Iterator[Any]:
    """Yields one record per line of a delimiter-separated dump (Open
    Library's `type key revision last_modified {json}` files) as a dict
    {column: text}, or as a tuple of strings when `columns` is None.

    `header`: False (default; every line is data), True (the first line
    names the columns, as in IMDb's *.tsv.gz) or "auto" (the first line
    is a header when at least half of `columns` appear in it). With a
    header the fields are located BY NAME, so reordered columns work and
    NEW extra columns are tolerated (ignored); but when `columns` is
    given and any of them is missing from the header (a renamed or
    dropped column) DumpImportError is raised before a single record --
    a changed format fails loudly. With a header and columns=None the
    records are dicts keyed by the header's names. The header line is
    not counted in `stats`. A UTF-8 BOM before the header is ignored.
    `null`: text that marks a missing value (IMDb: backslash + "N"); a
    field equal to it becomes None (a field merely CONTAINING it is
    unchanged). Default None: no mapping, as before.

    `stream`: a DumpStream from open_dump() (progress counts its bytes) or
    any binary/text file-like with readline(). `json_columns`: names (or,
    with columns=None, 0-based positions) whose text is parsed with
    json.loads and replaced by the result ("" -> None). With `columns`
    given, a line is split at most len(columns)-1 times, so a stray
    delimiter lands in the last column; `min_columns` (default
    len(columns)) is how many a line needs.

    Tolerance: a line with too few columns, bad JSON, or longer than
    `max_line_bytes` is skipped and counted (`stats`, `on_bad_line`) --
    but a systematic mismatch (most of the first 50 lines have the wrong
    column count, or over 20% of the first 1000 are unreadable) raises
    DumpImportError, so a changed dump format fails loudly. Blank lines
    and CRLF endings are fine. Cancelling raises ImportCancelled; progress
    is reported every 500 lines. Memory stays flat: one line at a time."""
    names = list(columns) if columns is not None else None
    if names is not None:
        wanted = min_columns if min_columns is not None else len(names)
        split_max = len(names) - 1
    else:
        wanted = min_columns or 1
        split_max = -1
    json_positions: list[int] = []
    for column in json_columns:
        if isinstance(column, int):
            json_positions.append(column)
        elif names is not None and column in names:
            json_positions.append(names.index(column))
        else:
            raise ValueError(f"json column {column!r} is not one of the columns")
    if header not in (False, True, "auto"):
        raise ValueError("header must be False, True or 'auto'")
    null_bytes = null.encode("utf-8") if null is not None else None
    sep = delimiter.encode("utf-8")
    source = _LineSource(
        source=stream, kind="tab-separated dump", cancelled=cancelled, progress=progress,
        on_bad_line=on_bad_line, stats=stats, max_line_bytes=max_line_bytes,
    )
    pending_header = bool(header)
    positions: Optional[list[int]] = None   # header mode: where each output column sits in a line
    out_names = names
    for number, raw in source.lines():
        if raw is None:
            if pending_header:
                raise DumpImportError(f"This doesn't look like the expected {source.kind}: the header line is far too long.")
            source.bad(number, f"line longer than {max_line_bytes} bytes", None)
            continue
        if pending_header:
            pending_header = False
            first = raw[3:] if raw.startswith(b"\xef\xbb\xbf") else raw
            found = [p.decode("utf-8", "replace").strip() for p in first.split(sep)]
            is_header = True
            if header == "auto":
                is_header = names is not None and 2 * len(set(names) & set(found)) >= len(names)
                if not is_header:
                    raw = first  # a BOM'd first data line: keep it clean
            if is_header:
                if names is not None:
                    missing = [n for n in names if n not in found]
                    if missing:
                        raise DumpImportError(
                            f"This doesn't look like the expected {source.kind}: its header has columns "
                            f"{', '.join(found)[:300]} but {', '.join(missing)} "
                            f"{'is' if len(missing) == 1 else 'are'} missing. The dump format may have changed."
                        )
                    positions = [found.index(n) for n in names]
                else:
                    positions = list(range(len(found)))
                    out_names = found
                wanted = min_columns if min_columns is not None else len(found)
                split_max = len(found) - 1
                continue
        parts = raw.split(sep, split_max)
        if len(parts) < wanted:
            source.bad(number, f"expected {wanted} columns, found {len(parts)}", raw, shape=True)
            continue
        if positions is not None:
            parts = [parts[i] for i in positions]
        if null_bytes is not None:
            parts = [None if p == null_bytes else p for p in parts]
        try:
            for position in json_positions:
                if position < len(parts) and parts[position] is not None:
                    text = parts[position]
                    parts[position] = _loads(text) if text.strip() else None
        except ValueError as exc:  # JSONDecodeError and bad UTF-8 are both ValueErrors
            source.bad(number, f"bad JSON ({exc})", raw)
            continue
        values = [p.decode("utf-8", "replace") if isinstance(p, bytes) else p for p in parts]
        source.good()
        yield dict(zip(out_names, values)) if out_names is not None else tuple(values)


def iter_jsonl_records(
    stream,
    *,
    progress: Optional[ProgressFn] = None,
    cancelled: Optional[CancelledFn] = None,
    on_bad_line: Optional[BadLineFn] = None,
    stats: Optional[ReadStats] = None,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
) -> Iterator[dict]:
    """Yields the JSON object on each line of a JSON-lines dump (MusicBrainz's
    JSON dumps, ...). Same tolerance, tripwire, cancel/progress and memory
    behaviour as iter_tsv_records(); a line that isn't a JSON object counts
    as bad."""
    source = _LineSource(
        source=stream, kind="JSON-lines dump", cancelled=cancelled, progress=progress,
        on_bad_line=on_bad_line, stats=stats, max_line_bytes=max_line_bytes,
    )
    for number, raw in source.lines():
        if raw is None:
            source.bad(number, f"line longer than {max_line_bytes} bytes", None)
            continue
        try:
            record = _loads(raw)
        except ValueError as exc:
            source.bad(number, f"bad JSON ({exc})", raw)
            continue
        if not isinstance(record, dict):
            source.bad(number, "not a JSON object", raw)
            continue
        source.good()
        yield record


# ---- PostgreSQL COPY text format -----------------------------------------

# One backslash escape of COPY's text format, per the PostgreSQL docs
# (COPY, "Text Format"): \NNN octal (1-3 digits), \xHH hex (1-2 digits),
# \b \f \n \r \t \v, and any other character after a backslash stands for
# itself (so "\\" is a backslash and "\N" inside a field is just "N").
# A backslash that ends the field is kept as it is.
_PG_ESCAPE = re.compile(rb"\\(?:([0-7]{1,3})|x([0-9A-Fa-f]{1,2})|(.)|\Z)", re.DOTALL)
_PG_SIMPLE_ESCAPES = {
    b"b": bytes((8,)),
    b"f": bytes((12,)),
    b"n": bytes((10,)),
    b"r": bytes((13,)),
    b"t": bytes((9,)),
    b"v": bytes((11,)),
}
_BACKSLASH = bytes((92,))
_TAB = bytes((9,))
_PG_END_OF_DATA = bytes((92, 46))  # backslash + dot


def _pg_escape_match(match: "re.Match[bytes]") -> bytes:
    octal, hexa, char = match.groups()
    if octal:
        return bytes((int(octal, 8) & 0xFF,))
    if hexa:
        return bytes((int(hexa, 16),))
    if char is None:  # a lone backslash at the very end
        return _BACKSLASH
    return _PG_SIMPLE_ESCAPES.get(char, char)


def unescape_pgcopy(data: bytes) -> bytes:
    """Resolves the backslash escapes of one PostgreSQL COPY text field."""
    return _PG_ESCAPE.sub(_pg_escape_match, data)


def iter_pgcopy_records(
    stream,
    columns: Optional[Sequence[str]],
    *,
    null: str = r"\N",
    exact: bool = False,
    progress: Optional[ProgressFn] = None,
    cancelled: Optional[CancelledFn] = None,
    on_bad_line: Optional[BadLineFn] = None,
    stats: Optional[ReadStats] = None,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
) -> Iterator[Any]:
    """Yields one record per line of a PostgreSQL `COPY ... TO` text file
    (MusicBrainz's `mbdump/<table>` members): a dict {column: text-or-None},
    or a tuple when `columns` is None. Tab-separated, no header, `\\N`
    (`null`) is None, and the backslash escapes are resolved exactly as
    PostgreSQL does -- see unescape_pgcopy(). An empty field stays "" (only
    `null` means None). A line `\\.` ends the data (dumps don't have one,
    COPY accepts it). Values are always text: convert numbers/dates yourself.

    `columns` is the table's column order (core/musicbrainz_schema.TABLES).
    A line with fewer columns is a bad line; one with more has its extra
    trailing columns dropped, unless `exact=True`, which makes it bad too --
    pass the full verified list with exact=True so a table that gained a
    column fails loudly. Same tolerance as iter_tsv_records(): bad lines are
    skipped and counted (`stats`, `on_bad_line`), a systematic mismatch (most
    of the first 50 lines have the wrong column count) raises DumpImportError,
    `cancelled()` raises ImportCancelled, memory stays flat. Text is decoded
    as UTF-8 with errors replaced; `stats.replaced` counts the replacements.

    `stream`: a DumpStream, or any binary file-like with readline() -- e.g.
    a member from iter_tar_members()."""
    names = list(columns) if columns is not None else None
    ncols = len(names) if names is not None else 0
    null_bytes = null.encode("utf-8")
    null_in_fast_path = _BACKSLASH not in null_bytes
    source = _LineSource(
        source=stream, kind="PostgreSQL COPY dump", cancelled=cancelled, progress=progress,
        on_bad_line=on_bad_line, stats=stats, max_line_bytes=max_line_bytes, keep_blank=True,
    )
    stats = source.stats

    def decode(piece: bytes) -> str:
        try:
            return piece.decode("utf-8")
        except UnicodeDecodeError:
            text = piece.decode("utf-8", "replace")
            stats.replaced += text.count("\ufffd") - piece.count(b"\xef\xbf\xbd")
            return text

    for number, raw in source.lines():
        if raw is None:
            source.bad(number, f"line longer than {max_line_bytes} bytes", None)
            continue
        if raw == _PG_END_OF_DATA:
            break
        if not raw and ncols != 1:
            stats.blank += 1  # a row of one empty column is the only way a line is empty
            continue
        if _BACKSLASH not in raw:
            values = decode(raw).split("\t")
            if null_in_fast_path:
                values = [None if v == null else v for v in values]
        else:
            values = []
            for piece in raw.split(_TAB):
                if piece == null_bytes:
                    values.append(None)
                elif _BACKSLASH in piece:
                    values.append(decode(_PG_ESCAPE.sub(_pg_escape_match, piece)))
                else:
                    values.append(decode(piece))
        if names is not None:
            found = len(values)
            if found < ncols or (exact and found > ncols):
                source.bad(number, f"expected {'exactly ' if exact else ''}{ncols} columns, found {found}",
                           raw, shape=True)
                continue
        source.good()
        yield dict(zip(names, values)) if names is not None else tuple(values)
    if progress:
        progress(1.0)
    source._check_sample(final=True)


# ---- tar archives (MusicBrainz's mbdump.tar.bz2, the JSON dumps) -------------

def _clean_member_name(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]
    return name


def iter_tar_members(
    path_or_stream,
    wanted: Optional[Sequence[str]] = None,
    *,
    progress: Optional[ProgressFn] = None,
    cancelled: Optional[CancelledFn] = None,
) -> Iterator[tuple[str, Any]]:
    """Streams a .tar / .tar.gz / .tar.bz2 / .tar.xz member by member,
    without unpacking anything to disk or loading a member into memory:
    yields (member name, binary file-like) for each regular file. The
    compression is detected from the data, not the file name.

    The stream is read forward only, so consume each member (or leave it
    alone) BEFORE asking for the next one -- the file-like is dead after
    that. `wanted`: only yield these members; an entry matches the full
    name ("mbdump/artist") or just the last part ("artist"), the others are
    skipped without being read into Python (the decompressor still passes
    over their bytes), and the archive isn't read past the last wanted
    member. `path_or_stream`: a path, a DumpStream, or a binary file-like.

    `progress(fraction)` follows the compressed bytes read (path / DumpStream
    only) and `cancelled()` is checked as they come in, so a cancel doesn't
    wait for a multi-GB member to be skipped; ImportCancelled is raised.
    A damaged archive raises DumpImportError. Use `contextlib.closing()` if
    you may stop iterating early, so the file is closed promptly."""
    if isinstance(wanted, str):
        wanted = (wanted,)
    remaining = set(wanted) if wanted is not None else None
    counter: Optional[_CountingReader] = None
    total = 1
    owned = None
    if isinstance(path_or_stream, DumpStream):
        counter, total = path_or_stream._counter, path_or_stream._total
        fileobj = path_or_stream.stream
    elif isinstance(path_or_stream, (str, os.PathLike)):
        path = os.fspath(path_or_stream)
        if not os.path.isfile(path):
            raise DumpImportError(f"File not found: {path or '(not set)'}")
        try:
            owned = open(path, "rb")
        except OSError as exc:
            raise DumpImportError(f"Couldn't read {os.path.basename(path)}: {exc}") from exc
        counter = _CountingReader(owned)
        total = max(os.path.getsize(path), 1)
        fileobj = io.BufferedReader(counter, 1 << 20)
    else:
        fileobj = path_or_stream
    previous_hook = counter.hook if counter else None
    last_reported = [0.0]
    if counter is not None:
        def hook() -> None:
            if cancelled and cancelled():
                raise ImportCancelled()
            if progress:
                fraction = min(counter.count / total, 1.0)
                if fraction - last_reported[0] >= 0.005:
                    last_reported[0] = fraction
                    progress(fraction)
        counter.hook = hook
    try:
        try:
            archive = tarfile.open(fileobj=fileobj, mode="r|*")
        except (tarfile.TarError, OSError, EOFError, lzma.LZMAError, zlib.error) as exc:
            raise DumpImportError(f"Not a readable tar archive: {exc}") from exc
        try:
            while True:
                if cancelled and cancelled():
                    raise ImportCancelled()
                try:
                    info = archive.next()
                except (tarfile.TarError, OSError, EOFError, lzma.LZMAError, zlib.error) as exc:
                    raise DumpImportError(f"The archive is damaged or incomplete: {exc}") from exc
                if info is None:
                    break
                if not info.isfile():
                    continue
                name = _clean_member_name(info.name)
                if remaining is not None:
                    base = name.rsplit("/", 1)[-1]
                    hit = name if name in remaining else base if base in remaining else None
                    if hit is None:
                        continue
                    remaining.discard(hit)
                member = archive.extractfile(info)
                if member is None:
                    continue
                yield name, member
                if remaining is not None and not remaining:
                    break  # everything asked for has been seen
            if progress:
                progress(1.0)
        finally:
            archive.close()
    finally:
        if counter is not None:
            counter.hook = previous_hook
        if owned is not None:
            owned.close()


def read_small_member(path_or_stream, name: str, max_bytes: int = 1 << 20) -> Optional[bytes]:
    """The content of one small archive member (TIMESTAMP, SCHEMA_SEQUENCE,
    ...), or None if the archive has no such member. A member over
    `max_bytes` raises DumpImportError rather than being loaded."""
    with closing(iter_tar_members(path_or_stream, [name])) as members:
        for _, member in members:
            data = member.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise DumpImportError(f"{name} is larger than {max_bytes} bytes -- not the expected file.")
            return data
    return None


@dataclass
class ArchiveInfo:
    """The bookkeeping files at the start of a MusicBrainz dump archive.
    Any of them is None when the archive doesn't have it."""

    timestamp: Optional[str] = None            # TIMESTAMP, e.g. "2026-09-30 00:22:22.1+00"
    schema_sequence: Optional[int] = None      # SCHEMA_SEQUENCE
    replication_sequence: Optional[int] = None  # REPLICATION_SEQUENCE (may be empty -> None)


_INFO_FILES = ("TIMESTAMP", "SCHEMA_SEQUENCE", "REPLICATION_SEQUENCE")


def read_archive_info(path_or_stream, *, cancelled: Optional[CancelledFn] = None) -> ArchiveInfo:
    """Reads TIMESTAMP / SCHEMA_SEQUENCE / REPLICATION_SEQUENCE from the root
    of a dump archive. MusicBrainz puts them first, so this stops as soon as
    the tables (members inside a folder, like `mbdump/artist`) begin and only
    decompresses the first few KB. `path_or_stream` as for iter_tar_members;
    a stream can only be used once, so pass the path when you will read the
    tables afterwards."""
    info = ArchiveInfo()
    found = 0
    with closing(iter_tar_members(path_or_stream, cancelled=cancelled)) as members:
        for name, member in members:
            if "/" in name:
                break
            if name not in _INFO_FILES:
                continue
            text = member.read(4096).decode("utf-8", "replace").strip()
            found += 1
            if name == "TIMESTAMP":
                info.timestamp = text or None
            else:
                try:
                    number = int(text) if text else None
                except ValueError:
                    raise DumpImportError(f"{name} in the archive isn't a number: {text[:40]!r}") from None
                if name == "SCHEMA_SEQUENCE":
                    info.schema_sequence = number
                else:
                    info.replication_sequence = number
            if found == len(_INFO_FILES):
                break
    return info


def check_schema_sequence(archive_info: Union[ArchiveInfo, int, None], expected: Union[int, Iterable[int]]) -> int:
    """Fails loudly when a dump's schema differs from the one a recipe was
    written for (its table/column lists would then be wrong). `archive_info`:
    read_archive_info()'s result (or the number itself); `expected`: the
    schema sequence the recipe knows, or several it accepts. Returns the
    dump's number; raises DumpImportError otherwise."""
    accepted = sorted({expected} if isinstance(expected, int) else set(expected))
    found = archive_info.schema_sequence if isinstance(archive_info, ArchiveInfo) else archive_info
    wanted_text = " or ".join(str(n) for n in accepted)
    if found is None:
        raise DumpImportError(
            f"This dump doesn't say which database schema it uses (no SCHEMA_SEQUENCE file), "
            f"so it can't be checked against schema {wanted_text}. Use the official mbdump.tar.bz2."
        )
    if found not in accepted:
        raise DumpImportError(
            f"The dump was made with schema {found} but this recipe expects {wanted_text} -- "
            f"update the app (its column lists are for schema {wanted_text}), or use a dump of that schema."
        )
    return found


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
    `page_size` / `cache_mb`: bulk-load tuning (8 KiB pages suit big
    tables; the page cache is dropped when the build ends).

    For millions of rows, call create_fts_index() after the last add():
    the full-text index is then prebuilt inside the file instead of being
    rebuilt in memory every session (see core/local_db.fts_query).

        with SqliteBuilder(dest, tables, indexes) as out:
            out.add("series", (1, "Batman"))
            out.finish({"source": "ComicRack"})
    """

    def __init__(self, dest: str, tables: dict[str, list[str]], indexes: Iterable[str] = (), batch: int = 5000,
                 page_size: int = 8192, cache_mb: int = 200):
        self.dest = dest
        self._page_size = page_size
        self._cache_mb = cache_mb
        self._fts: dict[str, str] = {}
        self.sizes: dict[str, int] = {}  # filled by finish(): "(file)" and per table/index, in bytes
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
        # (page_size must come before the first table is created.)
        for pragma in (
            f"page_size = {self._page_size}", "journal_mode = OFF", "synchronous = OFF",
            "locking_mode = EXCLUSIVE", f"cache_size = -{self._cache_mb * 1024}",
        ):
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

    def add_many(self, table: str, rows: Iterable[tuple]) -> None:
        for row in rows:
            self.add(table, row)

    def create_fts_index(
        self,
        table: str,
        columns: Sequence[str],
        name: Optional[str] = None,
        tokenize: str = "unicode61 remove_diacritics 2",
        *,
        contentless: bool = False,
        prefix: Sequence[int] = (),
        optimize: bool = False,
        progress: Optional[ProgressFn] = None,
        cancelled: Optional[CancelledFn] = None,
        chunk: int = 50000,
    ) -> str:
        """Builds an FTS5 index over `columns` of `table` (a rowid table) and
        returns its name (default "<table>_fts"). Call after the last add()
        -- pending rows are flushed first -- so the index is filled in one
        pass instead of being maintained row by row.

        By default it is an EXTERNAL-CONTENT index: the text stays in `table`
        only (no second copy), the index maps words -> rowids, and the FTS
        table's columns can still be selected (they read through to `table`).
        `contentless=True` stores nothing but the index: smaller still, but
        only rowid and bm25 rank can be read back. `prefix`: e.g. (2, 3)
        also builds prefix indexes for fast "har*" queries.

        The rows go in by rowid range so `progress(fraction)` and `cancelled()`
        work on millions of rows. To index normalized text (e.g. punctuation
        folded like core/local_db.normalize_words), store the normalized
        text in its own column and index that."""
        if self._con is None:
            raise DumpImportError("create_fts_index() must be called inside the 'with' block")
        if table not in self._tables:
            raise DumpImportError(f"create_fts_index: unknown table {table!r}")
        cols = list(columns)
        if not cols:
            raise DumpImportError("create_fts_index: no columns")
        fts = name or f"{table}_fts"
        self._flush(table)
        options = ["content=''" if contentless else f"content='{table}'", "content_rowid='rowid'" if not contentless else None]
        options = [o for o in options if o]
        options.append("tokenize='" + tokenize.replace("'", "''") + "'")
        if prefix:
            options.append("prefix='" + " ".join(str(int(p)) for p in prefix) + "'")
        self._con.execute(f"create virtual table {fts} using fts5({', '.join(cols)}, {', '.join(options)})")
        low, high = self._con.execute(f"select min(rowid), max(rowid) from {table}").fetchone()
        if low is not None:
            column_list = ", ".join(cols)
            start = low
            while start <= high:
                if cancelled and cancelled():
                    raise ImportCancelled()
                end = start + chunk - 1
                self._con.execute(
                    f"insert into {fts}(rowid, {column_list}) select rowid, {column_list} "
                    f"from {table} where rowid between ? and ?", (start, end),
                )
                start = end + 1
                if progress:
                    progress(min((start - low) / (high - low + 1), 1.0))
        if optimize:
            self._con.execute(f"insert into {fts}({fts}) values('optimize')")
        self._con.commit()
        self._fts[fts] = f"{table}({', '.join(cols)})"
        if progress:
            progress(1.0)
        return fts

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
        Returns the row counts; `sizes` then holds the bytes per table/index
        and the file total ("(file)")."""
        for table in self._tables:
            self._flush(table)
        for statement in self._indexes:
            self._con.execute(statement)
        details = dict(info or {})
        details["built"] = datetime.datetime.now().isoformat(timespec="seconds")
        details.update({f"rows.{table}": str(count) for table, count in self.counts.items()})
        details.update({f"fts.{fts}": what for fts, what in self._fts.items()})
        self._con.execute(f"create table {INFO_TABLE} (key text primary key, value text)")
        self._con.executemany(f"insert into {INFO_TABLE} values (?, ?)", [(k, str(v)) for k, v in details.items()])
        self._con.commit()
        self._con.execute("analyze")
        self._con.commit()
        self._con.close()
        self._con = None
        self.sizes = self._measure()
        try:
            os.replace(self.partial, self.dest)
        except OSError as exc:
            raise DumpImportError(
                f"Couldn't replace {os.path.basename(self.dest)} -- is it open in another program? ({exc})"
            ) from exc
        self._finished = True
        return dict(self.counts)

    def _measure(self) -> dict[str, int]:
        """Bytes per table/index (via SQLite's dbstat, when compiled in) and
        for the whole file, under "(file)"."""
        sizes: dict[str, int] = {"(file)": os.path.getsize(self.partial)}
        try:
            con = sqlite3.connect(self.partial)
            try:
                for name, size in con.execute("select name, sum(pgsize) from dbstat group by name"):
                    sizes[name] = size
            finally:
                con.close()
        except sqlite3.DatabaseError:
            pass  # no dbstat: the file total is still there
        return sizes

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

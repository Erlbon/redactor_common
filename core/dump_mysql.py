"""
redactor_common/core/dump_mysql.py

Reads a `mysqldump` .sql file (ISFDB's backup, any dump made with the
default extended inserts) as a stream of rows, without loading it into a
database: `iter_mysql_dump(stream, {"pubs": ["pub_id", "pub_title"]})`
yields ("pubs", {"pub_id": "1", "pub_title": "..."}) for every row of the
tables asked for, and skips every other table's lines cheaply.

How it reads the format:

- Column names come from the dump's own `CREATE TABLE` blocks, so the
  recipe names the columns it WANTS (a subset, in any order) and a table
  that gains extra columns keeps working. A wanted column that is gone,
  a wanted table that is missing, or rows whose width doesn't match the
  table's CREATE statement fail loudly (DumpImportError) instead of
  producing a wrong database.
- `INSERT INTO `t` VALUES (...),(...);` is one physical line, however
  many rows it holds (mysqldump caps a line at ~1 MB). Strings are
  `'...'` with backslash escapes (\\' \\\\ \\n \\r \\t \\0 \\b \\Z) or a doubled
  quote; everything else is a bare token. NULL is None; every other
  value is TEXT (convert numbers and dates yourself), like the other
  readers here.
- `INSERT INTO `t` (a, b) VALUES ...` (a dump made with
  --complete-insert) works too: its own column list is used.
- Text is decoded as UTF-8 with errors replaced (`stats.replaced`
  counts them). Binary columns (`_binary '...'`, 0x... literals) are not
  decoded: keep them out of a recipe.

Same contract as the other readers: `progress(fraction)`, `cancelled()`
raising ImportCancelled, bad lines skipped and counted (`stats`,
`on_bad_line`) with a loud failure when the start of the file doesn't
look right, flat memory. `stream`: a DumpStream from open_dump() (a .zip
holding the .sql works) or any binary file-like with readline().
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Optional, Sequence

from redactor_common.core.dump_import import (
    DEFAULT_MAX_LINE_BYTES,
    BadLineFn,
    CancelledFn,
    DumpImportError,
    ProgressFn,
    ReadStats,
    _LineSource,
)

_CREATE = re.compile(rb"^CREATE TABLE `([^`]+)`")
_COLUMN = re.compile(rb"^\s+`([^`]+)`")
_INSERT = re.compile(rb"^INSERT INTO `([^`]+)` (?:\(([^)]*)\) )?VALUES ")
# Unrolled loops (no nested quantifier on the same characters), so a malformed
# line fails fast instead of backtracking for minutes.
_STRING = rb"'[^'\\]*(?:(?:\\.|'')[^'\\]*)*'"
_ROW = re.compile(rb"\(([^'()]*(?:" + _STRING + rb"[^'()]*)*)\)", re.DOTALL)
_FIELD = re.compile(rb"(" + _STRING + rb")|([^,']+)", re.DOTALL)
_ESCAPE = re.compile(rb"\\(.)|''", re.DOTALL)
_SIMPLE = {
    b"0": b"\x00", b"b": b"\x08", b"n": b"\n", b"r": b"\r", b"t": b"\t", b"Z": b"\x1a",
    b"%": b"\\%", b"_": b"\\_",  # MySQL keeps the backslash of these two
}


@dataclass
class MysqlReadStats(ReadStats):
    """ReadStats for iter_mysql_dump: `records` counts INSERT statements
    read for wanted tables; `rows` counts the rows yielded per table."""

    rows: dict = field(default_factory=dict)


def _unescape_match(match: "re.Match[bytes]") -> bytes:
    char = match.group(1)
    if char is None:  # a doubled quote
        return b"'"
    return _SIMPLE.get(char, char)


def unescape_mysql(data: bytes) -> bytes:
    """Resolves the escapes of one MySQL string literal's body (no outer quotes)."""
    return _ESCAPE.sub(_unescape_match, data)


def iter_mysql_dump(
    stream,
    tables: Mapping[str, Sequence[str]],
    *,
    progress: Optional[ProgressFn] = None,
    cancelled: Optional[CancelledFn] = None,
    on_bad_line: Optional[BadLineFn] = None,
    stats: Optional[MysqlReadStats] = None,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
) -> Iterator[tuple[str, dict[str, Optional[str]]]]:
    """Yields (table, {column: text-or-None}) for each row of the wanted
    `tables` ({table: [columns wanted]}), in dump order. Raises
    DumpImportError when a wanted table or column isn't in the dump."""
    wanted = {name: list(columns) for name, columns in tables.items()}
    stats = stats if stats is not None else MysqlReadStats()
    source = _LineSource(
        source=stream, kind="MySQL dump", cancelled=cancelled, progress=progress,
        on_bad_line=on_bad_line, stats=stats, max_line_bytes=max_line_bytes,
    )
    layout: dict[str, list[str]] = {}  # table -> its columns, from CREATE TABLE
    collecting: Optional[str] = None
    columns_seen: list[str] = []

    def decode(piece: bytes) -> str:
        try:
            return piece.decode("utf-8")
        except UnicodeDecodeError:
            text = piece.decode("utf-8", "replace")
            stats.replaced += text.count("�") - piece.count(b"\xef\xbf\xbd")
            return text

    def split_fields(row: bytes) -> list[Optional[str]]:
        out: list[Optional[str]] = []
        for match in _FIELD.finditer(row):
            quoted = match.group(1)
            if quoted is not None:
                body = quoted[1:-1]
                if b"\\" in body or b"''" in body:
                    body = _ESCAPE.sub(_unescape_match, body)
                out.append(decode(body))
            else:
                token = match.group(2)
                out.append(None if token == b"NULL" else token.decode("ascii", "replace"))
        return out

    for number, raw in source.lines():
        if raw is None:
            source.bad(number, f"line longer than {max_line_bytes} bytes", None)
            continue
        if collecting is not None:
            if raw.startswith(b")"):
                layout[collecting] = columns_seen
                for column in wanted[collecting]:
                    if column not in columns_seen:
                        raise DumpImportError(
                            f"The dump's table {collecting} has no column {column!r} (it has: "
                            f"{', '.join(columns_seen)}). The dump format may have changed."
                        )
                collecting = None
                continue
            match = _COLUMN.match(raw)
            if match:
                columns_seen.append(match.group(1).decode("utf-8", "replace"))
            continue
        if raw.startswith(b"CREATE TABLE"):
            match = _CREATE.match(raw)
            if match:
                name = match.group(1).decode("utf-8", "replace")
                if name in wanted:
                    collecting, columns_seen = name, []
            continue
        if not raw.startswith(b"INSERT INTO"):
            continue
        match = _INSERT.match(raw)
        if not match:
            continue
        name = match.group(1).decode("utf-8", "replace")
        if name not in wanted:
            continue
        if match.group(2):
            columns = [c.strip().strip(b"`").decode("utf-8", "replace") for c in match.group(2).split(b",")]
        elif name in layout:
            columns = layout[name]
        else:
            raise DumpImportError(f"The dump has rows for {name} before its CREATE TABLE statement.")
        try:
            positions = [(column, columns.index(column)) for column in wanted[name]]
        except ValueError as exc:
            raise DumpImportError(f"The dump's INSERT for {name} lacks a wanted column ({exc}).") from exc
        values = raw[match.end():]
        if values.endswith(b";"):
            values = values[:-1]
        width = len(columns)
        rows: list[dict[str, Optional[str]]] = []
        expected = 0  # where the next row must start: right after the comma that follows the last one
        problem = ""
        for row in _ROW.finditer(values):
            if row.start() != expected:
                problem = "rows are not separated by single commas"
                break
            expected = row.end() + 1
            fields = split_fields(row.group(1))
            if len(fields) != width:
                problem = f"a row of {name} has {len(fields)} values, the table has {width} columns"
                break
            rows.append({column: fields[index] for column, index in positions})
        else:
            if rows and expected - 1 != len(values):
                problem = "text left over after the last row"
        if problem or not rows:
            source.bad(number, problem or "no rows found", raw, shape=True)
            continue
        source.good()
        stats.rows[name] = stats.rows.get(name, 0) + len(rows)
        for record in rows:
            yield name, record
    if progress:
        progress(1.0)
    source._check_sample(final=True)
    missing = [name for name in wanted if name not in layout]
    if missing:
        raise DumpImportError(
            f"The dump has no table {', '.join(missing)}. Is this the right file? "
            f"(a MySQL dump with CREATE TABLE statements is expected)"
        )


def iter_mysql_table(stream, table: str, columns: Sequence[str], **kwargs: Any) -> Iterator[dict[str, Optional[str]]]:
    """iter_mysql_dump() for a single table: yields just the row dicts."""
    for _name, record in iter_mysql_dump(stream, {table: columns}, **kwargs):
        yield record

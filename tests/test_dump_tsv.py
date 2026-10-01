"""Tests for the TSV/JSONL dump readers, SqliteBuilder's FTS helper, isbn_norm and fts_query.

Fixtures are tiny synthetic files in Open Library's dump format (five
tab-separated columns, the last a JSON record), written gzip-compressed.
"""

import gzip
import io
import json
import os
import sqlite3
import tracemalloc

import pytest

from redactor_common.core.dump_import import (
    INFO_TABLE,
    DumpImportError,
    ImportCancelled,
    ReadStats,
    SqliteBuilder,
    iter_jsonl_records,
    iter_tsv_records,
    open_dump,
    read_import_info,
)
from redactor_common.core.isbn_norm import (
    clean_isbn,
    is_valid_isbn10,
    is_valid_isbn13,
    isbn10_to_13,
    isbn13_to_10,
    isbn_variants,
    normalize_isbn,
)
from redactor_common.core.local_db import LocalDatabase, LocalDatabaseError, fts_match_string, fts_query

OL_COLUMNS = ["type", "key", "revision", "last_modified", "json"]


def ol_line(i, title=None, **extra):
    record = {"title": title or f"Book {i}", "key": f"/books/OL{i}M", **extra}
    return "\t".join(["/type/edition", f"/books/OL{i}M", "3", "2024-01-02T03:04:05.678", json.dumps(record)])


def write_gz(path, lines, newline="\n"):
    with gzip.open(path, "wb") as f:
        f.write((newline.join(lines) + newline).encode("utf-8"))
    return str(path)


def read_tsv(path, **kw):
    with open_dump(str(path)) as dump:
        return list(iter_tsv_records(dump, OL_COLUMNS, json_columns=["json"], **kw))


# ---- TSV reader ---------------------------------------------------------

def test_tsv_open_library_shape(tmp_path):
    path = write_gz(tmp_path / "ol_dump_editions_2024-01-31.txt.gz", [
        ol_line(1, "Dune", isbn_13=["9780441172719"], authors=[{"key": "/authors/OL1A"}]),
        ol_line(2, "Émile"),
    ])
    stats = ReadStats()
    records = read_tsv(path, stats=stats)
    assert [r["key"] for r in records] == ["/books/OL1M", "/books/OL2M"]
    assert records[0]["type"] == "/type/edition" and records[0]["revision"] == "3"
    assert records[0]["json"]["isbn_13"] == ["9780441172719"]
    assert records[1]["json"]["title"] == "Émile"
    assert (stats.lines, stats.records, stats.bad) == (2, 2, 0)


def test_tsv_tuples_when_no_columns(tmp_path):
    path = write_gz(tmp_path / "x.txt.gz", ["a\tb\t{\"n\": 1}", "c\td\t{\"n\": 2}"])
    with open_dump(path) as dump:
        rows = list(iter_tsv_records(dump, None, json_columns=[2]))
    assert rows == [("a", "b", {"n": 1}), ("c", "d", {"n": 2})]


def test_tsv_crlf_blank_lines_and_empty_json(tmp_path):
    lines = [ol_line(1), "", "   ", ol_line(2), "/type/edition\t/books/OL3M\t1\tt\t"]
    path = write_gz(tmp_path / "x.txt.gz", lines, newline="\r\n")
    stats = ReadStats()
    records = read_tsv(path, stats=stats)
    assert [r["key"] for r in records] == ["/books/OL1M", "/books/OL2M", "/books/OL3M"]
    assert records[2]["json"] is None
    assert stats.blank == 2


def test_tsv_bad_json_and_short_lines_skipped_and_counted(tmp_path):
    lines = [ol_line(i) for i in range(1, 40)]
    lines.insert(5, "/type/edition\t/books/OLXM\t1\tt\t{not json")
    lines.insert(9, "only\ttwo")
    path = write_gz(tmp_path / "x.txt.gz", lines)
    seen = []
    stats = ReadStats()
    records = read_tsv(path, stats=stats, on_bad_line=lambda n, why, text: seen.append((n, why, text)))
    assert len(records) == 39
    assert stats.bad == 2 and stats.records == 39
    assert [n for n, _, _ in seen] == [6, 10]
    assert "bad JSON" in seen[0][1] and "columns" in seen[1][1]
    assert stats.first_error.startswith("line 6")


def test_tsv_extra_delimiter_lands_in_last_column():
    data = b"a\tb\tc d\te\n"
    rows = list(iter_tsv_records(io.BytesIO(data), ["x", "y", "z"]))
    assert rows == [{"x": "a", "y": "b", "z": "c d\te"}]


def test_tsv_min_columns_allows_short_lines():
    data = b"a\tb\n"
    rows = list(iter_tsv_records(io.BytesIO(data), ["x", "y", "z"], min_columns=2))
    assert rows == [{"x": "a", "y": "b"}]


def test_tsv_unknown_json_column_name_is_an_error():
    with pytest.raises(ValueError):
        list(iter_tsv_records(io.BytesIO(b""), ["a"], json_columns=["nope"]))


def test_tsv_wrong_column_count_in_first_lines_raises(tmp_path):
    # A dump that is e.g. comma-separated or has lost columns: fail loudly.
    path = write_gz(tmp_path / "x.txt.gz", [f"a,b,c,{i}" for i in range(80)])
    with pytest.raises(DumpImportError, match="columns"):
        read_tsv(path)


def test_tsv_all_json_broken_raises(tmp_path):
    lines = ["/type/edition\t/books/OL1M\t1\tt\tNOT-JSON %d" % i for i in range(30)]
    path = write_gz(tmp_path / "x.txt.gz", lines)
    with pytest.raises(DumpImportError, match="format may have changed"):
        read_tsv(path)


def test_tsv_small_file_entirely_unreadable_raises():
    with pytest.raises(DumpImportError):
        list(iter_tsv_records(io.BytesIO(b"x\ty\n"), ["a", "b", "c"]))


def test_tsv_many_bad_lines_within_first_1000_raises(tmp_path):
    lines = [ol_line(i) if i % 3 else "/type/edition\tk\t1\tt\t{bad" for i in range(300)]
    path = write_gz(tmp_path / "x.txt.gz", lines)
    with pytest.raises(DumpImportError, match="format may have changed"):
        read_tsv(path)


def test_tsv_sporadic_bad_lines_do_not_raise(tmp_path):
    lines = [ol_line(i) if i % 50 else "/type/edition\tk\t1\tt\t{bad" for i in range(1, 1200)]
    path = write_gz(tmp_path / "x.txt.gz", lines)
    stats = ReadStats()
    records = read_tsv(path, stats=stats)
    assert stats.bad == 23 and len(records) == 1199 - 23


def test_tsv_huge_line_is_read_and_over_long_line_skipped(tmp_path):
    big = "x" * 3_000_000
    lines = [ol_line(1), ol_line(2, description=big), ol_line(3, description="y" * 5000), ol_line(4)]
    path = write_gz(tmp_path / "x.txt.gz", lines)
    # Limit above the 3 MB record: everything is read.
    assert len(read_tsv(path)) == 4
    # Limit below it (but above the 5 KB one): the 3 MB line is skipped, reading continues.
    stats = ReadStats()
    records = read_tsv(path, stats=stats, max_line_bytes=100_000)
    assert [r["key"] for r in records] == ["/books/OL1M", "/books/OL3M", "/books/OL4M"]
    assert stats.bad == 1 and "longer than" in stats.first_error


def test_tsv_progress_and_cancel(tmp_path):
    path = write_gz(tmp_path / "x.txt.gz", [ol_line(i) for i in range(2000)])
    fractions = []
    with open_dump(path) as dump:
        n = sum(1 for _ in iter_tsv_records(dump, OL_COLUMNS, json_columns=["json"], progress=fractions.append))
    assert n == 2000
    assert fractions and fractions[-1] == 1.0 and all(0 <= f <= 1 for f in fractions)

    calls = []

    def cancelled():
        calls.append(1)
        return len(calls) >= 2

    with open_dump(path) as dump, pytest.raises(ImportCancelled):
        list(iter_tsv_records(dump, OL_COLUMNS, cancelled=cancelled))


def test_tsv_accepts_text_streams():
    rows = list(iter_tsv_records(io.StringIO("a\tb\nc\td\n"), ["x", "y"]))
    assert rows == [{"x": "a", "y": "b"}, {"x": "c", "y": "d"}]


def test_tsv_memory_stays_flat_on_200k_lines():
    line = (ol_line(1) + "\n").encode()
    total = 200_000

    class Source(io.RawIOBase):
        """Endless-looking source: a generator of lines, never held in memory."""

        def __init__(self):
            self.left = total

        def readline(self, limit=-1):
            if not self.left:
                return b""
            self.left -= 1
            return line

    tracemalloc.start()
    try:
        count = 0
        for record in iter_tsv_records(Source(), OL_COLUMNS, json_columns=["json"]):
            count += 1
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert count == total
    assert peak < 2_000_000  # a few lines' worth, not 200k records' worth


# ---- JSONL reader -------------------------------------------------------

def test_jsonl_basic_with_tolerance(tmp_path):
    lines = [json.dumps({"id": i, "name": f"n{i}"}) for i in range(60)]
    lines.insert(10, "{broken")
    lines.insert(20, "[1, 2]")
    lines.insert(30, "")
    path = write_gz(tmp_path / "x.jsonl.gz", lines, newline="\r\n")
    stats = ReadStats()
    with open_dump(path) as dump:
        records = list(iter_jsonl_records(dump, stats=stats))
    assert len(records) == 60 and records[0] == {"id": 0, "name": "n0"}
    assert stats.bad == 2 and stats.blank == 1


def test_jsonl_wrong_format_raises():
    data = b"".join(b"this is not json %d\n" % i for i in range(40))
    with pytest.raises(DumpImportError):
        list(iter_jsonl_records(io.BytesIO(data)))


def test_jsonl_cancel():
    data = b"".join(b'{"i": %d}\n' % i for i in range(2000))
    with pytest.raises(ImportCancelled):
        list(iter_jsonl_records(io.BytesIO(data), cancelled=lambda: True))


# ---- SqliteBuilder: FTS, sizes ------------------------------------------

TABLES = {"book": ["id integer primary key", "title text", "isbn text"]}


def build(tmp_path, rows, **fts_kw):
    dest = str(tmp_path / "out.sqlite")
    progress = []
    with SqliteBuilder(dest, TABLES, ["create index book_isbn on book(isbn)"]) as out:
        out.add_many("book", rows)
        name = out.create_fts_index("book", ["title"], progress=progress.append, **fts_kw)
        out.finish({"source": "test"})
    return dest, name, progress, out


def test_fts_index_prebuilt_and_queryable(tmp_path):
    rows = [(i, f"Title number {i}" if i % 10 else f"Café Harry {i}", f"isbn{i}") for i in range(1, 121)]
    dest, name, progress, out = build(tmp_path, rows, chunk=25)
    assert name == "book_fts"
    assert progress[-1] == 1.0 and progress == sorted(progress) and len(progress) > 3
    db = LocalDatabase(dest, ["book"])
    try:
        assert sorted(fts_query(db, "book_fts", "cafe harry", 50)) == list(range(10, 121, 10))  # diacritics folded
        assert fts_query(db, "book_fts", "harr")[:1]  # prefix on the last word
        assert fts_query(db, "book_fts", "harr", prefix=False) == []
        assert len(fts_query(db, "book_fts", "cafe harry", 3)) == 3
        assert fts_query(db, "book_fts", "harry 50", key="isbn", from_table="book") == ["isbn50"]
        assert fts_query(db, "book_fts", "harry 50", key="title") == ["Café Harry 50"]
        assert len(fts_query(db, "book_fts", "title", 2, columns=["title"])) == 2
        assert fts_query(db, "book_fts", "!!! ---") == []
        assert fts_query(db, "book_fts", 'tit"le ) OR "x') == []  # hostile text stays inert
    finally:
        db.close()
    info = sqlite3.connect(dest)
    try:
        assert read_import_info(info)["fts.book_fts"] == "book(title)"
    finally:
        info.close()


def test_fts_contentless_and_prefix_options(tmp_path):
    rows = [(1, "alpha beta", "a"), (2, "alpha gamma", "b")]
    dest, name, _, _ = build(tmp_path, rows, contentless=True, prefix=(2, 3))
    db = LocalDatabase(dest, ["book"])
    try:
        assert sorted(fts_query(db, name, "alpha")) == [1, 2]
        assert fts_query(db, name, "gam") == [2]
    finally:
        db.close()


def test_fts_ranks_better_match_first(tmp_path):
    rows = [(1, "dune dune dune", "a"), (2, "a long story that mentions dune once among many other words", "b")]
    dest, name, _, _ = build(tmp_path, rows)
    db = LocalDatabase(dest, ["book"])
    try:
        assert fts_query(db, name, "dune") == [1, 2]
    finally:
        db.close()


def test_fts_empty_table_and_errors(tmp_path):
    dest, name, progress, _ = build(tmp_path, [])
    assert progress[-1] == 1.0
    with pytest.raises(DumpImportError):
        with SqliteBuilder(str(tmp_path / "e.sqlite"), TABLES) as out:
            out.create_fts_index("nope", ["title"])
    with pytest.raises(ImportCancelled):
        with SqliteBuilder(str(tmp_path / "c.sqlite"), TABLES) as out:
            out.add("book", (1, "x", "y"))
            out.create_fts_index("book", ["title"], cancelled=lambda: True)
    assert not os.path.exists(str(tmp_path / "c.sqlite.partial"))
    with open_db(dest) as db:
        with pytest.raises(LocalDatabaseError):
            fts_query(db, "book_fts", "x", key="bad name")


class open_db:
    def __init__(self, path):
        self.db = LocalDatabase(path, ["book"])

    def __enter__(self):
        return self.db

    def __exit__(self, *a):
        self.db.close()


def test_builder_sizes_and_page_size(tmp_path):
    rows = [(i, f"t{i}", f"i{i}") for i in range(1, 2001)]
    dest, _, _, out = build(tmp_path, rows)
    assert out.sizes["(file)"] == os.path.getsize(dest)
    assert out.sizes["(file)"] > 0
    con = sqlite3.connect(dest)
    try:
        assert con.execute("pragma page_size").fetchone()[0] == 8192
        assert con.execute(f"select value from {INFO_TABLE} where key='rows.book'").fetchone()[0] == "2000"
    finally:
        con.close()


def test_builder_custom_page_size_and_old_signature(tmp_path):
    dest = str(tmp_path / "a.sqlite")
    with SqliteBuilder(dest, TABLES, [], 10, page_size=4096) as out:  # positional batch still works
        out.add("book", (1, "x", "y"))
        assert out.finish() == {"book": 1}
    con = sqlite3.connect(dest)
    try:
        assert con.execute("pragma page_size").fetchone()[0] == 4096
    finally:
        con.close()


# ---- local_db helpers ---------------------------------------------------

def test_has_table_and_table_columns(tmp_path):
    dest, _, _, _ = build(tmp_path, [(1, "x", "y")])
    with open_db(dest) as db:
        assert db.has_table("book") and db.has_table("BOOK") and db.has_table("book_fts")
        assert not db.has_table("nope")
        assert db.table_columns("book") == ["id", "title", "isbn"]
        assert db.table_columns("nope") == []
        assert db.table_columns('we"ird') == []


def test_fts_match_string():
    assert fts_match_string("Harry Potter") == '"harry" "potter"*'
    assert fts_match_string("Harry Potter", prefix=False) == '"harry" "potter"'
    assert fts_match_string("  !! ") == ""
    assert fts_match_string('a"b') == '"a" "b"*'


# ---- isbn_norm ----------------------------------------------------------

def test_isbn_clean_and_validate():
    assert clean_isbn("ISBN 0-306-40615-2") == "0306406152"
    assert clean_isbn("ISBN-13: 978-0-306-40615-7") == "9780306406157"
    assert clean_isbn(" 080442957x ") == "080442957X"
    assert clean_isbn(None) == ""
    assert is_valid_isbn10("0-306-40615-2") and is_valid_isbn10("080442957X") and is_valid_isbn10("080442957x")
    assert not is_valid_isbn10("0306406153") and not is_valid_isbn10("030640615") and not is_valid_isbn10("")
    assert is_valid_isbn13("978-0-306-40615-7") and not is_valid_isbn13("9780306406158")
    assert not is_valid_isbn13("97803064061x7") and not is_valid_isbn13("978030640615٧")


def test_isbn_conversion():
    assert isbn10_to_13("0-306-40615-2") == "9780306406157"
    assert isbn10_to_13("080442957X") == "9780804429573"
    assert isbn10_to_13("0306406153") is None
    assert isbn13_to_10("978-0-306-40615-7") == "0306406152"
    assert isbn13_to_10("9780804429573") == "080442957X"
    assert isbn13_to_10("9791090636071") is None  # 979: no ISBN-10 form
    assert isbn13_to_10("9780306406158") is None


def test_isbn_normalize_and_variants():
    assert normalize_isbn("0-306-40615-2") == "9780306406157"
    assert normalize_isbn("978 0 306 40615 7") == "9780306406157"
    assert normalize_isbn("1234567890") is None
    assert normalize_isbn("1234567890", strict=False) == "1234567890"
    assert normalize_isbn("9780306406158", strict=False) == "9780306406158"
    assert normalize_isbn("abc", strict=False) is None
    assert isbn_variants("0306406152") == ["9780306406157", "0306406152"]
    assert isbn_variants("9791090636071") == ["9791090636071"]
    assert isbn_variants("junk") == []

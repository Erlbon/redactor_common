"""iter_tsv_records(header=..., null=...): IMDb-style files (a header line,
backslash-N for missing values)."""

import gzip
import io

import pytest

from redactor_common.core.dump_import import (
    DumpImportError,
    ReadStats,
    iter_tsv_records,
    open_dump,
)

NULL = "\\N"  # literal backslash + N
COLS = ["tconst", "titleType", "primaryTitle", "startYear"]


def stream(text: str, newline: str = "\n", bom: bool = False) -> io.BytesIO:
    data = newline.join(text.split("\n")).encode("utf-8")
    return io.BytesIO((b"\xef\xbb\xbf" if bom else b"") + data)


BASIC = "tconst\ttitleType\tprimaryTitle\tstartYear\ntt0000001\tmovie\tCarmencita\t1894\ntt0000002\tshort\tLe clown\t\\N\n"


def test_header_true_gives_dicts_and_stats_exclude_header():
    stats = ReadStats()
    rows = list(iter_tsv_records(stream(BASIC), COLS, header=True, stats=stats))
    assert rows[0] == {"tconst": "tt0000001", "titleType": "movie", "primaryTitle": "Carmencita", "startYear": "1894"}
    assert len(rows) == 2 and stats.records == 2 and stats.lines == 2 and stats.bad == 0


def test_header_false_is_unchanged_and_header_line_is_data():
    rows = list(iter_tsv_records(stream(BASIC), COLS))
    assert len(rows) == 3 and rows[0]["tconst"] == "tconst"


def test_no_header_file_with_header_false_and_null():
    text = "tt1\tmovie\tA\t\\N\ntt2\tmovie\tB\t2000\n"
    rows = list(iter_tsv_records(stream(text), COLS, null=NULL))
    assert rows[0]["startYear"] is None and rows[1]["startYear"] == "2000"


def test_null_default_off_keeps_backslash_n_text():
    rows = list(iter_tsv_records(stream(BASIC), COLS, header=True))
    assert rows[1]["startYear"] == NULL


def test_null_maps_whole_field_only():
    text = "tconst\ttitleType\tprimaryTitle\tstartYear\ntt1\tmovie\tAC\\NDC\t\\N\ntt2\tmovie\t\\N\t1999\n"
    rows = list(iter_tsv_records(stream(text), COLS, header=True, null=NULL))
    assert rows[0]["primaryTitle"] == "AC\\NDC"  # inside a field: unchanged
    assert rows[0]["startYear"] is None
    assert rows[1]["primaryTitle"] is None and rows[1]["startYear"] == "1999"


def test_renamed_column_fails_loudly():
    text = "tconst\ttitle_type\tprimaryTitle\tstartYear\ntt1\tmovie\tA\t2000\n"
    with pytest.raises(DumpImportError, match="titleType"):
        list(iter_tsv_records(stream(text), COLS, header=True))


def test_extra_new_column_tolerated_and_reordered_columns_found_by_name():
    text = "startYear\tnewThing\ttconst\tprimaryTitle\ttitleType\n2000\tx\ttt1\tA\tmovie\n"
    rows = list(iter_tsv_records(stream(text), COLS, header=True))
    assert rows == [{"tconst": "tt1", "titleType": "movie", "primaryTitle": "A", "startYear": "2000"}]


def test_stray_tab_in_last_column_stays_in_it():
    text = "tconst\ttitleType\tprimaryTitle\tstartYear\ntt1\tmovie\tA\t20\t00\n"
    rows = list(iter_tsv_records(stream(text), COLS, header=True))
    assert rows[0]["startYear"] == "20\t00"


def test_crlf_and_bom():
    rows = list(iter_tsv_records(stream(BASIC, newline="\r\n", bom=True), COLS, header=True, null=NULL))
    assert [r["tconst"] for r in rows] == ["tt0000001", "tt0000002"]
    assert rows[1]["startYear"] is None


def test_header_without_columns_gives_dicts_by_header_names():
    rows = list(iter_tsv_records(stream(BASIC), None, header=True))
    assert rows[0]["primaryTitle"] == "Carmencita" and len(rows[0]) == 4


def test_short_line_is_bad_but_counted():
    text = "tconst\ttitleType\tprimaryTitle\tstartYear\ntt1\tmovie\nttOK\tmovie\tA\t2000\n"
    stats = ReadStats()
    rows = list(iter_tsv_records(stream(text), COLS, header=True, stats=stats))
    assert len(rows) == 1 and stats.bad == 1


def test_auto_detects_header_and_plain_data():
    with_header = list(iter_tsv_records(stream(BASIC), COLS, header="auto"))
    assert len(with_header) == 2
    plain = list(iter_tsv_records(stream("tt1\tmovie\tA\t2000\n"), COLS, header="auto"))
    assert len(plain) == 1 and plain[0]["tconst"] == "tt1"


def test_auto_with_mostly_matching_but_renamed_header_fails():
    text = "tconst\ttitleType\tprimaryTitle\tyearStart\ntt1\tmovie\tA\t2000\n"
    with pytest.raises(DumpImportError, match="startYear"):
        list(iter_tsv_records(stream(text), COLS, header="auto"))


def test_empty_file_with_header_true_yields_nothing():
    assert list(iter_tsv_records(stream(""), COLS, header=True)) == []


def test_bad_header_argument():
    with pytest.raises(ValueError):
        list(iter_tsv_records(stream(BASIC), COLS, header="yes"))


def test_through_open_dump_gz(tmp_path):
    path = tmp_path / "title.basics.tsv.gz"
    with gzip.open(path, "wb") as f:
        f.write(BASIC.encode("utf-8"))
    with open_dump(str(path)) as dump:
        rows = list(iter_tsv_records(dump, COLS, header=True, null=NULL))
    assert rows[1]["startYear"] is None


def test_json_column_with_header_and_null():
    text = "id\tdata\n1\t{\"a\": 1}\n2\t\\N\n"
    rows = list(iter_tsv_records(stream(text), ["id", "data"], header=True, null=NULL, json_columns=["data"]))
    assert rows[0]["data"] == {"a": 1} and rows[1]["data"] is None

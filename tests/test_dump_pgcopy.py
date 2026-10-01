"""Tests for the PostgreSQL COPY reader, the tar streamer, the schema guard and
core/musicbrainz_schema.

Fixtures are tiny synthetic archives written by the tests (the real MusicBrainz
dump is many GB and is never downloaded): tar / tar.gz / tar.bz2 / tar.xz with
the layout of mbdump.tar.bz2 -- TIMESTAMP, COPYING, README, REPLICATION_SEQUENCE
and SCHEMA_SEQUENCE at the root, then `mbdump/<table>` members.

Backslashes in the fixtures are spelled with BS / explicit bytes, so every
escape is visible in the test and cannot be eaten by an editor or a script.
"""

import io
import os
import tarfile
import tracemalloc

import pytest

from redactor_common.core import musicbrainz_schema as mb
from redactor_common.core.dump_import import (
    ArchiveInfo,
    DumpImportError,
    ImportCancelled,
    ReadStats,
    check_schema_sequence,
    iter_pgcopy_records,
    iter_tar_members,
    open_dump,
    read_archive_info,
    read_small_member,
    unescape_pgcopy,
)

BS = bytes((92,))   # backslash
TAB = bytes((9,))
NL = bytes((10,))


def rows(data, columns=None, **kw):
    return list(iter_pgcopy_records(io.BytesIO(data), columns, **kw))


def one_field(field):
    """The value of a single-column record holding `field` (bytes, escapes included)."""
    return rows(field + NL, ["v"])[0]["v"]


# ---- COPY text format -----------------------------------------------------

@pytest.mark.parametrize("escaped, expected", [
    (BS + b"t", "\t"),
    (BS + b"n", "\n"),
    (BS + b"r", "\r"),
    (BS + BS, "\\"),
    (BS + b"b", chr(8)),
    (BS + b"f", chr(12)),
    (BS + b"v", chr(11)),
    (BS + b"101", "A"),                 # octal
    (BS + b"12", "\n"),                 # 1-2 digit octal
    (BS + b"0", chr(0)),
    (BS + b"1011", "A1"),               # at most three octal digits
    (BS + b"303" + BS + b"251", "é"),  # two octal escapes make one UTF-8 character
    (BS + b"x41", "A"),                 # hex
    (BS + b"x4", chr(4)),               # one hex digit
    (BS + b"x4A", "J"),
    (BS + b"x411", "A1"),               # at most two hex digits
    (BS + b"xZ", "xZ"),                 # x without hex digits is just x
    (BS + b"q", "q"),                   # unknown escape -> the character itself
    (b"a" + BS + b"Nb", "aNb"),         # \N inside a field is not NULL
    (b"ab" + BS, "ab" + BS.decode()),   # trailing backslash is kept
    (BS + b"7" + BS + b"8", chr(7) + "8"),  # 8 is not an octal digit
])
def test_every_escape(escaped, expected):
    assert one_field(escaped) == expected


def test_unescape_helper():
    assert unescape_pgcopy(b"a" + BS + b"tb" + BS + BS + b"c") == b"a\tb\\c"
    assert unescape_pgcopy(b"plain") == b"plain"


def test_null_versus_empty_string():
    data = b"1" + TAB + BS + b"N" + TAB + b"" + NL + b"2" + TAB + b"" + TAB + BS + b"N" + NL
    assert rows(data, ["id", "a", "b"]) == [
        {"id": "1", "a": None, "b": ""},
        {"id": "2", "a": "", "b": None},
    ]


def test_escaped_backslash_then_n_is_text_not_null():
    # the field is the three bytes \\N: an escaped backslash and an N
    assert one_field(BS + BS + b"N") == "\\N"


def test_tab_inside_data_is_escaped_and_does_not_split():
    data = b"a" + BS + b"tb" + TAB + b"c" + NL
    assert rows(data, ["x", "y"]) == [{"x": "a\tb", "y": "c"}]
    assert rows(data, None) == [("a\tb", "c")]


def test_newline_and_cr_inside_data_do_not_split_lines():
    data = b"line1" + BS + b"nline2" + BS + b"r" + TAB + b"z" + NL + b"next" + TAB + b"q" + NL
    assert rows(data, ["a", "b"]) == [{"a": "line1\nline2\r", "b": "z"}, {"a": "next", "b": "q"}]


def test_fast_path_and_slow_path_agree_on_plain_lines():
    plain = b"1" + TAB + b"Beatles" + TAB + b"caf\xc3\xa9" + NL
    assert rows(plain, ["a", "b", "c"]) == [{"a": "1", "b": "Beatles", "c": "café"}]
    mixed = plain + b"2" + TAB + BS + b"N" + TAB + b"x" + BS + b"ty" + NL
    got = rows(mixed, ["a", "b", "c"])
    assert got[0]["c"] == "café" and got[1] == {"a": "2", "b": None, "c": "x\ty"}


def test_crlf_endings_tolerated():
    assert rows(b"a" + TAB + b"b" + b"\r\n" + b"c" + TAB + b"d" + b"\r\n", ["x", "y"]) == [
        {"x": "a", "y": "b"}, {"x": "c", "y": "d"}]


def test_trailing_extra_columns_dropped_by_default():
    data = b"1" + TAB + b"a" + TAB + b"extra" + TAB + b"more" + NL
    assert rows(data, ["id", "name"]) == [{"id": "1", "name": "a"}]
    assert rows(data, None) == [("1", "a", "extra", "more")]


def test_exact_makes_extra_columns_bad():
    data = (b"1" + TAB + b"a" + TAB + b"extra" + NL) * 3
    stats = ReadStats()
    with pytest.raises(DumpImportError, match="columns"):
        rows(data, ["id", "name"], exact=True, stats=stats)


def test_too_few_columns_is_bad_but_tolerated_when_rare():
    good = b"1" + TAB + b"a" + NL
    data = good * 30 + b"only-one" + NL + good * 30
    stats, bad = ReadStats(), []
    got = rows(data, ["id", "name"], stats=stats, on_bad_line=lambda n, why, text: bad.append((n, why, text)))
    assert len(got) == 60
    assert stats.bad == 1 and stats.records == 60 and "line 31" in stats.first_error
    assert bad == [(31, "expected 2 columns, found 1", "only-one")]


def test_systematic_column_mismatch_fails_loudly():
    data = (b"a" + TAB + b"b" + TAB + b"c" + NL) * 200
    with pytest.raises(DumpImportError, match="format may have changed"):
        rows(data, ["id", "name", "gid", "comment"])


def test_non_utf8_bytes_replaced_and_counted():
    data = b"ok" + TAB + b"bad\xff\xfebytes" + NL + b"x" + TAB + b"good" + NL
    stats = ReadStats()
    got = rows(data, ["a", "b"], stats=stats)
    assert got[0]["b"] == "bad��bytes" and got[1]["b"] == "good"
    assert stats.replaced == 2
    # also inside a line that has escapes (slow path)
    stats = ReadStats()
    got = rows(b"x" + BS + b"ty\xff" + NL, ["a"], stats=stats)
    assert got == [{"a": "x\ty�"}] and stats.replaced == 1


def test_genuine_replacement_character_is_not_counted():
    stats = ReadStats()
    got = rows("a�b".encode("utf-8") + NL, ["a"], stats=stats)
    assert got == [{"a": "a�b"}] and stats.replaced == 0


def test_empty_lines_single_column_vs_multi_column():
    assert rows(b"" + NL + b"x" + NL, ["v"]) == [{"v": ""}, {"v": "x"}]
    stats = ReadStats()
    assert rows(b"a" + TAB + b"b" + NL + NL + b"c" + TAB + b"d" + NL, ["x", "y"], stats=stats) == [
        {"x": "a", "y": "b"}, {"x": "c", "y": "d"}]
    assert stats.blank == 1 and stats.bad == 0


def test_row_of_empty_columns_is_not_blank():
    assert rows(TAB + NL, ["a", "b"]) == [{"a": "", "b": ""}]
    assert rows(TAB + TAB + NL, None) == [("", "", "")]


def test_end_of_data_marker_stops():
    data = b"1" + NL + BS + b"." + NL + b"2" + NL
    assert rows(data, ["v"]) == [{"v": "1"}]
    # but an escaped backslash followed by a dot is data
    assert rows(BS + BS + b"." + NL, ["v"]) == [{"v": "\\."}]


def test_custom_null_marker():
    data = b"a" + TAB + b"NULL" + TAB + b"" + NL
    assert rows(data, ["x", "y", "z"], null="NULL") == [{"x": "a", "y": None, "z": ""}]


def test_missing_final_newline():
    assert rows(b"a" + TAB + b"b", ["x", "y"]) == [{"x": "a", "y": "b"}]


def test_very_long_line_ok_and_over_limit_skipped():
    long_value = b"x" * 3_000_000
    data = b"1" + TAB + long_value + NL
    assert rows(data, ["id", "v"])[0]["v"] == long_value.decode()
    stats = ReadStats()
    data = b"1" + TAB + b"a" + NL + b"2" + TAB + b"y" * 5000 + NL + b"3" + TAB + b"b" + NL
    got = rows(data, ["id", "v"], max_line_bytes=1000, stats=stats)
    assert [r["id"] for r in got] == ["1", "3"] and stats.bad == 1


def test_cancel_mid_stream():
    data = (b"1" + TAB + b"a" + NL) * 5000
    seen = []
    with pytest.raises(ImportCancelled):
        for rec in iter_pgcopy_records(io.BytesIO(data), ["a", "b"], cancelled=lambda: len(seen) >= 600):
            seen.append(rec)
    assert 600 <= len(seen) < 5000


def test_progress_with_dump_stream(tmp_path):
    path = tmp_path / "t.txt"
    path.write_bytes((b"1" + TAB + b"a" + NL) * 3000)
    seen = []
    with open_dump(str(path)) as dump:
        n = sum(1 for _ in iter_pgcopy_records(dump, ["a", "b"], progress=seen.append))
    assert n == 3000 and seen[-1] == 1.0 and seen == sorted(seen)


def test_flat_memory_200k_lines(tmp_path):
    path = tmp_path / "big.copy"
    line = b"12345" + TAB + b"Some Artist Name" + TAB + BS + b"N" + TAB + b"a" + BS + b"tb" + TAB + b"2020" + NL
    with open(path, "wb") as f:
        for _ in range(200):
            f.write(line * 1000)
    tracemalloc.start()
    try:
        n = 0
        with open(path, "rb") as f:
            for rec in iter_pgcopy_records(f, ["a", "b", "c", "d", "e"]):
                n += 1
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert n == 200_000
    assert peak < 3_000_000


# ---- tar streaming --------------------------------------------------------

def build_archive(path, mode, schema=b"31\n", extra=()):
    members = [
        ("TIMESTAMP", b"2026-09-30 00:22:22.1+00\n"),
        ("COPYING", b"public domain\n"),
        ("README", b"readme\n"),
        ("REPLICATION_SEQUENCE", b"12345\n"),
        ("SCHEMA_SEQUENCE", schema),
        ("mbdump/artist", b"1" + TAB + b"Beatles" + NL + b"2" + TAB + b"Stones" + NL),
        ("mbdump/release", b"10" + TAB + b"Abbey Road" + NL),
        ("mbdump/track", (b"1" + TAB + b"Come Together" + NL) * 50),
        *extra,
    ]
    with tarfile.open(path, mode) as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return str(path)


MODES = [("w", ".tar"), ("w:gz", ".tar.gz"), ("w:bz2", ".tar.bz2"), ("w:xz", ".tar.xz")]


@pytest.mark.parametrize("mode, suffix", MODES)
def test_tar_all_members(tmp_path, mode, suffix):
    path = build_archive(tmp_path / f"mbdump{suffix}", mode)
    seen = {}
    for name, member in iter_tar_members(path):
        seen[name] = member.read()
    assert list(seen) == ["TIMESTAMP", "COPYING", "README", "REPLICATION_SEQUENCE", "SCHEMA_SEQUENCE",
                          "mbdump/artist", "mbdump/release", "mbdump/track"]
    assert seen["SCHEMA_SEQUENCE"] == b"31\n"
    assert seen["mbdump/release"] == b"10" + TAB + b"Abbey Road" + NL


@pytest.mark.parametrize("mode, suffix", MODES)
def test_tar_wanted_skips_the_rest(tmp_path, mode, suffix):
    path = build_archive(tmp_path / f"mbdump{suffix}", mode)
    got = [(name, member.read()) for name, member in iter_tar_members(path, ["mbdump/track", "release"])]
    assert [name for name, _ in got] == ["mbdump/release", "mbdump/track"]
    assert got[1][1].count(NL) == 50
    # wanted given as one plain string; unknown names simply never appear
    assert [n for n, _ in iter_tar_members(path, "mbdump/artist")] == ["mbdump/artist"]
    assert list(iter_tar_members(path, ["nothing/here"])) == []


@pytest.mark.parametrize("mode, suffix", MODES)
def test_tar_unconsumed_members_are_skipped_without_error(tmp_path, mode, suffix):
    path = build_archive(tmp_path / f"mbdump{suffix}", mode)
    names = [name for name, _ in iter_tar_members(path)]          # never read any
    assert len(names) == 8
    partly = []
    for name, member in iter_tar_members(path):
        if name == "mbdump/track":
            partly.append(member.readline())                       # only the first line
        elif name == "mbdump/artist":
            partly.append(member.read(2))
    assert partly == [b"1\t", b"1\tCome Together\n"]


def test_tar_feeds_pgcopy_without_extracting(tmp_path):
    path = build_archive(tmp_path / "mbdump.tar.bz2", "w:bz2")
    got = {}
    for name, member in iter_tar_members(path, [mb.table_member("artist")]):
        got[name] = list(iter_pgcopy_records(member, ["id", "name"]))
    assert got == {"mbdump/artist": [{"id": "1", "name": "Beatles"}, {"id": "2", "name": "Stones"}]}
    assert os.listdir(tmp_path) == ["mbdump.tar.bz2"]


def test_tar_from_file_like_and_dump_stream(tmp_path):
    path = build_archive(tmp_path / "mbdump.tar.xz", "w:xz")
    with open(path, "rb") as f:
        assert [n for n, _ in iter_tar_members(f, ["mbdump/artist"])] == ["mbdump/artist"]
    with open_dump(path) as dump:  # already decompressed by open_dump: plain tar inside
        seen = [n for n, m in iter_tar_members(dump, ["mbdump/release"])]
    assert seen == ["mbdump/release"]


def test_tar_member_names_with_dot_slash_prefix(tmp_path):
    path = tmp_path / "x.tar"
    with tarfile.open(path, "w") as tar:
        info = tarfile.TarInfo("./mbdump/artist")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"x\n"))
        folder = tarfile.TarInfo("./mbdump")
        folder.type = tarfile.DIRTYPE
        tar.addfile(folder)
    assert [n for n, _ in iter_tar_members(str(path))] == ["mbdump/artist"]
    assert [n for n, _ in iter_tar_members(str(path), ["artist"])] == ["mbdump/artist"]


def test_tar_progress_and_cancel(tmp_path):
    path = build_archive(tmp_path / "mbdump.tar.bz2", "w:bz2", extra=[("mbdump/zzz", os.urandom(200_000))])
    seen = []
    for _ in iter_tar_members(path, progress=seen.append):
        pass
    assert seen[-1] == 1.0 and seen == sorted(seen)
    with pytest.raises(ImportCancelled):
        for _ in iter_tar_members(path, cancelled=lambda: True):
            pass


def test_cancel_in_the_middle_of_a_member(tmp_path):
    big = (b"1" + TAB + b"row" + NL) * 20000
    path = build_archive(tmp_path / "mbdump.tar.gz", "w:gz", extra=[("mbdump/big", big)])
    count = 0
    with pytest.raises(ImportCancelled):
        for name, member in iter_tar_members(path, ["big"]):
            for _ in iter_pgcopy_records(member, ["a", "b"], cancelled=lambda: count >= 1000):
                count += 1
    assert 1000 <= count < 20000


def test_damaged_and_non_tar_files_raise_dump_import_error(tmp_path):
    good = build_archive(tmp_path / "mbdump.tar.bz2", "w:bz2", extra=[("mbdump/zzz", os.urandom(100_000))])
    cut = tmp_path / "cut.tar.bz2"
    data = open(good, "rb").read()
    cut.write_bytes(data[: len(data) // 2])
    with pytest.raises(DumpImportError):
        for _, member in iter_tar_members(str(cut)):
            member.read()
    junk = tmp_path / "junk.tar.bz2"
    junk.write_bytes(b"this is not an archive at all" * 100)
    with pytest.raises(DumpImportError):
        list(iter_tar_members(str(junk)))
    with pytest.raises(DumpImportError, match="not found"):
        list(iter_tar_members(str(tmp_path / "missing.tar")))


def test_read_small_member(tmp_path):
    path = build_archive(tmp_path / "mbdump.tar.bz2", "w:bz2")
    assert read_small_member(path, "SCHEMA_SEQUENCE") == b"31\n"
    assert read_small_member(path, "NOPE") is None
    with pytest.raises(DumpImportError, match="larger"):
        read_small_member(path, "mbdump/track", max_bytes=10)


# ---- archive info + schema guard -------------------------------------------

class CountingFile(io.RawIOBase):
    def __init__(self, path):
        self._f = open(path, "rb")
        self.read_bytes = 0

    def readable(self):
        return True

    def readinto(self, b):
        data = self._f.read(len(b))
        b[: len(data)] = data
        self.read_bytes += len(data)
        return len(data)

    def close(self):
        self._f.close()
        super().close()


def test_read_archive_info(tmp_path):
    path = build_archive(tmp_path / "mbdump.tar.bz2", "w:bz2")
    info = read_archive_info(path)
    assert info == ArchiveInfo(timestamp="2026-09-30 00:22:22.1+00", schema_sequence=31, replication_sequence=12345)


def test_read_archive_info_stops_at_the_tables(tmp_path):
    # the tables are incompressible random data: reading them would read the whole file
    path = build_archive(tmp_path / "mbdump.tar.bz2", "w:bz2", extra=[("mbdump/huge", os.urandom(3_000_000))])
    raw = CountingFile(path)
    try:
        info = read_archive_info(io.BufferedReader(raw, 1 << 16))
    finally:
        raw.close()
    assert info.schema_sequence == 31
    assert raw.read_bytes < os.path.getsize(path) // 2   # one bz2 block or so, not the 3 MB table


def test_read_archive_info_missing_files(tmp_path):
    path = tmp_path / "bare.tar"
    with tarfile.open(path, "w") as tar:
        info = tarfile.TarInfo("mbdump/artist")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"x\n"))
    info = read_archive_info(str(path))
    assert info == ArchiveInfo()
    with pytest.raises(DumpImportError, match="SCHEMA_SEQUENCE"):
        check_schema_sequence(info, 31)


def test_empty_replication_sequence_and_bad_number(tmp_path):
    path = build_archive(tmp_path / "a.tar", "w", extra=[])
    empty = tmp_path / "b.tar"
    with tarfile.open(empty, "w") as tar:
        for name, data in (("REPLICATION_SEQUENCE", b""), ("SCHEMA_SEQUENCE", b"abc\n")):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    with pytest.raises(DumpImportError, match="isn't a number"):
        read_archive_info(str(empty))
    assert read_archive_info(path).replication_sequence == 12345


def test_check_schema_sequence_pass_and_fail(tmp_path):
    ok = read_archive_info(build_archive(tmp_path / "ok.tar.bz2", "w:bz2", schema=b"31\n"))
    assert check_schema_sequence(ok, 31) == 31
    assert check_schema_sequence(ok, [30, 31]) == 31
    assert check_schema_sequence(31, 31) == 31
    new = read_archive_info(build_archive(tmp_path / "new.tar.bz2", "w:bz2", schema=b"32\n"))
    with pytest.raises(DumpImportError) as exc:
        check_schema_sequence(new, 31)
    text = str(exc.value)
    assert "schema 32" in text and "expects 31" in text and "update the app" in text
    with pytest.raises(DumpImportError, match="expects 30 or 31"):
        check_schema_sequence(new, [31, 30])


# ---- the schema module -------------------------------------------------------

def test_schema_constants_are_sane():
    assert isinstance(mb.SCHEMA_SEQUENCE_EXPECTED, int)
    wanted = ["artist", "artist_credit", "artist_credit_name", "release_group", "release", "medium", "track",
              "recording", "label", "release_label", "release_country", "area", "language", "script",
              "medium_format", "release_group_primary_type", "release_status"]
    for table in wanted:
        columns = mb.table_columns(table)
        assert columns and len(set(columns)) == len(columns), table
    assert mb.TABLES["artist"][:2] == ("id", "gid")
    assert mb.TABLES["artist_credit_name"] == ("artist_credit", "position", "artist", "name", "join_phrase")
    assert len(mb.TABLES["track"]) == 12 and len(mb.TABLES["release"]) == 14
    assert not set(mb.TABLES) & mb.DERIVED_TABLES      # only the CC0 core archive
    assert mb.table_member("artist") == "mbdump/artist"
    with pytest.raises(KeyError):
        mb.table_columns("work_tag")


def test_artist_roundtrip_through_archive_with_verified_columns(tmp_path):
    columns = mb.table_columns("artist")
    values = ["7", "11111111-2222-3333-4444-555555555555", "Sigur Rós", "Sigur Rós", "1994"]
    values += [BS.decode() + "N"] * (len(columns) - len(values))
    line = TAB.decode().join(values).encode("utf-8") + NL
    path = build_archive(tmp_path / "mbdump.tar.bz2", "w:bz2", extra=[("mbdump/artist2", line)])
    found = []
    for _, member in iter_tar_members(path, ["artist2"]):
        found = list(iter_pgcopy_records(member, columns, exact=True))
    assert found[0]["name"] == "Sigur Rós" and found[0]["begin_date_year"] == "1994"
    assert found[0]["end_area"] is None and len(found[0]) == len(columns)

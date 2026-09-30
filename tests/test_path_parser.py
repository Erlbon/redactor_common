"""Tests for core/path_parser.py and the path mode of ParseFilenameDialog."""

import pytest

from redactor_common.core import filename_parser
from redactor_common.core.filename_parser import SERIES_INDEX_FIELD_PATTERN
from redactor_common.core.path_parser import (
    HIGH_CONFIDENCE, folder_value_counts, is_path_pattern, parse_path,
    parse_path_detailed, relative_segments, split_path_pattern, split_pattern_history,
)

VALID = {"genre", "author", "series", "series_index", "title", "albumartist", "album",
         "track", "season", "number"}
SHAPES = {"series_index": SERIES_INDEX_FIELD_PATTERN}
ROOT = "C:/Lib"


def test_split_path_pattern_and_is_path_pattern():
    assert split_path_pattern("%a%/%b%/%c%") == ["%a%", "%b%", "%c%"]
    assert split_path_pattern("\\%a%\\\\%b%/") == ["%a%", "%b%"]
    assert split_path_pattern("%title%") == ["%title%"]
    assert is_path_pattern("%a%/%b%") and is_path_pattern("%a%\\%b%")
    assert not is_path_pattern("%a% - %b%")
    assert split_pattern_history(["%a%", "%a%/%b%", "%c%"]) == (["%a%", "%c%"], ["%a%/%b%"])


def test_relative_segments_under_root_and_outside():
    assert relative_segments("C:/Lib/Fantasy/Tolkien/Hobbit.epub", ROOT) == ["Fantasy", "Tolkien", "Hobbit"]
    assert relative_segments("C:\\Lib\\Hobbit.epub", ROOT) == ["Hobbit"]
    # Outside the root: as many parents as the pattern has segments, never the drive.
    assert relative_segments("D:/Other/A/B/C/Hobbit.epub", ROOT, 3) == ["B", "C", "Hobbit"]
    assert relative_segments("D:/x.epub", ROOT, 3) == ["x"]
    assert relative_segments("//srv/share/a/x.epub", "", 5) == ["a", "x"]
    assert len(relative_segments("/".join("abcdefghij") + "/f.epub", "")) == 6
    assert relative_segments("", ROOT) == []


def test_right_to_left_exact_match():
    r = parse_path_detailed(
        "C:/Lib/Fantasy/Tolkien/Hobbit.epub", "%genre%/%author%/%title%", ROOT, VALID)
    assert r.matched and r.values == {"genre": "Fantasy", "author": "Tolkien", "title": "Hobbit"}
    assert r.missing_segments == [] and len(r.matched_segments) == 3
    assert parse_path("C:/Lib/Fantasy/Tolkien/Hobbit.epub", "%genre%/%author%/%title%", ROOT, VALID) == r.values


def test_fewer_pattern_segments_ignore_outer_folders():
    v = parse_path("C:/Lib/Fantasy/Tolkien/Hobbit.epub", "%author%/%title%", ROOT, VALID)
    assert v == {"author": "Tolkien", "title": "Hobbit"}


def test_more_pattern_segments_leave_outer_empty_and_report():
    r = parse_path_detailed("C:/Lib/Tolkien/Hobbit.epub", "%genre%/%author%/%title%", ROOT, VALID)
    assert r.values == {"genre": "", "author": "Tolkien", "title": "Hobbit"}
    assert r.missing_segments == ["%genre%"]
    assert any("outer" in n for n in r.notes)
    assert 0 < r.confidence < 1


def test_multi_field_segment_and_shapes():
    r = parse_path_detailed(
        "C:/Lib/Saga/Saga 03 - Dragon.epub", "%series%/%series% %series_index% - %title%",
        ROOT, VALID, strip_leading_zeros_fields={"series_index"}, field_patterns=SHAPES)
    # the repeated field in the stem segment still captures via its own segment compile
    assert r.values["series_index"] == "3" and r.values["title"] == "Dragon"
    assert r.values["series"] == "Saga"
    r2 = parse_path_detailed(
        "C:/Lib/Saga/Saga 03 - Dragon.epub", "%author%/%series% %series_index% - %title%",
        ROOT, VALID, strip_leading_zeros_fields={"series_index"}, field_patterns=SHAPES)
    assert r2.values == {"author": "Saga", "series": "Saga", "series_index": "3", "title": "Dragon"}


def test_optional_groups_in_segments():
    pat = "%author%/%title%[#%number%]"
    assert parse_path("C:/Lib/Ann/Book[#7].epub", pat, ROOT, VALID, field_patterns={"number": r"\d+"}) == {
        "author": "Ann", "title": "Book", "number": "7"}
    assert parse_path("C:/Lib/Ann/Book.epub", pat, ROOT, VALID, field_patterns={"number": r"\d+"}) == {
        "author": "Ann", "title": "Book", "number": ""}


def test_backslash_patterns_and_windows_paths():
    v = parse_path("C:\\Lib\\Rock\\Album X\\01 - Song.mp3", "%albumartist%\\%album%\\%track% - %title%",
                   "C:\\Lib", VALID)
    assert v == {"albumartist": "Rock", "album": "Album X", "track": "01", "title": "Song"}


def test_path_outside_root_falls_back():
    v = parse_path("D:/Elsewhere/Ann/Book.epub", "%author%/%title%", ROOT, VALID)
    assert v == {"author": "Ann", "title": "Book"}
    # no root at all
    assert parse_path("/srv/books/Ann/Book.epub", "%author%/%title%", "", VALID) == {
        "author": "Ann", "title": "Book"}


def test_no_match_returns_none_with_reasons():
    r = parse_path_detailed("C:/Lib/Ann/Book.epub", "%author%/%series% - %title%", ROOT, VALID)
    assert not r.matched and r.confidence == 0.0
    assert r.notes and "does not match" in r.notes[0]
    assert parse_path("C:/Lib/Ann/Book.epub", "%author%/%series% - %title%", ROOT, VALID) is None
    assert parse_path_detailed("C:/Lib/Ann/Book.epub", "", ROOT, VALID).notes


def test_failed_folder_segment_is_reported_not_fatal():
    r = parse_path_detailed("C:/Lib/Ann/Book.epub", "Books/%title%", ROOT, VALID)
    assert r.matched and r.values == {"title": "Book"}
    assert r.missing_segments == ["Books"] and r.confidence == 0.5
    ok = parse_path_detailed("C:/Lib/BOOKS/Book.epub", "Books/%title%", ROOT, VALID)
    assert ok.confidence == 1.0  # fixed folder names match case-insensitively


def test_confidence_ordering_and_corroboration():
    pat = "%genre%/%author%/%title%"
    path = "C:/Lib/Fantasy/Tolkien/Hobbit.epub"
    full_shaped = parse_path_detailed(path, "Fantasy/%author%/%title%", ROOT, VALID).confidence
    loose = parse_path_detailed(path, pat, ROOT, VALID).confidence
    partial = parse_path_detailed("C:/Lib/Tolkien/Hobbit.epub", pat, ROOT, VALID).confidence
    assert full_shaped > loose > partial > 0
    counts = {("genre", "fantasy"): 5, ("author", "tolkien"): 3}
    corroborated = parse_path_detailed(
        path, pat, ROOT, VALID, corroborate=lambda f, v: counts.get((f, v.casefold()), 0)).confidence
    assert corroborated > loose
    assert corroborated >= HIGH_CONFIDENCE > loose
    # a value unique to this item does not raise anything
    same = parse_path_detailed(path, pat, ROOT, VALID, corroborate=lambda f, v: 1).confidence
    assert same == loose


def test_folder_value_counts_builds_corroborator():
    paths = ["C:/Lib/SF/Herbert/Dune.epub", "C:/Lib/SF/Asimov/Found.epub"]
    rs = [parse_path_detailed(p, "%genre%/%author%/%title%", ROOT, VALID) for p in paths]
    counts = folder_value_counts(rs)
    assert counts[("genre", filename_parser.normalize_field_value("SF"))] == 2
    assert counts[("author", "herbert")] == 1


def test_normalizers_and_leading_zeros_apply():
    def display(name):
        last, _, first = name.partition(", ")
        return f"{first} {last}".strip()
    v = parse_path("C:/Lib/Tolkien, J.R.R./Hobbit 02.epub", "%author%/%title% %number%", ROOT, VALID,
                   strip_leading_zeros_fields={"number"}, normalizers={"author": display},
                   field_patterns={"number": r"\d+"})
    assert v == {"author": "J.R.R. Tolkien", "title": "Hobbit", "number": "2"}


def test_golden_filename_parsing_unchanged():
    # Plain filename parsing is untouched by the path machinery.
    assert filename_parser.parse_filename(
        "Foo 03 - My Book", "%series% %series_index% - %title%", {"series", "series_index", "title"},
        {"series_index"}, strip_leading_zeros_fields={"series_index"}
    ) == {"series": "Foo", "series_index": "3", "title": "My Book"}
    assert filename_parser.parse_filename("nomatch", "%series% - %title%", {"series", "title"}) is None


# -- dialog (offscreen) -------------------------------------------------------------

PLACEHOLDERS = [("genre", "Genre"), ("author", "Author"), ("title", "Title")]


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _dialog(qapp, history, root="C:/Lib", callback=None, paths=None):
    from redactor_common.gui.parse_filename_dialog import ParseFilenameDialog
    paths = paths or [
        "C:/Lib/Fantasy/Tolkien/Hobbit.epub",
        "C:/Lib/Fantasy/Tolkien/Silmarillion.epub",
        "C:/Lib/Fantasy/Le Guin/Earthsea.epub",
    ]
    return ParseFilenameDialog(
        paths, PLACEHOLDERS, lambda p: p, history, "%title%", {"genre", "author", "title"},
        library_root=root, on_library_root_changed=callback)


def test_dialog_path_mode_preview_and_accepted_changes(qapp):
    d = _dialog(qapp, ["%genre%/%author%/%title%"])
    assert d.is_path_mode() and d.library_root() == "C:/Lib"
    assert d.preview_table.columnCount() == 5
    changes = d.accepted_changes()
    assert changes[0] == {"genre": "Fantasy", "author": "Tolkien", "title": "Hobbit"}
    assert changes[2]["author"] == "Le Guin"
    results = d.parse_results()
    assert results[0].confidence > 0.75 and len(results[0].matched_segments) == 3
    assert "3 of 3" in d.status_label.text()


def test_dialog_switches_between_modes_and_root_row(qapp):
    d = _dialog(qapp, ["%title%"])
    assert not d.is_path_mode() and d.preview_table.columnCount() == 3
    assert d._root_row.isHidden()
    d.pattern_edit.setText("%author%/%title%")
    assert d.is_path_mode() and not d._root_row.isHidden()
    d.pattern_edit.setText("%title%")
    assert not d.is_path_mode() and d.preview_table.columnCount() == 3
    assert d.parse_results() == {}


def test_dialog_filename_mode_unchanged(qapp):
    from redactor_common.gui.parse_filename_dialog import ParseFilenameDialog
    items = ["Foo 03 - My Book.epub", "Bar 10 - Other.epub"]
    d = ParseFilenameDialog(
        items, [("series", "S")], lambda p: p, ["%series% %series_index% - %title%"], "%title%",
        {"series", "series_index", "title"}, {"series_index"},
        strip_leading_zeros_fields={"series_index"})
    assert d.preview_table.columnCount() == 3 and not d.is_path_mode()
    assert d.accepted_changes() == {
        0: {"series": "Foo", "series_index": "3", "title": "My Book"},
        1: {"series": "Bar", "series_index": "10", "title": "Other"}}


def test_dialog_library_root_callback(qapp, monkeypatch):
    from PyQt6.QtWidgets import QFileDialog
    seen = []
    d = _dialog(qapp, ["%genre%/%author%/%title%"], root="", callback=seen.append)
    assert "No library root" in d.status_label.text()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: "C:/Lib")
    d._choose_root()
    assert seen == ["C:/Lib"] and d.library_root() == "C:/Lib"
    assert d.root_label.text() == "C:/Lib"
    assert d.accepted_changes()[0]["genre"] == "Fantasy"


def test_dialog_detects_path_pattern_from_history(qapp):
    d = _dialog(qapp, ["%title% - %author%", "%genre%/%author%/%title%"])
    assert d.pattern_edit.text() == "%genre%/%author%/%title%"
    assert d.is_path_mode()


def test_dialog_low_confidence_rows_start_unticked(qapp):
    d = _dialog(qapp, ["Sci-Fi/%author%/%title%"])  # 'Sci-Fi' never matches; Tolkien corroborated
    assert d.parse_results()[0].confidence == pytest.approx(0.65)
    assert d.accepted_changes()  # >= 0.5 is ticked
    d2 = _dialog(qapp, ["A/B/%author%/%title%"], paths=["C:/Lib/Ann/Book.epub"])
    assert d2.parse_results()[0].confidence < 0.5
    assert d2.accepted_changes() == {}

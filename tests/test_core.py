import os
import pathlib

from redactor_common.core import table_settings, rename_pattern, filename_parser, search_replace, case_conversion, save_errors, error_summary, tool_locator, auto_number, series_numbering
from decimal import Decimal

def test_table_settings():
    protected = frozenset({"filename"})
    assert table_settings.is_column_visible("filename", {"filename"}, protected) is True
    assert table_settings.is_column_visible("title", {"title"}, protected) is False
    assert table_settings.is_column_visible("title", set(), protected) is True
    assert table_settings.sanitize_hidden_fields({"filename", "title"}, protected) == {"title"}
    assert table_settings.merge_column_order(["b", "a"], ["a", "b", "c"]) == ["b", "a", "c"]
    assert table_settings.merge_column_order(["z"], ["a", "b"]) == ["a", "b"]

def test_rename_pattern():
    values = {"title": "My Book", "series": "Foo", "series_index": "3"}
    assert rename_pattern.render_filename(values, "%series% %series_index% - %title%") == "Foo 3 - My Book"
    assert rename_pattern.render_filename({}, "%title%") == "untitled"
    assert rename_pattern.zero_pad_numeric_value("5.5") == "05.5"
    assert rename_pattern.zero_pad_numeric_value("7") == "07"
    assert rename_pattern.sanitize_filename("A / B: C?") == "A B C"
    assert rename_pattern.validate_filename_stem("CON") != ""
    assert rename_pattern.validate_filename_stem("Good Name") == ""
    taken = set()
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p1 = rename_pattern.unique_path(d, "Book", ".epub", taken)
        taken.add(os.path.normcase(os.path.abspath(p1)))
        with open(p1, "w") as f: f.write("x")
        p2 = rename_pattern.unique_path(d, "Book", ".epub", set())
        assert p2 != p1
        assert "(2)" in p2

def test_filename_parser():
    valid = {"series", "series_index", "title"}
    numeric = {"series_index"}
    result = filename_parser.parse_filename("Foo 03 - My Book", "%series% %series_index% - %title%", valid, numeric, strip_leading_zeros_fields={"series_index"})
    assert result == {"series": "Foo", "series_index": "3", "title": "My Book"}
    assert filename_parser.parse_filename("nomatch", "%series% - %title%", valid) is None
    count = filename_parser.count_matching_filenames(["Foo 03 - Book1", "Bar 04 - Book2"], "%series% %series_index% - %title%", valid, numeric)
    assert count == 2
    # tie-break: both patterns match count=1 against a single filename, so
    # the one listed FIRST wins (natural fit for newest-first history)
    best = filename_parser.best_matching_pattern(["Foo 03 - Book1"], ["%title%", "%series% %series_index% - %title%"], valid, numeric)
    assert best[0] == "%title%"
    # a pattern requiring a numeric series_index only matches files that
    # actually have a numeric-shaped second field -- clear winner
    best2 = filename_parser.best_matching_pattern(
        ["Foo 03 - Book1", "Bar 04 - Book2", "Baz NotANumber - Book3"],
        ["%series% %series_index% - %title%", "%series% NotANumber - %title%"], valid, numeric,
    )
    assert best2 == ("%series% %series_index% - %title%", 2)

def test_search_replace():
    assert search_replace.apply_replace("Hello World", "World", "There") == "Hello There"
    assert search_replace.would_change("abc", "b", "x") is True
    assert search_replace.would_change("abc", "z", "x") is False
    try:
        search_replace.apply_replace("abc", "[", "x", use_regex=True)
        assert False, "should have raised"
    except search_replace.SearchReplaceError:
        pass

def test_case_conversion():
    assert case_conversion.apply_case_conversion("hello world", "UPPERCASE") == "HELLO WORLD"
    assert case_conversion.apply_case_conversion("the lord of the rings", "Title Case") == "The Lord of the Rings"
    assert case_conversion.apply_case_conversion("HELLO", "lowercase") == "hello"
    assert case_conversion.apply_case_conversion("hello world. bye", "Sentence case") == "Hello world. bye"

def test_save_errors():
    class FakeErr(OSError):
        pass
    e = FakeErr("path too long")
    assert save_errors.is_path_too_long_error(e) is True
    assert "shorten" in save_errors.describe_save_error(e)
    assert save_errors.describe_save_error(ValueError("other")) == "other"

def test_error_summary():
    errors = [f"error {i}" for i in range(10)]
    s = error_summary.summarize_errors(errors, max_shown=3)
    assert s.count(";") == 2
    assert s.endswith(", ...")

def test_auto_number():
    assert auto_number.generate_auto_number(0, start=1, increment=1, padding=2) == "01"
    assert auto_number.generate_auto_number(1, start=1, increment=1, padding=2) == "02"
    assert auto_number.generate_auto_number(9, start=1, increment=1, padding=2) == "10"
    assert auto_number.generate_auto_number(0, start=5, increment=10, padding=0) == "5"
    assert auto_number.generate_auto_number(2, start=5, increment=10, padding=0) == "25"
    assert auto_number.generate_auto_number(0, start=-5, increment=1, padding=3) == "-05"
    assert auto_number.generate_auto_number(3, start=0, increment=5, padding=0) == "15"
    assert auto_number.generate_auto_number(0, start=1, increment=1, padding=5) == "00001"
    assert auto_number.apply_auto_number_to_text_field("Pilot", "01", " - ") == "01 - Pilot"
    assert auto_number.apply_auto_number_to_text_field("", "01", " - ") == "01"
    assert auto_number.apply_auto_number_to_text_field("Pilot", "01", ". ") == "01. Pilot"

def test_series_numbering():
    assert series_numbering.parse_decimal("5", "1") == Decimal("5")
    assert series_numbering.parse_decimal("1.5", "1") == Decimal("1.5")
    assert series_numbering.parse_decimal("", "3") == Decimal("3")
    assert series_numbering.parse_decimal("not a number", "2") == Decimal("2")
    assert series_numbering.parse_decimal("-2", "1") == Decimal("-2")

    assert series_numbering.format_series_number(Decimal("1.00")) == "1"
    assert series_numbering.format_series_number(Decimal("100")) == "100"  # not "1E+2"
    assert series_numbering.format_series_number(Decimal("1.50")) == "1.5"
    assert series_numbering.format_series_number(Decimal("-1")) == "-1"

    assert series_numbering.generate_series_numbers(5) == ["1", "2", "3", "4", "5"]
    assert series_numbering.generate_series_numbers(3, start="10") == ["10", "11", "12"]
    assert series_numbering.generate_series_numbers(4, start="1", step="2") == ["1", "3", "5", "7"]
    # the classic float-accumulation trap (1 + 0.5*3 can drift to
    # 2.4999999999999996 in binary float) -- Decimal must not have it
    assert series_numbering.generate_series_numbers(5, start="1", step="0.5") == ["1", "1.5", "2", "2.5", "3"]
    assert series_numbering.generate_series_numbers(0) == []
    assert series_numbering.generate_series_numbers(-3) == []
    assert series_numbering.generate_series_numbers(3, start="", step="") == ["1", "2", "3"]
    assert series_numbering.generate_series_numbers(3, start="abc", step="xyz") == ["1", "2", "3"]
    assert series_numbering.generate_series_numbers(3, start="0.5", step="1") == ["0.5", "1.5", "2.5"]

def test_tool_locator():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        tools_dir = d / "tools"
        tools_dir.mkdir()
        bundled_exe = tools_dir / "mp3val.exe"
        bundled_exe.write_bytes(b"")

        # bundled dir wins when no override
        assert tool_locator.find_tool("mp3val.exe", tools_dir=tools_dir) == bundled_exe

        # a real, existing override wins even with a bundled copy present
        override_exe = d / "custom_mp3val.exe"
        override_exe.write_bytes(b"")
        result = tool_locator.find_tool("mp3val.exe", tools_dir=tools_dir, override=str(override_exe))
        assert result == override_exe

        # a broken override is NOT found, even though a bundled copy exists --
        # no silent fallback
        missing = d / "does_not_exist.exe"
        result = tool_locator.find_tool("mp3val.exe", tools_dir=tools_dir, override=str(missing))
        assert result is None

        # no bundled dir, no override, nothing on a fake empty PATH -> None
        empty_tools_dir = d / "empty_tools"
        empty_tools_dir.mkdir()
        result = tool_locator.find_tool("definitely_not_a_real_binary_xyz", tools_dir=empty_tools_dir)
        assert result is None

        # bare exe_name (no ".exe") still finds a ".exe"-suffixed bundled file
        result = tool_locator.find_tool("mp3val", tools_dir=tools_dir)
        assert result == bundled_exe

if __name__ == "__main__":
    test_table_settings()
    test_rename_pattern()
    test_filename_parser()
    test_search_replace()
    test_case_conversion()
    test_save_errors()
    test_error_summary()
    test_auto_number()
    test_series_numbering()
    test_tool_locator()
    print("ALL TESTS PASSED")

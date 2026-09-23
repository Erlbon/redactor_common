"""Tests for the core modules promoted 2026-09-23: subprocess_utils,
app_paths, crash_log, managed_list, pattern_history, version_bump,
languages, plus the rename/parse/case/tool-lookup additions merged in
from epub and video."""

import datetime
import subprocess
import sys
from pathlib import Path

import pytest

from redactor_common.core import (
    app_paths, case_conversion, crash_log, filename_parser, languages, managed_list,
    pattern_history, rename_pattern, subprocess_utils, tool_locator, version_bump,
)


# -- subprocess_utils ----------------------------------------------------------

def test_run_tool_decodes_utf8_output_and_closes_stdin():
    script = (
        "import sys; data = sys.stdin.read(); "
        "sys.stdout.buffer.write('Am\\u00e9lie'.encode('utf-8')); "
        "sys.stderr.buffer.write(repr(data).encode())"
    )
    result = subprocess_utils.run_tool([sys.executable, "-c", script], timeout=30)
    assert result.returncode == 0
    assert result.stdout == "Amélie"  # not cp1252 mojibake
    assert result.stderr == "''"  # stdin was DEVNULL, so read() returned at once


def test_run_tool_replaces_invalid_utf8_instead_of_raising():
    script = "import sys; sys.stdout.buffer.write(b'ok \\xff\\xfe')"
    result = subprocess_utils.run_tool([sys.executable, "-c", script], timeout=30)
    assert result.stdout.startswith("ok ")


def test_run_tool_times_out():
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess_utils.run_tool([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5)


def test_run_tool_missing_executable_raises_file_not_found():
    with pytest.raises(FileNotFoundError):
        subprocess_utils.run_tool(["definitely-not-a-real-tool-xyz"], timeout=5)


def test_no_window_kwargs_matches_platform():
    kwargs = subprocess_utils.no_window_kwargs()
    assert ("creationflags" in kwargs) == (sys.platform == "win32")


# -- app_paths -------------------------------------------------------------------

def test_app_paths_dev_mode(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert app_paths.base_dir(tmp_path) == tmp_path.resolve()
    assert app_paths.tools_dir(tmp_path) == tmp_path.resolve() / "tools"
    assert app_paths.asset_path("assets/icon.ico", tmp_path) == tmp_path.resolve() / "assets/icon.ico"
    assert app_paths.settings_ini_path("cbzredactor", tmp_path).name == "cbzredactor_settings.ini"
    assert app_paths.crash_log_path("cbzredactor", tmp_path).name == "cbzredactor_crash.log"


def test_app_paths_frozen(tmp_path, monkeypatch):
    exe = tmp_path / "dist" / "app.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "meipass"), raising=False)
    assert app_paths.base_dir("ignored") == exe.parent
    assert app_paths.asset_path("icon.ico", "ignored") == tmp_path / "meipass" / "icon.ico"


# -- crash_log ---------------------------------------------------------------------

def test_crash_log_writes_entry_and_chains_hooks(tmp_path, monkeypatch):
    log = tmp_path / "sub" / "app_crash.log"
    seen = []
    monkeypatch.setattr(sys, "excepthook", lambda *a: seen.append("previous"))
    crash_log.install(log, also_call=lambda *a: seen.append("dialog"), enable_faulthandler=False)
    try:
        raise ValueError("boom")
    except ValueError:
        sys.excepthook(*sys.exc_info())
    text = log.read_text(encoding="utf-8")
    assert "ValueError: boom" in text and crash_log.ENTRY_SEPARATOR in text
    assert seen == ["dialog", "previous"]


def test_crash_log_failing_dialog_never_blocks_previous_hook(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(sys, "excepthook", lambda *a: seen.append("previous"))

    def broken_dialog(*_a):
        raise RuntimeError("dialog broke")

    crash_log.install(tmp_path / "x.log", also_call=broken_dialog, enable_faulthandler=False)
    sys.excepthook(ValueError, ValueError("x"), None)
    assert seen == ["previous"]


def test_crash_log_trim_keeps_whole_recent_entries(tmp_path):
    log = tmp_path / "c.log"
    for i in range(50):
        crash_log.write_crash_entry(ValueError, ValueError(f"entry {i} " + "x" * 200), None, log)
    crash_log.trim_if_oversized(log, max_bytes=2000)
    text = log.read_text(encoding="utf-8")
    assert len(text.encode("utf-8")) <= 2000
    assert text.startswith(crash_log.ENTRY_SEPARATOR)
    assert "entry 49" in text and "entry 0 " not in text


# -- managed_list ------------------------------------------------------------------

def test_managed_list_names():
    merged = managed_list.merge_names(["Action", "Drama"], ["drama", " Noir ", ""])
    assert merged == ["Action", "Drama", "Noir"]
    assert managed_list.exclude_hidden_names(["Action", "Drama"], ["ACTION"]) == ["Drama"]
    assert managed_list.add_name(["A"], "a") == ["A"]
    assert managed_list.add_name(["A"], " B ") == ["A", "B"]
    assert managed_list.remove_name(["A", "B"], "b") == ["A"]


def test_managed_list_pairs():
    merged = managed_list.merge_pairs([("en", "English")], [("en", "Dup"), ("xx", "Custom"), ("", "Bad")])
    assert merged == [("en", "English"), ("xx", "Custom")]
    assert managed_list.exclude_hidden_codes([("en", "English"), ("de", "German")], ["de"]) == [("en", "English")]
    assert managed_list.add_pair([("xx", "Old")], "xx", "New") == [("xx", "New")]
    assert managed_list.remove_pair([("xx", "A"), ("yy", "B")], "xx") == [("yy", "B")]
    assert managed_list.add_code(["a"], "a") == ["a"]


def test_managed_list_json_roundtrip_and_corruption():
    assert managed_list.decode_names(managed_list.encode_names(["a", "b"])) == ["a", "b"]
    assert managed_list.decode_pairs(managed_list.encode_pairs([("en", "English")])) == [("en", "English")]
    assert managed_list.decode_names("not json") == []
    assert managed_list.decode_pairs('[["en"], 5, ["de", "German"]]') == [("de", "German")]


# -- pattern_history -----------------------------------------------------------------

def test_pattern_history():
    assert pattern_history.dedupe_and_trim(["a", "b"], "b") == ["b", "a"]
    assert pattern_history.dedupe_and_trim(["a"], "  ") == ["a"]
    assert len(pattern_history.dedupe_and_trim([str(i) for i in range(20)], "new", 15)) == 15
    encoded = pattern_history.encode_history(["%title%, %year%", "%a%"])
    assert pattern_history.decode_history(encoded) == ["%title%, %year%", "%a%"]
    # video's legacy \x1f format still reads back
    assert pattern_history.decode_history("%a%\x1f%b%") == ["%a%", "%b%"]
    assert pattern_history.decode_history("") == []


# -- version_bump ------------------------------------------------------------------------

def test_version_bump_same_day_and_new_day(tmp_path):
    f = tmp_path / "version.py"
    f.write_text('APP_NAME = "x"\nAPP_VERSION = "2026-09-23#04"\n', encoding="utf-8")
    old, new = version_bump.bump_version_file(f, today=datetime.date(2026, 9, 23))
    assert (old, new) == ("2026-09-23#04", "2026-09-23#05")
    old, new = version_bump.bump_version_file(f, today=datetime.date(2026, 9, 24))
    assert new == "2026-09-24#01"
    assert 'APP_VERSION = "2026-09-24#01"' in f.read_text(encoding="utf-8")


def test_version_bump_missing_line_exits(tmp_path):
    f = tmp_path / "version.py"
    f.write_text("nothing here\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        version_bump.bump_version_file(f)


def test_pep440_from():
    assert version_bump.pep440_from("2026-09-04#10") == "2026.9.4.10"


# -- languages ---------------------------------------------------------------------------

def test_languages_convert_between_styles():
    assert languages.convert("de", "alpha3") == "deu"
    assert languages.convert("de", "alpha3b") == "ger"
    assert languages.convert("ger", "alpha2") == "de"
    assert languages.convert("en-GB", "alpha3") == "eng"
    assert languages.convert("zz", "alpha3") == "zz"  # unknown: unchanged
    assert languages.convert("und", "alpha2") == "und"  # no 2-letter form: unchanged
    assert languages.name_for("FRA") == "French"
    assert languages.language_pairs(["en", "zz", "no"], "alpha3") == [("eng", "English"), ("nor", "Norwegian")]


# -- case_conversion (merged from video) -----------------------------------------------------

def test_title_case_clause_boundaries_and_first_letter():
    t = case_conversion.to_title_case
    assert t("the lord of the rings") == "The Lord of the Rings"
    assert t("star wars: a new hope") == "Star Wars: A New Hope"
    assert t("mission - a test") == "Mission - A Test"
    assert t("(the end)") == "(The End)"
    assert t("don't stop") == "Don't Stop"
    assert t("a  b") == "A  B"  # consecutive spaces kept


def test_case_mode_aliases():
    assert case_conversion.apply_case_conversion("abc", "upper") == "ABC"
    assert case_conversion.apply_case_conversion("abc", "UPPERCASE") == "ABC"
    assert case_conversion.apply_case_conversion("abc", "nonsense") == "abc"


# -- rename_pattern (merged from epub) -------------------------------------------------------------

def test_render_optional_groups():
    pattern = "%author% [%series% %series_index%] - %title% (%year%)"
    assert rename_pattern.render_filename({"author": "A", "title": "T"}, pattern) == "A - T"
    assert rename_pattern.render_filename(
        {"author": "A", "series": "S", "title": "T", "year": "2001"}, pattern
    ) == "A [S] - T (2001)"
    # a group with no tokens is ordinary literal text
    assert rename_pattern.render_filename({"title": "T"}, "%title% (unabridged)") == "T (unabridged)"
    # opt-out keeps the old behavior
    assert rename_pattern.render_filename({"title": "T"}, "%title% (%year%)", optional_groups=False) == "T ()"


def test_render_aliases():
    assert rename_pattern.render_filename({"genres": "Sci-Fi"}, "%tags%", aliases={"tags": "genres"}) == "Sci-Fi"


# -- filename_parser (merged from epub) ----------------------------------------------------------------

KEYS = {"authors", "series", "series_index", "title", "year", "month"}
SHAPES = {
    "series_index": filename_parser.SERIES_INDEX_FIELD_PATTERN,
    "year": filename_parser.YEAR_FIELD_PATTERN,
    "month": filename_parser.MONTH_FIELD_PATTERN,
}


def _parse(stem, pattern, **kw):
    return filename_parser.parse_filename(
        stem, pattern, KEYS, strip_leading_zeros_fields={"series_index"},
        field_patterns=SHAPES, normalizers={"month": filename_parser.normalize_month}, **kw,
    )


def test_parse_hyphenated_name_not_split_on_inner_hyphen():
    # Epub's loose-whitespace rule alone parsed this as authors="Jean".
    assert _parse("Jean-Paul Sartre - Nausea", "%authors% - %title%") == {
        "authors": "Jean-Paul Sartre", "title": "Nausea",
    }


def test_parse_loose_whitespace_fallback():
    assert _parse("Author-Title", "%authors% - %title%") == {"authors": "Author", "title": "Title"}
    assert _parse("Author-Title", "%authors% - %title%", loose_whitespace_fallback=False) is None


def test_parse_optional_group_present_and_absent():
    pattern = "%authors% - [%series% %series_index%] - %title%"
    assert _parse("A - [Saga 03] - T", pattern) == {
        "authors": "A", "series": "Saga", "series_index": "3", "title": "T",
    }
    parsed = _parse("A - T", pattern)
    assert parsed["authors"] == "A" and parsed["title"] == "T" and parsed["series"] == ""


def test_parse_series_index_shapes_and_months():
    assert _parse("Saga 01-06. - T", "%series% %series_index% - %title%")["series_index"] == "1-6"
    assert _parse("Saga 3.5 - T", "%series% %series_index% - %title%")["series_index"] == "3.5"
    assert _parse("T 2020 Jan.", "%title% %year% %month%") == {"title": "T", "year": "2020", "month": "1"}


def test_strip_leading_zeros_shapes():
    s = filename_parser.strip_leading_zeros
    assert (s("007"), s("03.5"), s("01-06"), s("5."), s("0")) == ("7", "3.5", "1-6", "5", "0")


def test_field_value_counts():
    stems = ["Pratchett - Mort", "PRATCHETT - Eric", "Gaiman - Coraline"]
    counts = filename_parser.field_value_counts(stems, "%authors% - %title%", "authors", KEYS)
    assert counts == {"pratchett": 2, "gaiman": 1}


def test_best_matching_pattern_passes_parse_options():
    stems = ["Saga 01-02 - T"]
    best = filename_parser.best_matching_pattern(
        stems, ["%series% %series_index% - %title%"], KEYS, field_patterns=SHAPES
    )
    assert best == ("%series% %series_index% - %title%", 1)


# -- tool_locator install-dir tier --------------------------------------------------------------------

def test_find_tool_install_dirs_after_path(tmp_path):
    install = tmp_path / "Calibre2"
    install.mkdir()
    exe = install / "ebook-convert.exe"
    exe.write_text("")
    found = tool_locator.find_tool("ebook-convert", install_dirs=[tmp_path / "missing", install],
                                   which=lambda _n: None)
    assert found == exe
    # PATH still wins over an install dir
    on_path = str(tmp_path / "on_path.exe")
    assert tool_locator.find_tool("ebook-convert", install_dirs=[install], which=lambda _n: on_path) == Path(on_path)
    assert tool_locator.find_tool("ebook-convert", which=lambda _n: None) is None


def test_windows_program_dirs_platform_gated():
    dirs = tool_locator.windows_program_dirs("Calibre2")
    if sys.platform == "win32":
        assert dirs and all(d.name == "Calibre2" for d in dirs)
    else:
        assert dirs == []


def test_reserved_names_with_a_dotted_part():
    assert rename_pattern.is_reserved_name("CON")
    assert rename_pattern.is_reserved_name("con.mp4")
    assert not rename_pattern.is_reserved_name("Console")
    assert rename_pattern.validate_filename_stem("NUL.part1") != ""
    assert rename_pattern.render_filename({"t": "CON.x"}, "%t%") == "_CON.x"

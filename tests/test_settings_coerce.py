"""core/settings_coerce.py: every behaviour the four settings-adapter copies had."""

import math

import pytest

from redactor_common.core import settings_coerce as sc

GARBAGE = [None, "x", 1.5, [1], {"a": 1}, object(), b"bytes", float("nan")]


def test_required_turns_none_into_value_error():
    assert sc.required(0) == 0 and sc.required("") == "" and sc.required([]) == []
    with pytest.raises(ValueError, match="expected text"):
        sc.required(None, "expected text")


def test_bool_never_passes_as_int():
    # The trap all four copies solved: True is an int in Python.
    assert sc.coerce_int(True) is None and sc.coerce_int(False, default=7) == 7
    assert sc.coerce_int(3) == 3
    assert sc.coerce_float(True) is None
    assert sc.coerce_width_map({"a": True}) == {}
    assert sc.coerce_width_map({"a": True}, strict=True) is None
    assert not sc.is_int(True) and sc.is_int(0)


def test_coerce_bool():
    assert sc.coerce_bool(True) is True and sc.coerce_bool(False) is False
    for value in (1, 0, "true", "1", None, []):
        assert sc.coerce_bool(value) is None
    assert sc.coerce_bool("true", default=False) is False


def test_coerce_int_bounds_and_defaults():
    assert sc.coerce_int(5, 1, 10) == 5
    assert sc.coerce_int(1, 1, 10) == 1 and sc.coerce_int(10, 1, 10) == 10  # inclusive (cbz, video, mp3, epub)
    assert sc.coerce_int(0, 1, 10) is None and sc.coerce_int(11, 1, 10, default=2) == 2
    assert sc.coerce_int(-5) == -5  # unbounded
    for value in GARBAGE + ["5", 5.0]:
        assert sc.coerce_int(value, 0, 99, default=-1) == -1


def test_coerce_float():
    assert sc.coerce_float(2) == 2.0 and sc.coerce_float(2.5, 0, 3) == 2.5
    assert sc.coerce_float(3.5, 0, 3) is None
    for value in (float("nan"), float("inf"), "2.5", None):
        assert sc.coerce_float(value) is None


def test_coerce_choice():
    choices = ("wrap", "ellipsis", "clip")
    assert sc.coerce_choice("clip", choices) == "clip"
    assert sc.coerce_choice("Clip", choices) is None
    assert sc.coerce_choice(["clip"], choices, "wrap") == "wrap"  # unhashable: no crash
    assert sc.coerce_choice("x", iter(choices), "wrap") == "wrap"  # a one-shot iterable works
    assert sc.coerce_choice(True, (0, 1)) is None  # True is not the choice 1
    assert sc.coerce_choice(1, (0, 1)) == 1
    assert sc.coerce_choice(True, (True, False)) is True


def test_coerce_str_and_text_checks():
    assert sc.coerce_str("a") == "a" and sc.coerce_str("") == ""
    assert sc.coerce_str(5) is None and sc.coerce_str(None, default="d") == "d"
    # video's _text_ok: single line, no NUL, at most 2048 characters
    assert sc.coerce_str("a\nb", single_line=True) is None
    assert sc.coerce_str("a\rb", single_line=True) is None
    assert sc.coerce_str("a\x00b", single_line=True) is None
    assert sc.coerce_str("a" * 2049, single_line=True, max_len=2048) is None
    assert sc.coerce_str("a" * 2048, single_line=True, max_len=2048) == "a" * 2048
    assert sc.coerce_str("abc", max_len=2) is None
    assert sc.coerce_str("a\nb") == "a\nb"  # no checks asked
    assert sc.coerce_str("x", check=lambda s: s == "y") is None
    assert sc.coerce_str("x", check=lambda s: 1 / 0) is None  # a raising check is a no, not a crash


def test_coerce_str_list_cbz_mp3_keep_blanks():
    assert sc.coerce_str_list(["a", "", " "]) == ["a", "", " "]
    assert sc.coerce_str_list(["a", 1]) is None  # one non-string rejects the whole list (all copies)
    assert sc.coerce_str_list("a") is None and sc.coerce_str_list(None, default=[]) == []
    value = ["x"]
    assert sc.coerce_str_list(value) is not value  # a copy


def test_coerce_str_list_epub_drops_blanks():
    assert sc.coerce_str_list(["a", "", "  ", "b"], drop_blank=True) == ["a", "b"]


def test_coerce_str_list_video_clean_list():
    sep = "\x1f"
    value = [" a ", "a", "", "b\x1fc", "d\ne", "f"]
    assert sc.coerce_str_list(
        value, strip=True, drop_blank=True, forbidden=sep, single_line=True, max_len=2048, dedupe=True
    ) == ["a", "f"]
    assert sc.coerce_str_list(["x", "y", "z"], limit=2) == ["x", "y"]
    assert sc.coerce_str_list(["a,b", "c"], forbidden=",") == ["c"]  # video's hidden-columns codec


def test_coerce_pairs():
    assert sc.coerce_pairs([["en", "English"], ("de", "German")]) == [("en", "English"), ("de", "German")]
    assert sc.coerce_pairs([["en", "English"]], as_lists=True) == [["en", "English"]]  # epub's shape
    for bad in ([["en"]], [["en", 1]], [["a", "b", "c"]], "en", [None], {"en": "English"}):
        assert sc.coerce_pairs(bad) is None
    assert sc.coerce_pairs([["", "English"]]) == [("", "English")]  # cbz/mp3 keep blanks
    assert sc.coerce_pairs([["", "English"]], require_nonblank=True) is None  # epub rejects
    assert sc.coerce_pairs([["en", " "]], require_nonblank=True, default=[]) == []
    assert sc.coerce_pairs([]) == []


def test_coerce_width_map_cbz_strict():
    assert sc.coerce_width_map({"title": 120}, lo=1, hi=9999, strict=True) == {"title": 120}
    assert sc.coerce_width_map({"title": 120, "x": 0}, lo=1, hi=9999, strict=True) is None
    assert sc.coerce_width_map({"title": 10000}, lo=1, hi=9999, strict=True) is None
    assert sc.coerce_width_map({"title": "120"}, strict=True) is None
    assert sc.coerce_width_map({1: 120}, strict=True) is None
    assert sc.coerce_width_map([("a", 1)]) is None


def test_coerce_width_map_epub_video_lenient():
    value = {"title": 120, "x": 0, "y": -3, "z": "wide", "big": 6000, "ok": 5000}
    assert sc.coerce_width_map(value, hi=5000) == {"title": 120, "ok": 5000}  # video: 0 < w <= 5000
    assert sc.coerce_width_map(value) == {"title": 120, "big": 6000, "ok": 5000}  # epub: w > 0
    assert sc.coerce_width_map(value, allowed_keys=["title", "nope"]) == {"title": 120}  # epub column keys


def test_raw_text():
    assert sc.raw_text(None) == "" and sc.raw_text(None, "d") == "d"
    assert sc.raw_text("a") == "a"
    assert sc.raw_text(["[1", "2]"]) == "[1, 2]"  # QSettings' reading of an unquoted hand-typed "[1, 2]"
    assert sc.raw_text(5) == "5" and sc.raw_text(b"x") == "x"
    assert sc.raw_text(True) == "true" and sc.raw_text(False) == "false"  # as the ini file spells them


def test_parse_bool():
    for raw in (True, "true", "TRUE", " yes ", "on", "1", 1):
        assert sc.parse_bool(raw) is True
    for raw in (False, "false", "No", "off", "0", 0):
        assert sc.parse_bool(raw, True) is False
    assert sc.parse_bool(None, True) is True and sc.parse_bool("", True) is True  # unset = default (video)
    assert sc.parse_bool("garbage", True) is True and sc.parse_bool(["a"], False) is False
    # cbz's history switch / video's raw == "1": anything that isn't a true word is off
    assert sc.parse_bool("garbage", True, other_is_false=True) is False
    assert sc.parse_bool(None, False, other_is_false=True) is False


def test_parse_int():
    assert sc.parse_int("12", 0) == 12 and sc.parse_int(" 12 ", 0) == 12 and sc.parse_int(12, 0) == 12
    for raw in ("abc", "2.5", ["1", "2"], None, "", True, {"a": 1}):
        assert sc.parse_int(raw, 7) == 7  # cbz _int_value: never raises
    assert sc.parse_int("50", 1, 1, 10) == 1 and sc.parse_int("-1", 3, 0) == 3  # video's range check


def test_parse_float():
    assert sc.parse_float("2.5", 0.0) == 2.5 and sc.parse_float(3, 0.0) == 3.0
    for raw in ("x", None, "nan", "inf", True, [1]):
        assert sc.parse_float(raw, 1.0) == 1.0
    assert sc.parse_float("5", 1.0, 0, 2) == 1.0
    assert not math.isnan(sc.parse_float("nan", 0.0))


def test_parse_choice():
    assert sc.parse_choice("clip", ("wrap", "clip"), "wrap") == "clip"
    assert sc.parse_choice("bogus", ("wrap", "clip"), "wrap") == "wrap"
    assert sc.parse_choice(None, ("wrap", "clip"), "wrap") == "wrap"


def test_parse_json_family():
    assert sc.parse_json('{"a": 1}') == {"a": 1}
    assert sc.parse_json("", "d") == "d" and sc.parse_json("{nope", "d") == "d"
    assert sc.parse_json_list("[1, 2]") == [1, 2] and sc.parse_json_list('{"a": 1}') == []
    assert sc.parse_json_list(["[1", "2]"]) == [1, 2]  # unquoted comma value rejoined
    assert sc.parse_str_list('["a", 1, "", " "]') == ["a", "", " "]
    assert sc.parse_str_list('["a", 1, "", " "]', drop_blank=True) == ["a"]  # epub's last session files


def test_parse_width_map_variants():
    raw = '{"title": 120, "a": "80", "b": 0, "c": "wide", "d": 6000, "e": true}'
    assert sc.parse_width_map(raw) == {"title": 120, "a": 80, "d": 6000}  # epub by key
    assert sc.parse_width_map(raw, hi=5000, numeric_strings=False) == {"title": 120}  # video
    assert sc.parse_width_map(raw, lo=None, strict=True) == {}  # cbz: one bad entry empties it
    assert sc.parse_width_map('{"title": 120, "b": 0}', lo=None, strict=True) == {"title": 120, "b": 0}
    assert sc.parse_width_map('{"0": 50, "x": 60, "2": "70"}', int_keys=True) == {0: 50, 2: 70}  # epub by index
    assert sc.parse_width_map("[1]") == {} and sc.parse_width_map("") == {} and sc.parse_width_map(None) == {}


def test_parse_int_set():
    assert sc.parse_int_set('[3, "4", -1, "x", true, 3]') == {3, 4}
    assert sc.parse_int_set("[-1, 2]", minimum=None) == {-1, 2}
    assert sc.parse_int_set("nope") == set()


def test_recipe_from_json_value():
    assert sc.recipe_from_json_value("") == ""
    assert sc.recipe_from_json_value({"order": ["a"]}) == '{"order": ["a"]}'
    assert sc.recipe_from_json_value([1]) == "[1]"
    for bad in ("text", 5, None, True):
        assert sc.recipe_from_json_value(bad) is None

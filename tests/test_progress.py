"""Tests for gui/progress.py's pure logic: _elide_label(). The rest of
run_with_progress needs a real QProgressDialog and is exercised instead
by whichever consuming app's own test suite uses it, consistent with
this repo's existing core-logic-only test convention (see test_theme.py)."""

from redactor_common.gui.progress import _elide_label


def test_short_text_unchanged():
    assert _elide_label("short.epub") == "short.epub"
    print("PASS: text already under the limit is returned unchanged")


def test_text_exactly_at_limit_unchanged():
    text = "x" * 70
    assert _elide_label(text, max_length=70) == text
    print("PASS: text exactly at the limit is not elided")


def test_long_text_truncated_with_ellipsis():
    text = "x" * 100
    result = _elide_label(text, max_length=70)
    assert len(result) == 70, len(result)
    assert result.endswith("…"), result
    assert result[:69] == "x" * 69
    print("PASS: text over the limit is truncated to exactly max_length, ending in an ellipsis")


def test_default_max_length_used_when_omitted():
    text = "x" * 200
    result = _elide_label(text)
    assert len(result) == 70, len(result)
    print("PASS: the default max_length (70) applies when none is given")


def test_empty_and_none_text_handled():
    assert _elide_label("") == ""
    assert _elide_label(None) == ""
    print("PASS: empty string and None both return an empty string, no crash")


def test_trailing_whitespace_before_ellipsis_stripped():
    # "Loading: some file name here" cut mid-word can leave a trailing
    # space right before the "..." -- strip it so it reads cleanly.
    text = "Loading: " + ("a " * 40)
    result = _elide_label(text, max_length=20)
    assert not result[:-1].endswith(" "), result
    assert result.endswith("…"), result
    print("PASS: trailing whitespace right before the ellipsis is stripped")


if __name__ == "__main__":
    test_short_text_unchanged()
    test_text_exactly_at_limit_unchanged()
    test_long_text_truncated_with_ellipsis()
    test_default_max_length_used_when_omitted()
    test_empty_and_none_text_handled()
    test_trailing_whitespace_before_ellipsis_stripped()
    print("\nALL PROGRESS TESTS PASSED")

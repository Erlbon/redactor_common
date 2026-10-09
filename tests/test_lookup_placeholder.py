"""The lookup dialog's Found side says what happened when there is no image."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from redactor_common.gui.lookup_dialog import LookupDialogBase, LookupResult  # noqa: E402

placeholder = LookupDialogBase._found_placeholder


def test_a_match_without_a_cover_says_so_positively():
    text = placeholder(LookupResult(fields={"series": "Batgirl"}))
    assert "Match found" in text and "no cover" in text


def test_no_match_an_error_and_not_searched_are_told_apart():
    assert placeholder(LookupResult()) == "No match"
    assert placeholder(LookupResult(error="boom")) == "Lookup failed"
    assert placeholder(None) == "Not searched yet"

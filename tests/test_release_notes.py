"""Tests for core/release_notes.py -- what goes into a GitHub Release's notes."""

import pytest

from redactor_common.core.release_notes import build_notes, previous_release

CHANGELOG = """# Changelog

## 2026-09-28#08 -- Local GCD

Local lookup.

## 2026-09-28#07 -- No freezing

Background lookups.

## 2026-09-28#04 -- Low-res tags

Tagging.

## 2026-09-23#02 -- Cover area

Splitter.

## 2026-09-23#01 -- Covers off the GUI thread

Thumbnails.
"""


def test_previous_release_is_the_highest_older_tag():
    tags = ["v2026-09-28-04", "v2026-09-23-01", "v2026-09-19-01"]
    assert previous_release("v2026-09-28-08", tags) == "v2026-09-28-04"
    # Re-running an older tag still gets its own range.
    assert previous_release("v2026-09-28-04", tags) == "v2026-09-23-01"
    assert previous_release("v2026-09-10-01", tags) is None
    assert previous_release("v2026-09-28-08", ["not-a-version", "v2026-09-23-01"]) == "v2026-09-23-01"


def test_notes_cover_every_version_since_the_previous_release():
    notes = build_notes(CHANGELOG, "v2026-09-28-08", "v2026-09-28-04")
    assert "## 2026-09-28#08 -- Local GCD" in notes
    assert "## 2026-09-28#07 -- No freezing" in notes
    assert "Low-res tags" not in notes  # already in the previous release
    assert notes.index("#08") < notes.index("#07")  # newest first
    assert notes.startswith("Everything since v2026-09-28-04 (2 versions):")


def test_the_release_that_missed_its_notes_would_now_get_them():
    notes = build_notes(CHANGELOG, "v2026-09-28-04", "v2026-09-23-01")
    assert "Tagging." in notes and "Splitter." in notes
    assert "Thumbnails." not in notes


def test_single_version_keeps_the_old_plain_body():
    assert build_notes(CHANGELOG, "v2026-09-28-08", "v2026-09-28-07") == "Local lookup.\n"


def test_later_sections_are_never_included():
    notes = build_notes(CHANGELOG, "v2026-09-28-07", "v2026-09-28-04")
    assert notes == "Background lookups.\n"


def test_missing_section_for_the_tag_fails_loudly():
    with pytest.raises(ValueError, match="no section"):
        build_notes(CHANGELOG, "v2026-09-28-09", "v2026-09-28-08")
    with pytest.raises(ValueError, match="Not a release tag"):
        build_notes(CHANGELOG, "2026-09-28#08", None)


def test_runs_as_a_module_from_an_app_root(tmp_path):
    """How the release workflows call it: python -m ... from the repo root."""
    import subprocess
    import sys

    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    (tmp_path / "tags.txt").write_text("v2026-09-28-04\n", encoding="utf-8")
    subprocess.run(
        [sys.executable, "-m", "redactor_common.core.release_notes", "v2026-09-28-08", "tags.txt", "notes.md"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    notes = (tmp_path / "notes.md").read_text(encoding="utf-8")
    assert "## 2026-09-28#08" in notes and "## 2026-09-28#07" in notes and "#04" not in notes

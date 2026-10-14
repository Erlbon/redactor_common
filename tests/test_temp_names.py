"""core/temp_names: the family's own temp/backup names are recognised, a user's lookalike names are not
(the narrow-matching rule), and an app can register its own. Cases from videoredactor's
tests/test_temp_names.py, which this module was promoted from, plus the other apps' markers."""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from redactor_common.core import temp_names  # noqa: E402
from redactor_common.core.folder_discovery import find_by_extension  # noqa: E402
from redactor_common.core.temp_names import is_app_temp_name, register  # noqa: E402

TEMP_NAMES = [
    # videoredactor's cases
    "Movie.repairing.mp4",
    "Movie.repairing.mkv",
    "Movie.partial.mp4",
    "Movie.partial.m4v",
    "Movie.redact-orig.mkv",
    "Movie.redact-orig2.mp4",
    "Movie.redact-orig13.mp4",
    ".Movie.redact-1a2b3c4d.mp4",
    ".Movie.redact-1a2b3c4d.partial.mkv",
    "Show.S01E01.partial.mp4",
    "MOVIE.REPAIRING.MP4",
    "Movie.Redact-ORIG.mkv",
    ".Movie.REDACT-ABCDEF01.MKV",
    # videoredactor's library_safety temp names, now listed too
    "Movie.redact-part.mkv",
    "Movie.redact-resize.mp4",
    # the rest of the family
    "Book.redact-orig.epub",
    "Comic.redact-orig3.cbz",
    "Song.redact-orig.mp3",
    ".mp3redactor-redact-k2j3h4.mp3",      # mp3redactor's REDACT_SCRATCH_PREFIX scratch
    ".mp3redactor-abc123.tmp",             # mp3redactor tag_writer scratch
    ".videoredactor-abc123.tmp",           # videoredactor mp4_backend scratch
    ".1a2b3c4d.part",                      # core/move_plan verified cross-volume copy
]

CASE_VARIANTS = {"MOVIE.REPAIRING.MP4", "Movie.Redact-ORIG.mkv", ".Movie.REDACT-ABCDEF01.MKV"}

LEGIT_NAMES = [
    "My.Partial.Cut.mkv",
    "Partial.mkv",
    "Repairing The Roof.mp4",
    "Movie.repairing.part2.mp4",
    "The.Partial.Truth.2020.mp4",
    "redact-orig.mp4",
    "Movie.redact-original.mp4",
    "Movie.redact-orig-cut.mkv",
    ".hidden-but-legit.mp4",
    ".Movie.redact-zzzzzzzz.mp4",   # not 8 hex digits
    ".Movie.redact-1a2b3c.mp4",     # too short
    "Movie.mp4",
    "mp3redactor-notes.mp3",        # the scratch prefix needs its leading dot
    ".mp3redactor.mp3",             # ... and the dash
    "Part.mkv",
    "Song.Part.One.mp3",
    "Kill.Bill.Part.mkv",           # a real name: the conversion markers are redact-part / redact-resize
    "Movie.part.mkv",
    "Movie.resize.mp4",
    "1a2b3c4d.part",                # the move copy is hidden
    ".1a2b3c4d.parts",
]


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    monkeypatch.setattr(temp_names, "_registered", [])
    monkeypatch.setattr(temp_names, "_registered_keys", set())


@pytest.mark.parametrize("name", TEMP_NAMES)
def test_family_temp_names_recognised(name):
    assert is_app_temp_name(name)


@pytest.mark.parametrize("name", LEGIT_NAMES)
def test_lookalike_user_names_not_recognised(name):
    assert not is_app_temp_name(name)


def test_matches_the_backup_name_commit_in_place_writes(tmp_path):
    from redactor_common.core.pipeline import _backup_path

    original = tmp_path / "Song.mp3"
    original.write_bytes(b"x")
    first = _backup_path(str(original))
    assert is_app_temp_name(os.path.basename(first))
    open(first, "wb").close()
    assert is_app_temp_name(os.path.basename(_backup_path(str(original))))  # the numbered one too


def test_register_string_pattern_matches_whole_name_ignoring_case():
    assert not is_app_temp_name("Comic.cbz.tmp_write")
    register(r".+\.tmp_(?:write|pages|clean|resize)")
    assert is_app_temp_name("Comic.cbz.tmp_write")
    assert is_app_temp_name("COMIC.CBZ.TMP_PAGES")
    assert not is_app_temp_name("Comic.cbz.tmp_writer")  # whole name, not a prefix match
    assert not is_app_temp_name("Comic.cbz")


def test_register_compiled_pattern_callable_and_list():
    register([re.compile(r"\.scratch-\d+\.epub"), lambda name: name.startswith("~$")])
    assert is_app_temp_name(".scratch-42.epub")
    assert not is_app_temp_name(".SCRATCH-42.epub")  # a compiled pattern is used with its own flags
    assert is_app_temp_name("~$Book.epub")
    assert not is_app_temp_name("Book.epub")


def test_register_is_idempotent_and_rejects_non_patterns():
    register(r"x\.tmp")
    register(r"x\.tmp")
    assert len(temp_names._registered) == 1
    with pytest.raises(TypeError):
        register([42])


def _touch(root, rel):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


VIDEO_EXTS = {".mp4", ".m4v", ".mkv"}


def test_folder_search_skips_every_temp_pattern_and_keeps_lookalikes(tmp_path):
    # The upper-case variants would collide with their lower-case twins on a case-insensitive
    # filesystem; they are covered above.
    temp = [n for n in TEMP_NAMES if n not in CASE_VARIANTS and os.path.splitext(n)[1].lower() in VIDEO_EXTS]
    legit = [n for n in LEGIT_NAMES if os.path.splitext(n)[1].lower() in VIDEO_EXTS]
    for name in temp + legit:
        _touch(tmp_path, name)
    ignored = []
    found = find_by_extension([tmp_path], VIDEO_EXTS, recursive=False, skip=is_app_temp_name, ignored=ignored)
    assert {os.path.basename(p) for p in found} == set(legit)
    assert {os.path.basename(p) for p in ignored} == set(temp)


def test_nested_folders_filtered_only_when_recursive(tmp_path):
    _touch(tmp_path, "a/b/Real.mkv")
    _touch(tmp_path, "a/b/Real.repairing.mkv")
    _touch(tmp_path, "a/.Real.redact-00ff00ff.mp4")
    _touch(tmp_path, "a/Real.redact-orig2.mkv")
    ignored = []
    found = find_by_extension([tmp_path], VIDEO_EXTS, recursive=True, skip=is_app_temp_name, ignored=ignored)
    assert [os.path.basename(p) for p in found] == ["Real.mkv"]
    assert len(ignored) == 3
    ignored = []
    assert find_by_extension([tmp_path], VIDEO_EXTS, recursive=False, skip=is_app_temp_name, ignored=ignored) == []
    assert ignored == []


def test_non_matching_extension_temp_lookalike_is_not_counted_as_ignored(tmp_path):
    _touch(tmp_path, "notes.partial.txt")
    ignored = []
    assert find_by_extension([tmp_path], VIDEO_EXTS, skip=is_app_temp_name, ignored=ignored) == []
    assert ignored == []

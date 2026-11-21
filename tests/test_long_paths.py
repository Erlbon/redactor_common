"""Names that end in a dot ('R.E.M.'): core/long_paths.py, and the note reader and writer (core/notes.py) using it. On Windows
an ordinary path to such a folder drops the dot and finds nothing; the notes code now reads and writes there, and gives
plain paths back."""

import os

import pytest

from redactor_common.core.long_paths import LONG_PREFIX, literal, needs_literal
from redactor_common.core.notes import (
    STATE_FOREIGN, STATUS_EXISTS, STATUS_WRITTEN, existing_note, note_status, write_note,
)

windows_only = pytest.mark.skipif(os.name != "nt", reason="only Windows drops the trailing dot")


def make_dot_folder(tmp_path):
    folder = tmp_path / "R.E.M."
    os.makedirs(literal(folder))
    return folder


@windows_only
def test_literal_only_where_a_name_ends_in_a_dot_or_space(tmp_path):
    assert literal(tmp_path / "R.E.M.") == LONG_PREFIX + str(tmp_path / "R.E.M.")
    assert literal(tmp_path / "Ultravox") == str(tmp_path / "Ultravox") and not needs_literal(tmp_path / "Ultravox")
    assert needs_literal(tmp_path / "Trailing space ")
    assert literal("\\\\server\\share\\R.E.M.") == LONG_PREFIX + "UNC\\server\\share\\R.E.M."
    assert not needs_literal("C:\\a\\..\\b") and literal(LONG_PREFIX + "C:\\x.") == LONG_PREFIX + "C:\\x."


@windows_only
def test_a_note_is_written_found_and_read_in_a_folder_whose_name_ends_in_a_dot(tmp_path):
    folder = make_dot_folder(tmp_path)
    result = write_note(folder, "artist", "# R.E.M.\n\nAthens, Georgia.\n")
    assert result.status == STATUS_WRITTEN, result.message
    assert result.path == os.path.join(str(folder), "artist.md")  # a plain path, the dot kept
    assert os.path.isfile(literal(result.path))
    assert existing_note(folder) == result.path
    state = note_status(result.path)
    assert state.kind == STATE_FOREIGN and "Athens" in state.text
    assert write_note(folder, "artist", "# again\n").status == STATUS_EXISTS  # never overwritten
    names = os.listdir(literal(folder))
    assert names == ["artist.md"]  # no temp file left behind

"""mp3redactor's review of 2026-11-27, cards 67 and 68 (library fixes): sanitize_filename never gives back a Windows
device name, and Windows Media Player's albumartsmall.jpg thumbnail is not taken as a folder's cover."""

from redactor_common.core.lookups.cover_finder import find_image_in_folder
from redactor_common.core.rename_pattern import render_filename, sanitize_filename


def test_sanitize_filename_prefixes_device_names():
    assert sanitize_filename("CON") == "_CON"
    assert sanitize_filename("Con") == "_Con"
    assert sanitize_filename("aux.") == "_aux"
    assert sanitize_filename("nul.part1") == "_nul.part1"
    assert sanitize_filename("Console") == "Console"
    assert sanitize_filename("Con Air") == "Con Air"


def test_render_filename_still_prefixes_once():
    assert render_filename({"title": "Aux"}, "%title%") == "_Aux"


def test_albumartsmall_is_not_a_cover(tmp_path):
    (tmp_path / "AlbumArtSmall.jpg").write_bytes(b"x")
    assert find_image_in_folder(tmp_path) is None
    (tmp_path / "AlbumArt.jpg").write_bytes(b"x")
    assert find_image_in_folder(tmp_path).name == "AlbumArt.jpg"

"""core/lookups/cover_finder.py: the album-art image beside the files, found by name (moved from mp3redactor's
core/cover_art.py, 2026-11-04)."""

from redactor_common.core.lookups import cover_finder


def test_find_folder_image_by_name_case_insensitively(tmp_path):
    song = tmp_path / "song.mp3"
    song.write_bytes(b"x")
    assert cover_finder.find_folder_image(song) is None
    (tmp_path / "Folder.JPG").write_bytes(b"jpeg")
    assert cover_finder.find_folder_image(song).name == "Folder.JPG"
    (tmp_path / "cover.png").write_bytes(b"png")
    assert cover_finder.find_folder_image(song).name == "cover.png"  # cover.* wins


def test_a_folder_named_like_an_image_is_not_one(tmp_path):
    (tmp_path / "cover.jpg").mkdir()
    assert cover_finder.find_image_in_folder(tmp_path) is None


def test_a_missing_folder_has_no_image(tmp_path):
    assert cover_finder.find_image_in_folder(tmp_path / "gone") is None

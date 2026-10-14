"""core/folder_discovery.find_by_extension: the one walker behind Open Folder, Refresh List and the convert
offer, covering what each app's copy did (mp3 find_mp3_files / find_importable_files, epub
_find_epubs_in_folder / find_convertible_in_folder, video discover_video_files /
discover_convertible_files, cbz comic_files_in_folder)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from redactor_common.core.folder_discovery import (  # noqa: E402
    extension_labels, find_by_extension, has_extension, normalize_extensions,
)


def touch(root, rel, data=b"x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return str(p)


def names(paths):
    return [os.path.basename(p) for p in paths]


def test_normalize_extensions_adds_dots_lowercases_and_dedupes():
    assert normalize_extensions([".MP3", "flac", " .Wav ", "mp3", "", ".tar.gz"]) == (".mp3", ".flac", ".wav", ".tar.gz")


def test_has_extension_needs_a_name_before_it():
    exts = normalize_extensions(["cbz", "tar.gz"])
    assert has_extension("A.CBZ", exts) and has_extension("x.tar.gz", exts)
    assert not has_extension(".cbz", exts) and not has_extension("a.cbzx", exts)


def test_folder_search_recursive_and_flat(tmp_path):
    touch(tmp_path, "b.mp3")
    touch(tmp_path, "A.MP3")
    touch(tmp_path, "notes.txt")
    touch(tmp_path, "sub/c.mp3")
    touch(tmp_path, "sub/deeper/d.mp3")
    assert names(find_by_extension([tmp_path], [".mp3"])) == ["A.MP3", "b.mp3", "c.mp3", "d.mp3"]
    assert names(find_by_extension([tmp_path], [".mp3"], recursive=False)) == ["A.MP3", "b.mp3"]


def test_results_are_absolute_strings_sorted_by_folder_levels(tmp_path):
    touch(tmp_path, "a b/x.epub")
    touch(tmp_path, "a/x.epub")
    touch(tmp_path, "Z.epub")
    found = find_by_extension([str(tmp_path)], {".epub"})
    assert all(isinstance(p, str) and os.path.isabs(p) for p in found)
    # Like sorting pathlib paths: folder "a" before folder "a b", case-insensitively where the OS is.
    rel = [os.path.relpath(p, tmp_path) for p in found]
    assert rel.index(os.path.join("a", "x.epub")) < rel.index(os.path.join("a b", "x.epub"))
    assert found == sorted(found, key=lambda p: tuple(os.path.normcase(p).split(os.sep)))


def test_files_and_folders_mixed_and_deduplicated(tmp_path):
    a = touch(tmp_path, "a.flac")
    touch(tmp_path, "b.wav")
    touch(tmp_path, "c.mp3")
    variant = a.replace(os.sep, "/") if os.sep != "/" else a
    found = find_by_extension([a, tmp_path, variant, tmp_path / "a.flac"], [".flac", ".wav"])
    assert names(found) == ["a.flac", "b.wav"]


@pytest.mark.skipif(os.path.normcase("A") == "A", reason="case-insensitive paths only")
def test_case_variants_of_one_path_are_one_file(tmp_path):
    a = touch(tmp_path, "a.cbz")
    assert len(find_by_extension([a, a.upper(), tmp_path], [".cbz"])) == 1


def test_a_named_file_needs_a_wanted_extension(tmp_path):
    txt = touch(tmp_path, "readme.txt")
    assert find_by_extension([txt], [".mp3"]) == []


def test_missing_paths_and_unreadable_folders_are_passed_over(tmp_path):
    touch(tmp_path, "ok.mkv")
    missing = tmp_path / "not here"
    assert names(find_by_extension([missing, tmp_path], [".mkv"])) == ["ok.mkv"]
    assert find_by_extension([missing], [".mkv"], recursive=False) == []


def test_a_folder_named_like_a_file_is_not_a_file(tmp_path):
    (tmp_path / "folder.cbz").mkdir()
    touch(tmp_path, "real.cbz")
    assert names(find_by_extension([tmp_path], [".cbz"], recursive=False)) == ["real.cbz"]
    assert names(find_by_extension([tmp_path], [".cbz"], recursive=True)) == ["real.cbz"]


def test_compound_and_several_extensions(tmp_path):
    touch(tmp_path, "a.cbz")
    touch(tmp_path, "b.CBR")
    touch(tmp_path, "c.cb7")
    touch(tmp_path, "d.zip")
    assert names(find_by_extension([tmp_path], ("cbz", ".cbr", ".CB7"))) == ["a.cbz", "b.CBR", "c.cb7"]


def is_scratch(name):
    return name.startswith(".scratch-")


def test_skip_applies_to_folder_finds_and_collects_ignored(tmp_path):
    touch(tmp_path, "a.mp3")
    s = touch(tmp_path, ".scratch-1.mp3")
    touch(tmp_path, ".scratch-2.txt")  # not a wanted extension: not counted as ignored
    ignored = []
    assert names(find_by_extension([tmp_path], [".mp3"], skip=is_scratch, ignored=ignored)) == ["a.mp3"]
    assert ignored == [s]


def test_a_file_named_directly_is_kept_unless_skip_named(tmp_path):
    touch(tmp_path, "a.mp3")
    s = touch(tmp_path, ".scratch-1.mp3")
    # videocli's rule: what the user names in full is what they mean, even after a folder search skipped it
    ignored = []
    assert names(find_by_extension([tmp_path, s], [".mp3"], skip=is_scratch, ignored=ignored)) == [".scratch-1.mp3", "a.mp3"]
    assert ignored == []
    # mp3redactor's old rule: never, even when named
    ignored = []
    found = find_by_extension([s, tmp_path], [".mp3"], skip=is_scratch, skip_named=True, ignored=ignored)
    assert names(found) == ["a.mp3"] and ignored == [s]


def test_resolve_follows_symlinks(tmp_path):
    real = touch(tmp_path, "real/a.mp3")
    link = tmp_path / "link.mp3"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    assert len(find_by_extension([real, link], [".mp3"])) == 2
    assert find_by_extension([real, link], [".mp3"], resolve=True) == [os.path.realpath(real)]


def test_pathlike_inputs(tmp_path):
    touch(tmp_path, "a.epub")
    assert names(find_by_extension([tmp_path], [".epub"])) == names(find_by_extension([str(tmp_path)], [".epub"]))


def test_extension_labels():
    assert extension_labels(["x/a.flac", "b.WAV", "c.flac", "noext"]) == ["FLAC", "WAV"]

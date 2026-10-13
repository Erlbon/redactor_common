"""A11: the Recycle Bin is only trusted on fixed local disks; elsewhere
move_to_trash refuses (file untouched) unless a fallback folder is given."""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from redactor_common.core import trash  # noqa: E402

UNC = chr(92) * 2 + "server" + chr(92) + "share" + chr(92) + "dir" + chr(92) + "a.mkv"


def test_unc_path_is_never_reliable():
    assert trash.trash_is_reliable(UNC, drive_type=lambda p: 3) is False
    assert trash.trash_is_reliable("//server/share/a.mkv", drive_type=lambda p: 3) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows drive types")
def test_windows_fixed_disk_is_reliable_other_drive_types_are_not():
    assert trash.trash_is_reliable("D:/Video/a.mkv", drive_type=lambda p: 3) is True
    assert trash.trash_is_reliable("Z:/Video/a.mkv", drive_type=lambda p: 4) is False  # DRIVE_REMOTE
    assert trash.trash_is_reliable("E:/Video/a.mkv", drive_type=lambda p: 2) is False  # DRIVE_REMOVABLE
    assert trash.trash_is_reliable("Q:/Video/a.mkv", drive_type=lambda p: None) is False


@pytest.mark.skipif(sys.platform == "win32", reason="posix mount-point heuristic")
def test_posix_remote_mounts_are_not_reliable():
    assert trash.trash_is_reliable("/mnt/nas/a.mkv") is False
    assert trash.trash_is_reliable("/home/me/a.mkv") is True


def test_move_to_trash_refuses_on_an_unreliable_drive(tmp_path, monkeypatch):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    monkeypatch.setattr(trash, "trash_is_reliable", lambda p, drive_type=None: False)
    sent = []
    monkeypatch.setitem(sys.modules, "send2trash", types.SimpleNamespace(send2trash=sent.append))
    with pytest.raises(trash.TrashError) as exc:
        trash.move_to_trash(str(f))
    assert "kept" in str(exc.value)
    assert f.exists() and sent == []


def test_move_to_trash_uses_the_fallback_folder_when_given(tmp_path, monkeypatch):
    src = tmp_path / "src" / "Video" / "a.mkv"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"x")
    monkeypatch.setattr(trash, "trash_is_reliable", lambda p, drive_type=None: False)
    fallback = tmp_path / "quarantine"
    trash.move_to_trash(str(src), fallback_folder=str(fallback))
    assert not src.exists()
    moved = [p for p in fallback.rglob("a.mkv")]
    assert len(moved) == 1
    # dated subfolder mirroring the drive-relative path
    rel = moved[0].relative_to(fallback)
    assert len(rel.parts[0]) == 10 and rel.parts[0][4] == "-"
    assert rel.parts[-2:] == ("Video", "a.mkv")


def test_move_to_fallback_folder_never_overwrites(tmp_path):
    folder = tmp_path / "fb"
    a = tmp_path / "a.txt"
    a.write_text("one")
    first = trash.move_to_fallback_folder(str(a), str(folder))
    a.write_text("two")
    second = trash.move_to_fallback_folder(str(a), str(folder))
    assert first != second
    assert open(first).read() == "one" and open(second).read() == "two"


def test_move_to_fallback_folder_requires_a_folder(tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("one")
    with pytest.raises(trash.TrashError):
        trash.move_to_fallback_folder(str(a), "")
    assert a.exists()

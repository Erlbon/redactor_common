"""Audio writer checks, run in their own interpreter by test_audio_write.py (see test_meta_readers.py for why). Exit code 0 = all passed."""
import os
import pathlib
import struct
import sys

import mutagen
from mutagen.id3 import APIC, ID3, TCON, TIT2, TXXX

from redactor_common.core import audio_write as aw

FRAME = bytes([0xFF, 0xFB, 0x90, 0x00]) + bytes(413)


def make_mp3(path, v23=False, **frames):
    path.write_bytes(FRAME * 12)
    if frames or v23:
        tags = ID3()
        for frame in frames.values():
            tags.add(frame)
        tags.save(str(path), v2_version=3 if v23 else 4)
    return str(path)


def make_flac(path):
    info = struct.pack(">HHBBBBBBBBBBBBBBBB", 4096, 4096, 0, 0, 0, 0, 0, 0x0A, 0xC4, 0x40, 0xF0, 0, 0, 0, 0, 0, 0, 0)
    path.write_bytes(b"fLaC" + bytes([0x80, 0, 0, 34]) + info + bytes(34 - len(info)))
    return str(path)


def check_v23_is_kept_and_unknown_frames_stay(tmp):
    path = make_mp3(tmp / "a.mp3", v23=True, t=TIT2(encoding=3, text="Old"), c=APIC(encoding=3, mime="image/png", type=3, desc="", data=b"PNG"),
                    x=TXXX(encoding=3, desc="MINE", text=["keep"]))
    binned = []
    aw.write_audio_tags(path, {"title": "New", "artist": ["A", "B"]}, dispose_original=lambda p: (binned.append(p), os.remove(p)))
    tags = ID3(path)
    assert tags.version[:2] == (2, 3), tags.version
    assert str(tags["TIT2"]) == "New" and tags.getall("APIC")[0].data == b"PNG" and tags.getall("TXXX")[0].text == ["keep"]
    assert aw.read_audio_tags(path)["artist"] == ["A/B"]                            # v2.3: one text per frame
    assert len(binned) == 1 and sorted(os.listdir(tmp)) == ["a.mp3"]


def check_a_bare_mp3_gets_a_v24_tag_with_lists(tmp):
    path = make_mp3(tmp / "b.mp3")
    aw.write_audio_tags(path, {"genre": "Rock", "artist": ["A", "B"]})
    assert ID3(path).version[:2] == (2, 4) and aw.read_audio_tags(path)["artist"] == ["A", "B"]   # lists in, lists out
    aw.write_audio_tags(path, {"genre": ""})
    assert aw.read_audio_tags(path)["genre"] == []


def check_flac_and_a_failed_read_back_leave_the_file(tmp):
    path = make_flac(tmp / "c.flac")
    aw.write_audio_tags(path, {"title": "T", "tracknumber": "2"})
    assert mutagen.File(path, easy=True)["title"] == ["T"]
    before = open(path, "rb").read()

    def broken(_tmp):
        raise aw.AudioWriteError("did not read back")

    try:
        aw.edit_audio_copy(path, lambda t: None, verify=broken)
    except aw.AudioWriteError:
        pass
    else:
        raise AssertionError("verify failure not raised")
    assert open(path, "rb").read() == before and sorted(os.listdir(tmp)) == ["c.flac"]


def check_a_symlink_stays_a_link(tmp):
    real = make_mp3(tmp / "real.mp3")
    link = tmp / "link.mp3"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        return  # no right to make links here (Windows without developer mode)
    aw.write_audio_tags(str(link), {"title": "Through"})
    assert os.path.islink(link) and str(ID3(real)["TIT2"]) == "Through"


def check_save_tags_keeps_the_version_of_a_bare_id3(tmp):
    path = make_mp3(tmp / "d.mp3", v23=True, t=TIT2(encoding=3, text="x"))
    tags = ID3(path)
    tags.add(TCON(encoding=3, text="Jazz"))
    aw.save_tags(tags)
    assert ID3(path).version[:2] == (2, 3)


def main(tmp_root):
    root = pathlib.Path(tmp_root)
    failed = 0
    for name, check in sorted(globals().items()):
        if name.startswith("check_") and callable(check):
            tmp = root / name
            tmp.mkdir()
            try:
                check(tmp)
                print("ok  ", name)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print("FAIL", name, type(exc).__name__, exc)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))

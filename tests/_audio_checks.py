"""Audio reader checks, run in their own interpreter by test_meta_readers.py. Exit code 0 = all passed."""
import pathlib
import sys

from redactor_common.core.meta_audio import read_audio

FRAME = bytes([0xFF, 0xFB, 0x90, 0x00]) + bytes(413)          # MPEG-1 layer III, 128 kbps, 44.1 kHz: 417 bytes


def make_mp3(path, tagged=True):
    path.write_bytes(FRAME * 40)
    if tagged:
        from mutagen.id3 import APIC, COMM, ID3, TALB, TCON, TIT2, TPE1, TRCK, USLT
        tags = ID3()
        tags.add(TIT2(encoding=3, text="Blue Monday"))
        tags.add(TPE1(encoding=3, text=["New Order"]))
        tags.add(TALB(encoding=3, text="Power, Corruption & Lies"))
        tags.add(TRCK(encoding=3, text="5/8"))
        tags.add(TCON(encoding=3, text="Synthpop"))
        tags.add(COMM(encoding=3, lang="eng", desc="", text="a comment"))
        tags.add(USLT(encoding=3, lang="eng", desc="", text="How does it feel"))
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=bytes([0xFF, 0xD8, 0xFF])))
        tags.save(str(path))


def check_an_mp3_gives_its_tags_cover_and_technical_facts(tmp_path):
    p = tmp_path / "a.mp3"
    make_mp3(p)
    r = read_audio(str(p))
    assert not r.error
    assert (r.get("title"), r.get("artist"), r.get("album"), r.get("track"), r.get("genre")) == (
        "Blue Monday", "New Order", "Power, Corruption & Lies", "5/8", "Synthpop")
    assert r.get("comment") == "a comment" and r.get("lyrics") == "How does it feel" and "1 picture" in r.get("cover")
    assert r.get("bitrate").startswith("128 kbps") and r.get("samplerate") == "44.1 kHz" and r.get("channels")
    assert [f.group for f in r.fields if f.key == "title"] == ["Tags"] and "Technical" in r.groups()


def check_an_mp3_without_tags_still_gives_the_technical_facts(tmp_path):
    p = tmp_path / "b.mp3"
    make_mp3(p, tagged=False)
    r = read_audio(str(p))
    assert not r.error and r.get("title") == "" and r.get("length")


def check_a_file_that_is_not_audio_is_an_error_not_a_crash(tmp_path):
    p = tmp_path / "x.mp3"
    p.write_bytes(b"this is not audio at all" * 10)
    assert read_audio(str(p)).error
    assert read_audio(str(tmp_path / "missing.mp3")).error


def check_reading_never_changes_the_file(tmp_path):
    p = tmp_path / "a.mp3"
    make_mp3(p)
    before = p.read_bytes()
    read_audio(str(p))
    assert p.read_bytes() == before




if __name__ == "__main__":
    base = pathlib.Path(sys.argv[1])
    for name, fn in sorted(globals().items()):
        if name.startswith("check_"):
            sub = base / name
            sub.mkdir()
            fn(sub)
            print("ok", name)

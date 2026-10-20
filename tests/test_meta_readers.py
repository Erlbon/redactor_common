"""The read-only metadata readers: audio tags, ComicInfo in a CBZ, video through ffprobe."""
import json
import subprocess
import zipfile

import pytest

from redactor_common.core.meta_comic import read_comic
from redactor_common.core.meta_fields import MetaResult, add, duration_text
from redactor_common.core.meta_video import parse_probe, read_video


def test_add_leaves_out_empty_values_and_joins_lists():
    fields: list = []
    add(fields, "a", "A", "")
    add(fields, "b", "B", None)
    add(fields, "c", "C", ["x", " ", "y"])
    add(fields, "d", "D", 0)
    assert [(f.key, f.value) for f in fields] == [("c", "x, y"), ("d", "0")]


def test_duration_text():
    assert duration_text(187) == "3:07" and duration_text(3729) == "1:02:09" and duration_text("x") == "" and duration_text(-1) == ""


# audio ------------------------------------------------------------------------------------------------------------
# These run in a separate Python (tests/_audio_checks.py): with mutagen's tests inside the one pytest process that also holds
# the Qt tests, the interpreter crashed while shutting down (after all tests had passed) and the pre-push hook failed.
import os  # noqa: E402
import sys  # noqa: E402


def test_audio_reader_checks_in_a_clean_interpreter(tmp_path):
    try:
        import mutagen  # noqa: F401
    except ImportError:
        pytest.skip("mutagen is not installed")
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))                   # the parent of the checkout: import redactor_common resolves here
    env = dict(os.environ, PYTHONPATH=root + os.pathsep + os.environ.get("PYTHONPATH", ""))
    done = subprocess.run([sys.executable, os.path.join(here, "_audio_checks.py"), str(tmp_path)], capture_output=True,
                          text=True, env=env, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr


# comic ------------------------------------------------------------------------------------------------------------
INFO = """<?xml version="1.0"?><ComicInfo><Series>Blueberry</Series><Number>3</Number><Title>Chihuahua Pearl</Title>
<Year>1973</Year><Month>4</Month><Writer>Charlier</Writer><Penciller>Giraud</Penciller><Publisher>Dargaud</Publisher>
<Genre>Western</Genre><PageCount>48</PageCount><Summary>Mike Blueberry.</Summary></ComicInfo>"""


def make_cbz(path, info=INFO, folder=""):
    with zipfile.ZipFile(path, "w") as z:
        if info is not None:
            z.writestr(folder + "ComicInfo.xml", info)
        for i in range(3):
            z.writestr(f"{i:03}.jpg", bytes([0xFF, 0xD8]) + bytes(10))


def test_a_cbz_gives_comicinfo_credits_and_page_facts(tmp_path):
    p = tmp_path / "a.cbz"
    make_cbz(p)
    r = read_comic(str(p))
    assert not r.error
    assert (r.get("series"), r.get("number"), r.get("title"), r.get("date")) == ("Blueberry", "3", "Chihuahua Pearl", "1973-04")
    assert r.get("writer") == "Charlier" and r.get("penciller") == "Giraud" and r.get("publisher") == "Dargaud"
    assert r.get("pages") == "3" and r.get("pagecount") == "48"
    assert [f.group for f in r.fields if f.key == "writer"] == ["Credits"]


def test_comicinfo_in_a_subfolder_is_found(tmp_path):
    p = tmp_path / "a.cbz"
    make_cbz(p, folder="Sub/")
    assert read_comic(str(p)).get("series") == "Blueberry"


def test_a_cbz_without_comicinfo_says_so_and_still_counts_pages(tmp_path):
    p = tmp_path / "a.cbz"
    make_cbz(p, info=None)
    r = read_comic(str(p))
    assert r.error == "no ComicInfo.xml in this archive" and r.get("pages") == "3"


def test_bad_zip_bad_xml_and_oversized_xml_are_errors_not_crashes(tmp_path):
    bad = tmp_path / "bad.cbz"
    bad.write_bytes(b"not a zip")
    assert "not a zip" in read_comic(str(bad)).error
    broken = tmp_path / "broken.cbz"
    make_cbz(broken, info="<ComicInfo><Series>")
    assert "not valid XML" in read_comic(str(broken)).error
    huge = tmp_path / "huge.cbz"
    make_cbz(huge, info="<ComicInfo>" + "x" * (3 * 1024 * 1024) + "</ComicInfo>")
    assert "too large" in read_comic(str(huge)).error


# video ------------------------------------------------------------------------------------------------------------
PROBE = {"format": {"format_long_name": "Matroska / WebM", "duration": "5400.5", "bit_rate": "4500000", "size": "3037500000",
                    "tags": {"TITLE": "Casablanca", "ENCODER": "libmatroska"}},
         "chapters": [{}, {}, {}],
         "streams": [{"codec_type": "video", "codec_long_name": "H.264", "profile": "High", "width": 1920, "height": 1080,
                      "avg_frame_rate": "24000/1001", "pix_fmt": "yuv420p", "bit_rate": "4000000"},
                     {"codec_type": "audio", "codec_name": "ac3", "channel_layout": "5.1", "sample_rate": "48000",
                      "tags": {"language": "eng", "title": "Main"}, "disposition": {"default": 1}},
                     {"codec_type": "audio", "codec_name": "aac", "channels": 2, "tags": {"language": "nor"}},
                     {"codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "und"}, "disposition": {"forced": 1}}]}


def test_probe_json_becomes_container_tags_and_tracks():
    r = parse_probe(PROBE)
    assert r.get("container") == "Matroska / WebM" and r.get("length") == "1:30:00" and r.get("chapters") == "3"
    assert r.get("title") == "Casablanca" and r.get("bitrate") == "4500 kbps"
    assert r.get("video.1.size") == "1920 × 1080" and r.get("video.1.fps") == "23.976 fps"
    assert r.get("audio.1.channels") == "5.1" and r.get("audio.1.language") == "eng" and r.get("audio.1.flags") == "default"
    assert r.get("audio.2.language") == "nor" and r.get("subtitle.1.flags") == "forced" and r.get("subtitle.1.language") == ""
    assert "Audio 1" in r.groups() and "Audio 2" in r.groups() and "Video" in r.groups()


def test_read_video_runs_ffprobe_and_survives_every_failure(tmp_path):
    p = tmp_path / "m.mkv"
    p.write_bytes(b"x")
    seen = []

    def ok(args, timeout=None):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(PROBE), "")

    r = read_video(str(p), ffprobe="ffprobe", run=ok)
    assert not r.error and r.get("title") == "Casablanca" and seen[0][0] == "ffprobe" and seen[0][-1] == str(p)
    assert "-show_streams" in seen[0] and not any(a in seen[0] for a in ("-i", "-y"))      # a read, never a write

    def fails(args, timeout=None):
        return subprocess.CompletedProcess(args, 1, "", "first line\nInvalid data found")

    assert "Invalid data" in read_video(str(p), ffprobe="ffprobe", run=fails).error

    def missing(args, timeout=None):
        raise FileNotFoundError()

    assert "not found" in read_video(str(p), ffprobe="ffprobe", run=missing).error

    def slow(args, timeout=None):
        raise subprocess.TimeoutExpired(args, 1)

    assert read_video(str(p), ffprobe="ffprobe", run=slow).error
    assert read_video(str(tmp_path / "gone.mkv"), ffprobe="ffprobe", run=ok).error == "the file is not there"
    garbage = read_video(str(p), ffprobe="ffprobe", run=lambda a, timeout=None: subprocess.CompletedProcess(a, 0, "not json", ""))
    assert isinstance(garbage, MetaResult) and garbage.error

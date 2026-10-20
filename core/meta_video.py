"""
redactor_common/core/meta_video.py

Read-only facts of a video file (MP4, MKV, AVI, WebM and the rest ffprobe
knows): the container, its tags, every video, audio and subtitle track, and
the chapters. It asks ``ffprobe`` for JSON; nothing is written.

``read_video(path, ffprobe=None, run=None)``: ``ffprobe`` is the program to
use (found with ``tool_locator`` when not given), ``run`` replaces the
subprocess call in tests and receives the argument list.
"""
from __future__ import annotations

import json
import os

from .meta_fields import GROUP_CONTAINER, GROUP_TAGS, MetaResult, add, duration_text

PROBE_ARGS = ("-v", "error", "-show_format", "-show_streams", "-show_chapters", "-of", "json")
TIMEOUT = 30
KINDS = {"video": "Video", "audio": "Audio", "subtitle": "Subtitles"}
FORMAT_TAGS = (("title", "Title"), ("artist", "Artist"), ("album", "Album"), ("date", "Date"), ("comment", "Comment"),
               ("description", "Description"), ("genre", "Genre"), ("encoder", "Encoder"), ("creation_time", "Created"))


def _fps(text: str) -> str:
    try:
        a, _, b = text.partition("/")
        value = float(a) / float(b or 1)
    except (ValueError, ZeroDivisionError):
        return ""
    return f"{value:.3f}".rstrip("0").rstrip(".") if value else ""


def _kbps(value) -> str:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return ""
    return f"{round(n / 1000)} kbps" if n > 0 else ""


def parse_probe(data: dict) -> MetaResult:
    """Turn ffprobe's JSON into fields."""
    result = MetaResult("video")
    f = result.fields
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    add(f, "container", "Container", fmt.get("format_long_name") or fmt.get("format_name"), GROUP_CONTAINER)
    add(f, "length", "Length", duration_text(fmt.get("duration")), GROUP_CONTAINER)
    add(f, "bitrate", "Overall bitrate", _kbps(fmt.get("bit_rate")), GROUP_CONTAINER)
    try:
        add(f, "size", "File size", f"{int(fmt.get('size')):,} bytes", GROUP_CONTAINER)
    except (TypeError, ValueError):
        pass
    tags = {str(k).lower(): v for k, v in (fmt.get("tags") or {}).items()}
    for key, label in FORMAT_TAGS:
        add(f, key, label, tags.get(key), GROUP_TAGS)
    chapters = data.get("chapters") or []
    add(f, "chapters", "Chapters", len(chapters) or None, GROUP_CONTAINER)
    counts: dict = {}
    for s in streams:
        kind = s.get("codec_type")
        if kind not in KINDS:
            continue
        counts[kind] = counts.get(kind, 0) + 1
        n = counts[kind]
        group = f"{KINDS[kind]} {n}" if sum(1 for x in streams if x.get("codec_type") == kind) > 1 else KINDS[kind]
        pre = f"{kind}.{n}."
        t = {str(k).lower(): v for k, v in (s.get("tags") or {}).items()}
        add(f, pre + "codec", "Codec", s.get("codec_long_name") or s.get("codec_name"), group)
        add(f, pre + "profile", "Profile", s.get("profile"), group)
        if kind == "video":
            if s.get("width") and s.get("height"):
                add(f, pre + "size", "Picture size", f"{s['width']} × {s['height']}", group)
            add(f, pre + "fps", "Frame rate", _fps(str(s.get("avg_frame_rate") or "")) and
                _fps(str(s.get("avg_frame_rate"))) + " fps", group)
            add(f, pre + "pixfmt", "Pixel format", s.get("pix_fmt"), group)
            add(f, pre + "hdr", "HDR", "yes" if s.get("color_transfer") in ("smpte2084", "arib-std-b67") else None, group)
        if kind == "audio":
            add(f, pre + "channels", "Channels", s.get("channel_layout") or s.get("channels"), group)
            if s.get("sample_rate"):
                add(f, pre + "rate", "Sample rate", f"{int(s['sample_rate']) / 1000:g} kHz", group)
        add(f, pre + "bitrate", "Bitrate", _kbps(s.get("bit_rate")), group)
        add(f, pre + "language", "Language", t.get("language") if t.get("language") not in (None, "und") else None, group)
        add(f, pre + "title", "Title", t.get("title"), group)
        disp = s.get("disposition") or {}
        flags = [name for name in ("default", "forced") if disp.get(name)]
        add(f, pre + "flags", "Flags", flags, group)
    if not streams and not fmt:
        result.error = "ffprobe found nothing in this file"
    return result


def read_video(path: str, ffprobe=None, run=None) -> MetaResult:
    """Container, tracks and tags of the video at ``path``. Never raises."""
    if not os.path.isfile(path):
        return MetaResult("video", error="the file is not there")
    if run is None:
        from .subprocess_utils import run_tool
        run = run_tool
    if ffprobe is None:
        from .tool_locator import find_tool
        found = find_tool("ffprobe.exe")
        ffprobe = str(found) if found else ""
    if not ffprobe:
        return MetaResult("video", error="ffprobe was not found, so video details cannot be read")
    try:
        done = run([ffprobe, *PROBE_ARGS, path], timeout=TIMEOUT)
        if done.returncode != 0:
            return MetaResult("video", error=(done.stderr or "").strip().splitlines()[-1:][0] if (done.stderr or "").strip()
                              else f"ffprobe failed (code {done.returncode})")
        return parse_probe(json.loads(done.stdout or "{}"))
    except FileNotFoundError:
        return MetaResult("video", error="ffprobe was not found, so video details cannot be read")
    except Exception as e:   # noqa: BLE001 -- a timeout, bad JSON or any failure is a per-file error, never a crash
        return MetaResult("video", error=f"could not read the video: {e}")

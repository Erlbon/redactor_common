"""
redactor_common/core/meta_audio.py

Read-only tags and technical facts of an audio file (MP3, FLAC, Ogg Vorbis
and Opus, M4A/MP4, WAV and anything else mutagen knows). Nothing here ever
writes to the file.

mutagen is imported when a file is read and is optional: without it the
result is an ``error`` saying so, so importing this module never fails.
ID3 files (MP3 and friends) are read frame by frame, since the "easy" mapping
leaves out comments, lyrics and cover art; every other format goes through
mutagen's easy tag names, with the cover art looked for in its own place.
"""
from __future__ import annotations

from .meta_fields import GROUP_TAGS, GROUP_TECHNICAL, MetaResult, add, duration_text

ID3_TEXT = (("TIT2", "title", "Title"), ("TPE1", "artist", "Artist"), ("TALB", "album", "Album"),
            ("TPE2", "albumartist", "Album artist"), ("TRCK", "track", "Track"), ("TPOS", "disc", "Disc"),
            ("TDRC", "date", "Date"), ("TCON", "genre", "Genre"), ("TCOM", "composer", "Composer"),
            ("TPUB", "publisher", "Publisher"), ("TBPM", "bpm", "BPM"), ("TKEY", "key", "Key"),
            ("TSRC", "isrc", "ISRC"), ("TCOP", "copyright", "Copyright"), ("TENC", "encoded_by", "Encoded by"))
EASY_TEXT = (("title", "Title"), ("artist", "Artist"), ("album", "Album"), ("albumartist", "Album artist"),
             ("tracknumber", "Track"), ("discnumber", "Disc"), ("date", "Date"), ("genre", "Genre"),
             ("composer", "Composer"), ("organization", "Publisher"), ("bpm", "BPM"), ("copyright", "Copyright"),
             ("isrc", "ISRC"), ("comment", "Comment"), ("lyrics", "Lyrics"))
SNIPPET = 200


def _short(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= SNIPPET else text[:SNIPPET - 1] + "…"


def _id3_fields(tags, fields: list) -> None:
    for frame_id, key, label in ID3_TEXT:
        frame = tags.get(frame_id)
        if frame is not None:
            add(fields, key, label, [str(t) for t in getattr(frame, "text", [])])
    for frame in tags.getall("COMM"):
        if not str(getattr(frame, "desc", "")).strip():              # a comment with a description is usually software's own
            add(fields, "comment", "Comment", _short(" ".join(frame.text)))
            break
    for frame in tags.getall("USLT"):
        add(fields, "lyrics", "Lyrics", _short(frame.text))
        break
    for frame in tags.getall("TXXX"):
        desc = str(frame.desc).strip()
        if desc.upper().startswith("REPLAYGAIN_TRACK_GAIN"):
            add(fields, "replaygain", "ReplayGain", " ".join(str(t) for t in frame.text))
    pics = tags.getall("APIC")
    if pics:
        add(fields, "cover", "Cover art", f"{len(pics)} picture{'s' if len(pics) != 1 else ''}, {pics[0].mime}")


def _easy_fields(path: str, audio, fields: list) -> None:
    import mutagen
    easy = mutagen.File(path, easy=True)
    tags = easy.tags if easy is not None and easy.tags is not None else {}
    for key, label in EASY_TEXT:
        try:
            values = tags.get(key)
        except (KeyError, ValueError):
            values = None
        if values:
            add(fields, key, label, [_short(v) for v in values] if key in ("comment", "lyrics") else list(values))
    count = len(getattr(audio, "pictures", []) or [])
    if not count and audio is not None and audio.tags is not None and hasattr(audio.tags, "get"):
        try:
            count = len(audio.tags.get("covr") or [])                  # MP4 cover atom
        except (KeyError, ValueError):
            count = 0
    if count:
        add(fields, "cover", "Cover art", f"{count} picture{'s' if count != 1 else ''}")


def _technical(audio, fields: list, size: int) -> None:
    info = getattr(audio, "info", None)
    if info is None:
        return
    add(fields, "length", "Length", duration_text(getattr(info, "length", None)), GROUP_TECHNICAL)
    bitrate = getattr(info, "bitrate", 0) or 0
    if bitrate:
        mode = getattr(getattr(info, "bitrate_mode", None), "name", "")
        add(fields, "bitrate", "Bitrate", f"{round(bitrate / 1000)} kbps" + (f" ({mode})" if mode and mode != "UNKNOWN" else ""),
            GROUP_TECHNICAL)
    rate = getattr(info, "sample_rate", 0) or 0
    if rate:
        add(fields, "samplerate", "Sample rate", f"{rate / 1000:g} kHz", GROUP_TECHNICAL)
    channels = getattr(info, "channels", 0) or 0
    if channels:
        add(fields, "channels", "Channels", {1: "mono", 2: "stereo"}.get(channels, f"{channels} channels"), GROUP_TECHNICAL)
    bits = getattr(info, "bits_per_sample", 0) or 0
    if bits:
        add(fields, "bits", "Bits per sample", bits, GROUP_TECHNICAL)
    mime = (getattr(audio, "mime", None) or [""])[0]
    add(fields, "codec", "Format", type(info).__module__.rsplit(".", 1)[-1].upper() + (f" ({mime})" if mime else ""),
        GROUP_TECHNICAL)
    add(fields, "size", "File size", f"{size:,} bytes", GROUP_TECHNICAL)


def read_audio(path: str) -> MetaResult:
    """Tags and technical facts of the audio file at ``path``. Never raises."""
    result = MetaResult("audio")
    try:
        import mutagen
    except ImportError:
        result.error = "mutagen is not installed, so audio tags cannot be read"
        return result
    try:
        import os
        size = os.path.getsize(path)
        audio = mutagen.File(path)
        if audio is None:
            result.error = "not an audio file mutagen recognises"
            return result
        tags = getattr(audio, "tags", None)
        if tags is not None and hasattr(tags, "getall"):               # ID3
            _id3_fields(tags, result.fields)
        elif tags is not None:
            _easy_fields(path, audio, result.fields)
        _technical(audio, result.fields, size)
    except Exception as e:   # noqa: BLE001 -- any failure to parse is a per-file error, never a crash
        result.fields = []
        result.error = f"could not read the tags: {e}"
    return result

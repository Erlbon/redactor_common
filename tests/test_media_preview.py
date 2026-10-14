"""gui/media_preview.py: the pure helpers. The window itself (QMediaPlayer, FFmpeg backend) is not built here:
its shutdown crashed the whole pytest process about one run in three, so it is checked by hand instead."""

from redactor_common.gui import media_preview as mp



def test_format_time():
    assert mp.format_time(0) == "0:00"
    assert mp.format_time(65_000) == "1:05"
    assert mp.format_time(3_600_000) == "1:00:00"
    assert mp.format_time(-5) == "0:00"


def test_track_label():
    assert mp.track_label(0, "English", "") == "1 - English"
    assert mp.track_label(1, "", "") == "2"
    assert mp.track_label(2, "Norwegian", "Commentary") == "3 - Norwegian - Commentary"


def test_show_media_preview_accepts_a_pathlib_path(tmp_path, monkeypatch):
    """mp3redactor passes a Path; QUrl.fromLocalFile() takes only str, so the 2026-10-14#08 Preview crashed with a TypeError."""
    from pathlib import Path

    seen = {}

    class _Stub:
        def __init__(self, path, video=False, parent=None):
            seen["path"] = path

        def show(self):
            pass

        def __getattr__(self, name):  # destroyed.connect(...) etc.
            return _Stub.__dict__.get(name) or _Noop()

    class _Noop:
        def connect(self, *_a):
            pass

    monkeypatch.setattr(mp, "multimedia_available", lambda: True)
    monkeypatch.setattr(mp, "MediaPreviewDialog", _Stub)
    song = tmp_path / "a.mp3"
    song.write_bytes(b"x")
    mp.show_media_preview(None, Path(song))
    assert isinstance(seen["path"], str) and seen["path"] == str(song)

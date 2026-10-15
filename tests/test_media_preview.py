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
        def __init__(self, path, video=False, parent=None, autoplay=False):
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
    monkeypatch.setattr(mp, "_open_windows", [])
    song = tmp_path / "a.mp3"
    song.write_bytes(b"x")
    mp.show_media_preview(None, Path(song))
    assert isinstance(seen["path"], str) and seen["path"] == str(song)


def test_opening_a_second_player_closes_the_first(tmp_path, monkeypatch):
    """Only one player window at a time; the new one opens where the old one was."""
    events = []

    class _Rect:
        def __init__(self, tag):
            self.tag = tag

    class _Noop:
        def connect(self, *_a):
            pass

    class _Stub:
        count = 0

        def __init__(self, path, video=False, parent=None, autoplay=False):
            _Stub.count += 1
            self.n = _Stub.count
            self.destroyed = _Noop()

        def geometry(self):
            return _Rect(f"geometry of {self.n}")

        def setGeometry(self, rect):
            events.append(("setGeometry", self.n, rect.tag))

        def close(self):
            events.append(("close", self.n))

        def show(self):
            events.append(("show", self.n))

    monkeypatch.setattr(mp, "multimedia_available", lambda: True)
    monkeypatch.setattr(mp, "MediaPreviewDialog", _Stub)
    monkeypatch.setattr(mp, "_open_windows", [])
    song = tmp_path / "a.mp3"
    song.write_bytes(b"x")
    mp.show_media_preview(None, song)
    mp.show_media_preview(None, song)
    assert events == [("show", 1), ("close", 1), ("setGeometry", 2, "geometry of 1"), ("show", 2)]
    assert len(mp._open_windows) == 1 and mp._open_windows[0].n == 2

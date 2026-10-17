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


# --- the audio-track selector (2026-10-17): the logic that needs no live QMediaPlayer ---------------------------


def test_audio_track_label_shapes():
    assert mp.audio_track_label(0, "AAC", "eng") == "1 - AAC - eng"
    assert mp.audio_track_label(1, "AC3", "und") == "2 - AC3 - und (no language)"
    assert mp.audio_track_label(1, "AC3", "") == "2 - AC3 - und (no language)"
    assert mp.audio_track_label(0, "AAC", "AnyLanguage") == "1 - AAC - und (no language)"
    assert mp.audio_track_label(0, "AAC", "eng", "Commentary") == "1 - AAC - eng - Commentary"
    assert mp.audio_track_label(2, "", "nor") == "3 - nor"


def test_track_hints_accept_dicts_and_tuples_and_ignore_junk():
    hints = mp.normalize_track_labels({
        0: {"language": "eng", "title": "Main", "codec": "aac"}, "1": ("nor", "Norsk"), 2: ("swe", "", "ac3"),
        3: "junk", "x": ("a", "b"), 4: None,
    })
    assert hints == {0: ("eng", "Main", "aac"), 1: ("nor", "Norsk", ""), 2: ("swe", "", "ac3")}
    assert mp.normalize_track_labels(None) == {}


def test_selector_is_hidden_for_zero_or_one_track_and_shown_for_more():
    assert [mp.show_audio_selector(n) for n in (0, 1, 2, 5)] == [False, False, True, True]


def test_the_a_key_cycles_and_wraps():
    assert [mp.next_audio_track(i, 3) for i in (0, 1, 2)] == [1, 2, 0]
    assert mp.next_audio_track(0, 1) == 0 and mp.next_audio_track(-1, 2) == 1


class _FakePlayer:
    """A player whose backend either keeps position and state on a track switch, or restarts and pauses."""

    def __init__(self, position, playing, resets):
        self._pos, self.playing, self.resets, self.active, self.calls = position, playing, resets, 0, []

    def position(self):
        return self._pos

    def setPosition(self, value):
        self.calls.append(("setPosition", value))
        self._pos = value

    def play(self):
        self.calls.append("play")
        self.playing = True

    def pause(self):
        self.calls.append("pause")
        self.playing = False

    def setActiveAudioTrack(self, index):
        self.calls.append(("track", index))
        self.active = index
        if self.resets:
            self._pos, self.playing = 0, False


def test_switch_keeps_position_and_playing_state_on_a_well_behaved_backend():
    player = _FakePlayer(12_000, True, resets=False)
    mp.switch_audio_track(player, 1, True, lambda: player.playing)
    assert player.calls == [("track", 1)] and player.position() == 12_000 and player.playing


def test_switch_puts_position_and_playing_back_when_the_backend_restarts():
    player = _FakePlayer(12_000, True, resets=True)
    mp.switch_audio_track(player, 1, True, lambda: player.playing)
    assert player.active == 1 and player.position() == 12_000 and player.playing


def test_switch_keeps_a_paused_player_paused():
    player = _FakePlayer(7_000, False, resets=False)
    player.play_after = None
    mp.switch_audio_track(player, 1, False, lambda: player.playing)
    assert player.calls == [("track", 1)] and not player.playing
    resumed = _FakePlayer(7_000, False, resets=False)

    def starts_playing(index, original=resumed.setActiveAudioTrack):
        original(index)
        resumed.playing = True  # a backend that starts playing on a switch

    resumed.setActiveAudioTrack = starts_playing
    mp.switch_audio_track(resumed, 1, False, lambda: resumed.playing)
    assert not resumed.playing and "pause" in resumed.calls


def test_show_media_preview_passes_the_track_arguments_only_when_given(tmp_path, monkeypatch):
    seen = []

    class _Stub:
        def __init__(self, path, video=False, parent=None, autoplay=False, **extra):
            seen.append(extra)
            self.destroyed = type("N", (), {"connect": lambda *a: None})()

        def show(self):
            pass

    monkeypatch.setattr(mp, "multimedia_available", lambda: True)
    monkeypatch.setattr(mp, "MediaPreviewDialog", _Stub)
    monkeypatch.setattr(mp, "_open_windows", [])
    song = tmp_path / "a.mp3"
    song.write_bytes(b"x")
    mp.show_media_preview(None, song)
    mp._open_windows.clear()  # the stub has no window to close
    mp.show_media_preview(None, song, initial_audio_track=1, track_labels={0: ("eng", "")})
    assert seen == [{}, {"initial_audio_track": 1, "track_labels": {0: ("eng", "")}}]


def test_the_a_key_is_left_to_the_player_by_the_selector_combo():
    """Every item starts with 'Audio', so a plain combo would jump to the first item on 'A'."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtGui import QKeyEvent
    from PyQt6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    combo = mp._AudioCombo()
    combo.addItems(["Audio: 1 - AAC - eng", "Audio: 2 - AAC - nor"])
    combo.setCurrentIndex(1)
    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier, "a")
    combo.keyPressEvent(event)
    assert not event.isAccepted() and combo.currentIndex() == 1

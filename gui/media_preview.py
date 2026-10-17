"""
redactor_common/gui/media_preview.py

A small read-only preview window for an audio or video file: play/pause,
a seek bar, volume and, for files that have several, an audio-track and a
subtitle-track picker. Meant for checking a file in place (does it play, is
the right language on, is the tail intact) - there is no editing, no
playlist and nothing is written anywhere.

Usage, from an app's "Preview" action (one window per call, not modal, so
the main window stays usable; it frees itself when closed):

    from redactor_common.gui.media_preview import show_media_preview
    show_media_preview(self, path, video=True)

QtMultimedia is imported lazily inside the dialog so apps that never open a
preview don't pay its start-up cost, and a missing module becomes a normal
message box instead of an import error.
"""

from __future__ import annotations

import os

from typing import Callable, Mapping, Optional

from PyQt6.QtCore import QUrl, Qt
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QSlider, QVBoxLayout,
)

SEEK_STEP_MS = 5000
DEFAULT_VOLUME = 70  # percent


def format_time(ms: int) -> str:
    """Milliseconds as m:ss, or h:mm:ss from an hour up. Negative counts as 0."""
    seconds = max(0, int(ms)) // 1000
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def track_label(index: int, language: str, title: str) -> str:
    """"1 - English - Commentary": the picker text for one audio/subtitle track."""
    parts = [str(index + 1)]
    for part in (language, title):
        part = (part or "").strip()
        if part and part not in parts:
            parts.append(part)
    return " - ".join(parts)


UNDETERMINED_LANGUAGES = frozenset({"", "und", "undetermined", "anylanguage", "any language", "zxx"})


def audio_track_label(index: int, codec: str = "", language: str = "", title: str = "") -> str:
    """"1 - AAC - eng", "2 - AC3 - und (no language)", "1 - AAC - eng - Commentary": the text of one audio track in
    the selector (index is 0-based, shown 1-based). A track without a language says so instead of leaving a gap."""
    language = (language or "").strip()
    shown = "und (no language)" if language.lower() in UNDETERMINED_LANGUAGES else language
    parts = [str(index + 1)]
    for part in ((codec or "").strip(), shown, (title or "").strip()):
        if part and part not in parts:
            parts.append(part)
    return " - ".join(parts)


def normalize_track_labels(labels: Optional[Mapping]) -> dict[int, tuple[str, str, str]]:
    """The caller's `track_labels` as {audio track index: (language, title, codec)}. The index is the 0-based position
    among the file's AUDIO tracks (what QMediaPlayer.audioTracks() uses). A value may be a dict with the keys
    language / title / codec, or a (language, title) or (language, title, codec) sequence; anything else is ignored."""
    result: dict[int, tuple[str, str, str]] = {}
    for key, value in (labels or {}).items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(value, Mapping):
            language, title, codec = value.get("language", ""), value.get("title", ""), value.get("codec", "")
        elif isinstance(value, (tuple, list)) and 1 <= len(value) <= 3:
            language, title, codec = (list(value) + ["", ""])[:3]
        else:
            continue
        result[index] = (str(language or ""), str(title or ""), str(codec or ""))
    return result


def show_audio_selector(track_count: int) -> bool:
    """The audio selector is there only when there is something to choose between."""
    return track_count > 1


def next_audio_track(current: int, track_count: int) -> int:
    """The track the 'A' key moves to: the next one, from the last back to the first."""
    if track_count < 2:
        return max(current, 0)
    return (max(current, 0) + 1) % track_count


def switch_audio_track(player, index: int, was_playing: bool, is_playing: Callable[[], bool], tolerance_ms: int = 400) -> None:
    """Makes `index` the active audio track and keeps what the user was doing: the playback position and whether it
    was playing or paused. `player` needs position(), setPosition(), play(), pause() and setActiveAudioTrack()."""
    position = player.position()
    player.setActiveAudioTrack(index)
    if abs(player.position() - position) > tolerance_ms:
        player.setPosition(position)  # a backend that restarts the pipeline on a track switch
    playing = is_playing()
    if was_playing and not playing:
        player.play()
    elif playing and not was_playing:
        player.pause()


def multimedia_available() -> bool:
    # find_spec, not an import: loading QtMultimedia pulls in FFmpeg, whose shutdown can crash a process that
    # only asked "is it there?" (a test run did). The real import happens when a preview is opened.
    import importlib.util

    try:
        return all(importlib.util.find_spec(f"PyQt6.{name}") is not None for name in ("QtMultimedia", "QtMultimediaWidgets"))
    except (ImportError, ValueError):
        return False


class _AudioCombo(QComboBox):
    """The audio selector. A plain letter in a combo box jumps to the item starting with it, and every item starts
    with "Audio": the 'A' key must reach the player (cycle the track) instead."""

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_A and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            event.ignore()
            return
        super().keyPressEvent(event)


class MediaPreviewDialog(QDialog):
    def __init__(
        self, path: str, video: bool = False, parent=None, autoplay: bool = False,
        initial_audio_track: Optional[int] = None, track_labels: Optional[Mapping] = None,
    ) -> None:
        """`initial_audio_track`: open with this audio track active (0-based among the audio tracks), when the file has
        it. `track_labels`: {audio track index: language/title/codec} from the caller's own probe (ffprobe), used
        where QtMultimedia reports no language or title (it often does not: MP4 titles, 3-letter codes it cannot map)."""
        super().__init__(parent)
        from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer

        path = os.fspath(path)
        self._path = path
        self._video = video
        self._scrubbing = False
        self._initial_audio_track = initial_audio_track
        self._track_hints = normalize_track_labels(track_labels)
        self.setWindowTitle(f"Player - {os.path.basename(path)}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.resize(820, 560) if video else self.resize(520, 190)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(DEFAULT_VOLUME / 100)
        self.player.setAudioOutput(self.audio)

        layout = QVBoxLayout(self)
        if video:
            from PyQt6.QtMultimediaWidgets import QVideoWidget

            self.video_widget = QVideoWidget()
            self.video_widget.setMinimumSize(320, 180)
            self.player.setVideoOutput(self.video_widget)
            layout.addWidget(self.video_widget, 1)
        else:
            self.video_widget = None

        self.status_label = QLabel(os.path.basename(path))
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.play_btn = QPushButton("Play")
        self.play_btn.setMinimumWidth(70)
        self.play_btn.clicked.connect(self.toggle_play)
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.setRange(0, 0)
        self.seek.sliderPressed.connect(self._begin_scrub)
        self.seek.sliderReleased.connect(self._end_scrub)
        self.time_label = QLabel("0:00 / 0:00")
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(DEFAULT_VOLUME)
        self.volume.setMaximumWidth(90)
        self.volume.setToolTip("Volume")
        self.volume.valueChanged.connect(lambda v: self.audio.setVolume(v / 100))
        bar = QHBoxLayout()
        bar.addWidget(self.play_btn)
        bar.addWidget(self.seek, 1)
        bar.addWidget(self.time_label)
        bar.addWidget(self.volume)
        # The audio-track selector sits next to the volume; only shown for a file with more than one audio track.
        self.audio_combo = _AudioCombo()
        self.audio_combo.setToolTip("Audio track (the A key switches to the next one)")
        self.audio_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.audio_combo.setMinimumContentsLength(16)
        self.audio_combo.currentIndexChanged.connect(self._pick_audio_track)
        bar.addWidget(self.audio_combo)
        layout.addLayout(bar)

        self.subtitle_combo = QComboBox()
        self.subtitle_combo.currentIndexChanged.connect(self._pick_subtitle_track)
        tracks = QHBoxLayout()
        self._subtitle_caption = QLabel("Subtitles:")
        tracks.addWidget(self._subtitle_caption)
        tracks.addWidget(self.subtitle_combo, 1)
        layout.addLayout(tracks)
        self._set_track_rows_visible(False)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        footer = QHBoxLayout()
        footer.addStretch(1)
        footer.addWidget(close_btn)
        layout.addLayout(footer)

        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.tracksChanged.connect(self._fill_tracks)
        self.player.errorOccurred.connect(self._on_error)
        self.player.mediaStatusChanged.connect(self._on_media_status)

        self.player.setSource(QUrl.fromLocalFile(path))
        if autoplay:
            self.player.play()

    # --- transport -------------------------------------------------------------------------------

    def toggle_play(self) -> None:
        from PyQt6.QtMultimedia import QMediaPlayer

        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _begin_scrub(self) -> None:
        self._scrubbing = True

    def _end_scrub(self) -> None:
        self._scrubbing = False
        self.player.setPosition(self.seek.value())

    def _on_position(self, position: int) -> None:
        if not self._scrubbing:
            self.seek.setValue(position)
        self.time_label.setText(f"{format_time(position)} / {format_time(self.player.duration())}")

    def _on_duration(self, duration: int) -> None:
        self.seek.setRange(0, max(0, duration))
        self.time_label.setText(f"{format_time(self.player.position())} / {format_time(duration)}")

    def _on_state(self, state) -> None:
        from PyQt6.QtMultimedia import QMediaPlayer

        self.play_btn.setText("Pause" if state == QMediaPlayer.PlaybackState.PlayingState else "Play")

    def _on_media_status(self, status) -> None:
        from PyQt6.QtMultimedia import QMediaPlayer

        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.player.setPosition(0)
            self.player.pause()

    def _on_error(self, _error, message: str) -> None:
        self.status_label.setText(f"Can't play this file: {message or 'unknown error'}")

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key.Key_Space:
            self.toggle_play()
        elif key == Qt.Key.Key_Left:
            self.player.setPosition(max(0, self.player.position() - SEEK_STEP_MS))
        elif key == Qt.Key.Key_Right:
            self.player.setPosition(min(self.player.duration(), self.player.position() + SEEK_STEP_MS))
        elif key == Qt.Key.Key_A and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            self.cycle_audio_track()
        else:
            super().keyPressEvent(event)

    # --- tracks ----------------------------------------------------------------------------------

    def _set_track_rows_visible(self, audio: bool, subtitles: bool = False) -> None:
        self.audio_combo.setVisible(audio)
        for widget in (self._subtitle_caption, self.subtitle_combo):
            widget.setVisible(subtitles)

    def _track_labels(self, tracks) -> list[str]:
        from PyQt6.QtMultimedia import QMediaMetaData

        labels = []
        for index, meta in enumerate(tracks):
            language = meta.value(QMediaMetaData.Key.Language)
            language = language.name if hasattr(language, "name") else (str(language) if language else "")
            labels.append(track_label(index, language, meta.stringValue(QMediaMetaData.Key.Title)))
        return labels

    def _audio_labels(self, tracks) -> list[str]:
        """"Audio: 1 - AAC - eng" per audio track: the caller's hints win over what QtMultimedia reports."""
        from PyQt6.QtCore import QLocale
        from PyQt6.QtMultimedia import QMediaMetaData

        labels = []
        for index, meta in enumerate(tracks):
            hint_language, hint_title, hint_codec = self._track_hints.get(index, ("", "", ""))
            language = meta.value(QMediaMetaData.Key.Language)
            qt_language = ""
            if hasattr(language, "name") and language.name != "AnyLanguage":
                qt_language = QLocale.languageToCode(language, QLocale.LanguageCodeType.ISO639Alpha3) or language.name
            codec = meta.value(QMediaMetaData.Key.AudioCodec)
            qt_codec = codec.name.upper() if hasattr(codec, "name") and codec.name != "Unspecified" else ""
            title = hint_title or meta.stringValue(QMediaMetaData.Key.Title)
            labels.append("Audio: " + audio_track_label(
                index, (hint_codec or qt_codec).upper(), hint_language or qt_language, title))
        return labels

    def _fill_tracks(self) -> None:
        audio_tracks = self.player.audioTracks()
        subtitle_tracks = self.player.subtitleTracks() if self._video else []
        if self._initial_audio_track is not None and audio_tracks:
            wanted, self._initial_audio_track = self._initial_audio_track, None  # once: later refills keep the user's pick
            if 0 <= wanted < len(audio_tracks) and wanted != self.player.activeAudioTrack():
                self.player.setActiveAudioTrack(wanted)
        for combo, tracks, active in (
            (self.audio_combo, audio_tracks, self.player.activeAudioTrack()),
            (self.subtitle_combo, subtitle_tracks, self.player.activeSubtitleTrack()),
        ):
            combo.blockSignals(True)
            combo.clear()
            if combo is self.subtitle_combo:
                combo.addItem("Off")
            for label in (self._audio_labels(tracks) if combo is self.audio_combo else self._track_labels(tracks)):
                combo.addItem(label)
            offset = 1 if combo is self.subtitle_combo else 0
            combo.setCurrentIndex(max(0, active + offset) if active >= 0 else 0)
            combo.blockSignals(False)
        self._set_track_rows_visible(show_audio_selector(len(audio_tracks)), bool(subtitle_tracks))

    def _pick_audio_track(self, index: int) -> None:
        if index < 0:
            return
        from PyQt6.QtMultimedia import QMediaPlayer

        def playing() -> bool:
            return self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

        switch_audio_track(self.player, index, playing(), playing)

    def cycle_audio_track(self) -> None:
        """The 'A' key: the next audio track (does nothing for a file with one)."""
        count = self.audio_combo.count()
        if show_audio_selector(count):
            self.audio_combo.setCurrentIndex(next_audio_track(self.audio_combo.currentIndex(), count))

    def _pick_subtitle_track(self, index: int) -> None:
        self.player.setActiveSubtitleTrack(index - 1)  # row 0 is "Off" (-1)

    # --- life cycle ------------------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self.player.stop()
        self.player.setSource(QUrl())
        super().closeEvent(event)


_open_windows: list[MediaPreviewDialog] = []


def show_media_preview(
    parent, path: str, video: bool = False, autoplay: bool = True,
    initial_audio_track: Optional[int] = None, track_labels: Optional[Mapping] = None,
) -> MediaPreviewDialog | None:
    """Open the preview window for `path` (not modal; kept alive until closed).
    Returns it, or None after telling the user why not (missing file, no QtMultimedia).
    `initial_audio_track` (0-based among the audio tracks) opens it with that track active; `track_labels` is
    {audio track index: {"language", "title", "codec"}} from the caller's ffprobe, for the selector's text."""
    path = os.fspath(path)  # callers hand over str or pathlib.Path; QUrl.fromLocalFile() takes only str
    if not os.path.isfile(path):
        QMessageBox.information(parent, "Preview", f"The file no longer exists:\n{path}")
        return None
    if not multimedia_available():
        QMessageBox.information(parent, "Preview", "This build has no media playback support (QtMultimedia).")
        return None
    # Only one player at a time: two files playing over each other is never what anyone wants. The old window is
    # closed (which stops it) and the new one opens where it was, at the same size.
    previous_geometry = None
    for old in list(_open_windows):
        previous_geometry = old.geometry()
        old.close()
        _open_windows.remove(old)  # it is deleted later; do not count it as open meanwhile
    extra = {}  # only passed when asked for, so a caller (and a test stub) that knows nothing of tracks is unchanged
    if initial_audio_track is not None:
        extra["initial_audio_track"] = initial_audio_track
    if track_labels:
        extra["track_labels"] = track_labels
    dialog = MediaPreviewDialog(path, video=video, parent=parent, autoplay=autoplay, **extra)
    if previous_geometry is not None:
        dialog.setGeometry(previous_geometry)
    _open_windows.append(dialog)
    dialog.destroyed.connect(lambda *_: _open_windows.remove(dialog) if dialog in _open_windows else None)
    dialog.show()
    return dialog

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


def multimedia_available() -> bool:
    # find_spec, not an import: loading QtMultimedia pulls in FFmpeg, whose shutdown can crash a process that
    # only asked "is it there?" (a test run did). The real import happens when a preview is opened.
    import importlib.util

    try:
        return all(importlib.util.find_spec(f"PyQt6.{name}") is not None for name in ("QtMultimedia", "QtMultimediaWidgets"))
    except (ImportError, ValueError):
        return False


class MediaPreviewDialog(QDialog):
    def __init__(self, path: str, video: bool = False, parent=None) -> None:
        super().__init__(parent)
        from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer

        self._path = path
        self._video = video
        self._scrubbing = False
        self.setWindowTitle(f"Preview - {os.path.basename(path)}")
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
        layout.addLayout(bar)

        self.audio_combo = QComboBox()
        self.audio_combo.currentIndexChanged.connect(self._pick_audio_track)
        self.subtitle_combo = QComboBox()
        self.subtitle_combo.currentIndexChanged.connect(self._pick_subtitle_track)
        tracks = QHBoxLayout()
        self._audio_caption = QLabel("Audio:")
        self._subtitle_caption = QLabel("Subtitles:")
        tracks.addWidget(self._audio_caption)
        tracks.addWidget(self.audio_combo, 1)
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
        else:
            super().keyPressEvent(event)

    # --- tracks ----------------------------------------------------------------------------------

    def _set_track_rows_visible(self, audio: bool, subtitles: bool = False) -> None:
        for widget in (self._audio_caption, self.audio_combo):
            widget.setVisible(audio)
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

    def _fill_tracks(self) -> None:
        audio_tracks = self.player.audioTracks()
        subtitle_tracks = self.player.subtitleTracks() if self._video else []
        for combo, tracks, active in (
            (self.audio_combo, audio_tracks, self.player.activeAudioTrack()),
            (self.subtitle_combo, subtitle_tracks, self.player.activeSubtitleTrack()),
        ):
            combo.blockSignals(True)
            combo.clear()
            if combo is self.subtitle_combo:
                combo.addItem("Off")
            for label in self._track_labels(tracks):
                combo.addItem(label)
            offset = 1 if combo is self.subtitle_combo else 0
            combo.setCurrentIndex(max(0, active + offset) if active >= 0 else 0)
            combo.blockSignals(False)
        self._set_track_rows_visible(len(audio_tracks) > 1, bool(subtitle_tracks))

    def _pick_audio_track(self, index: int) -> None:
        if index >= 0:
            self.player.setActiveAudioTrack(index)

    def _pick_subtitle_track(self, index: int) -> None:
        self.player.setActiveSubtitleTrack(index - 1)  # row 0 is "Off" (-1)

    # --- life cycle ------------------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self.player.stop()
        self.player.setSource(QUrl())
        super().closeEvent(event)


_open_windows: list[MediaPreviewDialog] = []


def show_media_preview(parent, path: str, video: bool = False) -> MediaPreviewDialog | None:
    """Open the preview window for `path` (not modal; kept alive until closed).
    Returns it, or None after telling the user why not (missing file, no QtMultimedia)."""
    if not os.path.isfile(path):
        QMessageBox.information(parent, "Preview", f"The file no longer exists:\n{path}")
        return None
    if not multimedia_available():
        QMessageBox.information(parent, "Preview", "This build has no media playback support (QtMultimedia).")
        return None
    dialog = MediaPreviewDialog(path, video=video, parent=parent)
    _open_windows.append(dialog)
    dialog.destroyed.connect(lambda *_: _open_windows.remove(dialog) if dialog in _open_windows else None)
    dialog.show()
    return dialog

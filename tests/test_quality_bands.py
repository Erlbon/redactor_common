"""core/quality_bands.py (the audio profile and helpers) and gui/quality_colour.py (the shared painter)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication, QTableWidgetItem

from redactor_common.core.quality_bands import (
    AUDIO_CODEC_FACTORS, BAND_COLORS, AudioBandSettings, Band, BandResult, classify_audio, codec_factor,
    format_kbps, format_mbps, unknown,
)
from redactor_common.gui.colors import HIGHLIGHT_TEXT_COLOR
from redactor_common.gui.quality_colour import apply_band_colour

_app = QApplication.instance() or QApplication([])


def band(*a, **k):
    return classify_audio(*a, **k).band


def test_the_three_tints_are_the_cbz_ones():
    assert BAND_COLORS == {Band.POOR: "#fde047", Band.OK: "#86efac", Band.OVER: "#fdba74"}


def test_mp3_boundaries_around_128():
    assert band(127) is Band.POOR and band(128) is Band.OK and band(192) is Band.OK and band(320) is Band.OK
    assert band(64) is Band.POOR


def test_vbr_is_judged_by_its_average():
    assert band(190, vbr=True) is Band.OK and band(115, vbr=True) is Band.POOR
    assert "VBR average 190 kbps" in classify_audio(190, vbr=True).tooltip


def test_codec_factors_shift_the_effective_bitrate():
    # AAC counts for 0.75: 96 kbps AAC is MP3-equivalent 128 (fine), 95 is below it
    assert band(96, codec="aac") is Band.OK and band(95, codec="aac") is Band.POOR
    assert band(63, codec="opus") is Band.POOR and band(64, codec="opus") is Band.OK
    assert band(103, codec="vorbis") is Band.OK and band(102, codec="vorbis") is Band.POOR
    assert band(141, codec="wma") is Band.OK and band(140, codec="wma") is Band.POOR  # wma needs 10% more
    assert band(120, codec="somethingnew") is Band.POOR  # unknown codec: factor 1.0
    assert codec_factor("mp4a.40.2", AUDIO_CODEC_FACTORS) == 0.75 and codec_factor("Opus", AUDIO_CODEC_FACTORS) == 0.5
    assert codec_factor("", AUDIO_CODEC_FACTORS) == 1.0 and codec_factor("H.265", {"h265": 0.6}) == 0.6


def test_a_low_sample_rate_makes_stereo_music_poor_and_the_tooltip_says_why():
    low = classify_audio(256, sample_rate_hz=22050, channels=2)
    first_line = low.tooltip.split("\n")[0]
    assert low.band is Band.POOR and "sample rate is below 32 kHz" in first_line and "bitrate" not in first_line.split("Poor:")[1]
    both = classify_audio(64, sample_rate_hz=22050, channels=2)
    first = both.tooltip.split("\n")[0]
    assert both.band is Band.POOR and "bitrate" in first and "sample rate" in first
    assert band(256, sample_rate_hz=32000, channels=2) is Band.OK
    assert band(256, sample_rate_hz=22050, channels=None) is Band.POOR  # channels unknown: judged as stereo


def test_mono_gets_a_note_not_a_colour_change():
    mono = classify_audio(160, sample_rate_hz=22050, channels=1)
    assert mono.band is Band.OK and "Mono: fine for voice" in mono.tooltip
    assert band(96, channels=1) is Band.POOR  # a really low bitrate is still poor


def test_unknown_bitrate_has_no_colour():
    for value in (None, 0, -5):
        result = classify_audio(value)
        assert result.band is Band.UNKNOWN and not result.has_colour and "unknown" in result.tooltip
    assert unknown("x").band is Band.UNKNOWN


def test_over_is_unused_unless_an_app_passes_a_limit():
    assert band(320) is Band.OK
    assert band(320, settings=AudioBandSettings(over_kbps=256)) is Band.OVER
    assert band(256, settings=AudioBandSettings(over_kbps=256)) is Band.OK


def test_settings_change_the_boundaries_and_the_tooltip_wording():
    s = AudioBandSettings(poor_kbps=160, good_kbps=224)
    assert band(150, settings=s) is Band.POOR and band(160, settings=s) is Band.OK
    text = classify_audio(200, settings=s).tooltip
    assert "Poor below 160 kbps" in text and "good from about 224 kbps" in text and "transparent from about 256" in text


def test_formatting():
    assert format_mbps(2_100_000) == "2.1 Mbps" and format_mbps(20_000_000) == "20 Mbps" and format_kbps(191.6) == "192 kbps"


def test_the_painter_colours_after_the_row_tint_and_always_sets_the_tooltip():
    item = QTableWidgetItem("190")
    item.setBackground(QColor("#ff0000"))
    result = classify_audio(190)
    assert apply_band_colour(item, result) is True
    assert item.background().color() == QColor("#86efac") and item.foreground().color() == HIGHLIGHT_TEXT_COLOR
    assert item.toolTip() == result.tooltip
    poor = QTableWidgetItem("90")
    apply_band_colour(poor, classify_audio(90))
    assert poor.background().color() == QColor("#fde047")
    over = QTableWidgetItem("x")
    apply_band_colour(over, BandResult(Band.OVER, "o", "t"))
    assert over.background().color() == QColor("#fdba74")


def test_the_painter_never_overpaints_a_failed_item_or_paints_unknown():
    failed = QTableWidgetItem("x")
    failed.setBackground(QColor("#ff0000"))
    assert apply_band_colour(failed, classify_audio(190), failed=True) is False
    assert failed.background().color() == QColor("#ff0000") and failed.toolTip()  # red stays, tooltip still there
    blank = QTableWidgetItem("?")
    blank.setBackground(QColor("#abcdef"))
    assert apply_band_colour(blank, classify_audio(None)) is False
    assert blank.background().color() == QColor("#abcdef") and "unknown" in blank.toolTip()

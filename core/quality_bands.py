"""
redactor_common/core/quality_bands.py

The shared, Qt-free vocabulary of the quality colours of a table column (cbzredactor's Size column, videoredactor's
Bitrate column, mp3redactor's Bitrate column):

  Band          POOR (yellow: "check it", not "bad"), OK (green), OVER (orange: above a limit), UNKNOWN (no colour)
  BAND_COLORS   the three tints (solid light colours that read in light and dark themes, drawn with a dark text colour)
  BandResult    the band plus a short `summary` and the full `tooltip`
  codec_factor  a codec's weight in a table {codec: factor} (1.0 = the reference codec), tolerant of spelling
  format_*      "2.1 Mbps", "192 kbps"
  classify_audio  the AUDIO profile, a pure function (below)

An app keeps its own profile (videoredactor: bits per pixel per frame against the conversion limit) and returns a
BandResult; gui/quality_colour.apply_band_colour() paints it.

AUDIO profile. The bitrate of a file divided by the codec's factor (relative to MP3: mp3 1.0, aac 0.75, opus 0.5,
vorbis 0.8, wma 1.1) is its MP3-equivalent bitrate. POOR when that is below `poor_kbps` (default 128) OR the sample rate
is below 32 kHz for stereo music (the tooltip names which); OK otherwise. A VBR file is judged by its AVERAGE bitrate
(V2, about 190 kbps, is fine; V6, about 115, is poor). OVER (orange) is unused unless an app passes
`over_kbps`. A mono file gets a note and no change of colour (voice recordings are fine mono). No bitrate: UNKNOWN.
`good_kbps` (default 192) only shapes the tooltip's wording ("good from about 192 kbps, transparent from about 256").
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import Mapping, Optional


class Band(str, enum.Enum):
    POOR = "poor"
    OK = "ok"
    OVER = "over"
    UNKNOWN = "unknown"


# The cbzredactor tints; the text on them is dark (the same black as gui/colors.HIGHLIGHT_TEXT_COLOR).
BAND_COLORS: dict[Band, str] = {Band.POOR: "#fde047", Band.OK: "#86efac", Band.OVER: "#fdba74"}
BAND_TEXT_COLOR = "#000000"
LEGEND = "yellow = poor (check it), green = fine, orange = above the limit"


@dataclass(frozen=True)
class BandResult:
    band: Band
    summary: str = ""   # one short phrase
    tooltip: str = ""   # the full explanation, shown as the cell's tooltip

    @property
    def has_colour(self) -> bool:
        return self.band in BAND_COLORS


def unknown(tooltip: str) -> BandResult:
    """No colour; the tooltip says what is missing."""
    return BandResult(Band.UNKNOWN, "unknown", tooltip)


# -- helpers ------------------------------------------------------------------------------------------------------------

def _key(codec: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(codec or "").lower())


def codec_factor(codec: object, table: Mapping[str, float], default: float = 1.0) -> float:
    """The factor for `codec` in `table` (keys compared lower-case without punctuation, so "H.265" and "h265" are the
    same key). An unknown codec gets `default`; a codec name that merely starts with a listed key ("mp4a.40.2" for
    "mp4a", "wmv3" for "wmv") counts as that key."""
    wanted = _key(codec)
    if not wanted:
        return default
    cleaned = {_key(k): float(v) for k, v in table.items()}
    if wanted in cleaned:
        return cleaned[wanted]
    for key in sorted(cleaned, key=len, reverse=True):
        if key and wanted.startswith(key):
            return cleaned[key]
    return default


def format_mbps(bits_per_second: float) -> str:
    mbps = bits_per_second / 1_000_000
    return f"{mbps:.1f} Mbps" if mbps < 10 else f"{mbps:.0f} Mbps"


def format_kbps(kbps: float) -> str:
    return f"{kbps:.0f} kbps"


def join_tooltip(*parts: str) -> str:
    """The non-empty parts as tooltip lines."""
    return "\n".join(p for p in parts if p)


# -- the audio profile ----------------------------------------------------------------------------------------------------

AUDIO_CODEC_FACTORS: dict[str, float] = {
    "mp3": 1.0, "mpeg": 1.0, "aac": 0.75, "mp4a": 0.75, "opus": 0.5, "vorbis": 0.8, "ogg": 0.8, "wma": 1.1,
}
AUDIO_CODEC_NAMES = {"mp3": "MP3", "mpeg": "MP3", "aac": "AAC", "mp4a": "AAC", "opus": "Opus", "vorbis": "Vorbis",
                     "ogg": "Vorbis", "wma": "WMA"}
STEREO_MIN_SAMPLE_RATE_HZ = 32_000
TRANSPARENT_KBPS = 256


@dataclass(frozen=True)
class AudioBandSettings:
    poor_kbps: float = 128.0
    good_kbps: float = 192.0
    over_kbps: Optional[float] = None  # None: the OVER band is not used for audio


def _audio_name(codec: object) -> str:
    wanted = _key(codec)
    for key in sorted(AUDIO_CODEC_NAMES, key=len, reverse=True):
        if wanted.startswith(key):
            return AUDIO_CODEC_NAMES[key]
    return str(codec).upper() if codec else "MP3"


def classify_audio(
    bitrate_kbps: Optional[float],
    codec: str = "mp3",
    vbr: Optional[bool] = None,
    sample_rate_hz: Optional[float] = None,
    channels: Optional[int] = None,
    settings: AudioBandSettings = AudioBandSettings(),
) -> BandResult:
    """The band of an audio file (see the module docstring). `bitrate_kbps` is the file's (for VBR: average) bitrate;
    `vbr` only adds the note that the average was used."""
    if not bitrate_kbps or bitrate_kbps <= 0:
        return unknown("The bitrate is unknown, so the file has no quality colour.")
    factor = codec_factor(codec, AUDIO_CODEC_FACTORS)
    name = _audio_name(codec)
    effective = bitrate_kbps / factor
    mono = channels == 1
    low_rate = bool(sample_rate_hz) and sample_rate_hz < STEREO_MIN_SAMPLE_RATE_HZ and not mono

    head = f"{name}, {'VBR average ' if vbr else ''}{format_kbps(bitrate_kbps)}"
    if sample_rate_hz:
        head += f", {sample_rate_hz / 1000:g} kHz"
    if channels:
        head += ", mono" if mono else (", stereo" if channels == 2 else f", {channels} channels")
    equivalent = f" ({name}-equivalent {format_kbps(effective)})" if abs(factor - 1.0) > 1e-9 else ""

    reasons: list[str] = []
    if effective < settings.poor_kbps:
        reasons.append(f"the bitrate{equivalent} is below {format_kbps(settings.poor_kbps)}")
    if low_rate:
        reasons.append(f"the sample rate is below {STEREO_MIN_SAMPLE_RATE_HZ // 1000} kHz for stereo music")

    guide = (f"Poor below {format_kbps(settings.poor_kbps)} (MP3-equivalent); good from about "
             f"{format_kbps(settings.good_kbps)}, transparent from about {TRANSPARENT_KBPS} kbps (V0, 320).")
    notes = []
    if vbr:
        notes.append("VBR: the average bitrate is used (V2 is about 190 kbps and fine, V6 about 115 and poor).")
    if mono:
        notes.append("Mono: fine for voice recordings; it does not change the colour.")
    notes.append("Yellow means 'check it', not 'bad': speech and simple material need less.")

    if settings.over_kbps and bitrate_kbps > settings.over_kbps:
        return BandResult(Band.OVER, f"{format_kbps(bitrate_kbps)} (over)",
                          join_tooltip(f"{head}.", f"Above the limit of {format_kbps(settings.over_kbps)}.", guide, *notes))
    if reasons:
        return BandResult(Band.POOR, f"{format_kbps(bitrate_kbps)} (poor)",
                          join_tooltip(f"{head}. Poor: " + " and ".join(reasons) + ".", guide, *notes))
    return BandResult(Band.OK, f"{format_kbps(bitrate_kbps)} (fine)", join_tooltip(f"{head}. Fine.", guide, *notes))

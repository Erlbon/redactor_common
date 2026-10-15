"""
redactor_common/core/lrclib.py

LRCLIB (https://lrclib.net): a free, keyless, community database of song
lyrics, many of them time-synced ("LRC": one `[mm:ss.xx] line` per lyric
line). Qt-free, shared by mp3redactor (plain/synced lyrics) and
videoredactor (synced lyrics as a subtitle source for music videos).

What is here:

  LrclibClient        the two lookups: get_by_signature() (artist + title +
                      album + duration, an exact match or None) and search()
                      (a list). `fetch` is injectable (tests never touch the
                      network); the default fetch is throttled to about one
                      request per second and retries an HTTP 429 (LRCLIB
                      rate-limits shared addresses hard) with growing delays.
  Lyrics              one result: ids, names, track duration, plain text and
                      synced (LRC) text. `has_timing` says whether it can
                      become subtitles at all.
  parse_lrc()         LRC text -> sorted list of LrcLine(start, text).
  lrc_to_srt()        LRC text -> a valid SRT (LF line ends, UTF-8 by the
                      caller's writer), with an offset in seconds.
  score_match()       how well a result fits the local file (artist, title,
                      length), as an 'exact' / 'close' / 'different' kind and
                      a label that says how many seconds the video is longer
                      or shorter than the track.
  split_artist_title  "Artist - Title" from a file name.

Unicode is kept as it is: names are compared case-folded with punctuation
dropped, never ASCII-folded, so Cyrillic, Greek, CJK and accented names work.
"""

from __future__ import annotations

import difflib
import re
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence, Union

from redactor_common.core.lookup_client import (
    FetchFn,
    LookupError,
    Throttle,
    build_request,
    fetch_json,
    make_default_fetch,
    redact_url,
    throttled,
)

BASE_URL = "https://lrclib.net/api"
DEFAULT_USER_AGENT = "RedactorCommon (https://github.com/Erlbon)"
# About one request per second, as the service asks of polite clients.
DEFAULT_INTERVAL = 1.1
# LRCLIB answers 429 to shared addresses even on a first request; wait and retry.
DEFAULT_RETRY_429 = (3.0, 6.0, 12.0)
_RATE_LIMITED = "LRCLIB is rate-limiting requests right now; try again in a minute."
_UNREACHABLE = "Could not reach LRCLIB: {detail} (check your internet connection)"

# A lyric line longer than this is wrapped onto two lines in the SRT.
WRAP_COLUMNS = 42
# Seconds of difference between video and track that still count as exact / close.
EXACT_SECONDS = 2.0
CLOSE_SECONDS = 6.0


class LrclibError(LookupError):
    """LRCLIB could not be reached, rate-limited the request, or sent an unusable reply. The text is
    meant to be shown to the user."""


# -- results -------------------------------------------------------------------

@dataclass
class Lyrics:
    """One LRCLIB entry. `synced` is the LRC text ("" when the entry only has plain lyrics)."""

    id: Optional[int] = None
    track_name: str = ""
    artist_name: str = ""
    album_name: str = ""
    duration: Optional[float] = None  # the track's length in seconds
    instrumental: bool = False
    plain: str = ""
    synced: str = ""

    @property
    def has_timing(self) -> bool:
        """True when the synced text holds at least one timed, non-empty lyric line: only then can the
        entry become subtitles. Plain-only and instrumental entries cannot."""
        if self.instrumental or not (self.synced or "").strip():
            return False
        return any(line.text for line in parse_lrc(self.synced))


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def lyrics_from_json(entry: Any) -> Optional[Lyrics]:
    """A Lyrics from one LRCLIB JSON object; None when `entry` is not one."""
    if not isinstance(entry, dict):
        return None
    ident = entry.get("id")
    return Lyrics(
        id=ident if isinstance(ident, int) and not isinstance(ident, bool) else None,
        track_name=_text(entry.get("trackName")) or _text(entry.get("name")),
        artist_name=_text(entry.get("artistName")),
        album_name=_text(entry.get("albumName")),
        duration=_number(entry.get("duration")),
        instrumental=bool(entry.get("instrumental")),
        plain=_text(entry.get("plainLyrics")),
        synced=_text(entry.get("syncedLyrics")),
    )


# -- the client -----------------------------------------------------------------

class LrclibClient:
    """LRCLIB lookups. No API key.

        client = LrclibClient("TheVideoRedactor/2026-10-15 (https://github.com/Erlbon)")
        hit = client.get_by_signature("Depeche Mode", "Enjoy the Silence", "Violator", 399)
        results = client.search("Depeche Mode", "Enjoy the Silence")

    `fetch`: a callable taking a urllib Request and returning bytes (tests inject one; None builds the real
    one with `user_agent`, throttled to `interval` seconds between requests). `retry_429`: delays between
    retries of an HTTP 429. `sleep` is injectable so tests never wait.
    """

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        fetch: FetchFn | None = None,
        interval: float = DEFAULT_INTERVAL,
        retry_429: Sequence[float] = DEFAULT_RETRY_429,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 15.0,
        throttle: Throttle | None = None,
    ):
        self.user_agent = user_agent
        if fetch is None:
            fetch = throttled(make_default_fetch(user_agent, timeout=timeout), throttle or Throttle(interval))
        self._fetch = fetch
        self.retry_429 = tuple(retry_429)
        self._sleep = sleep

    def _get(self, path: str, params: dict[str, object], ignore_404: bool = False) -> Any:
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        request = build_request(f"{BASE_URL}{path}", clean, {"User-Agent": self.user_agent})
        try:
            return fetch_json(
                request, self._fetch, LrclibError, "LRCLIB", ignore_404=ignore_404,
                status_messages={429: _RATE_LIMITED}, retry_429=self.retry_429, sleep=self._sleep,
                unreachable_message=_UNREACHABLE,
            )
        except LrclibError as exc:
            raise LrclibError(redact_url(str(exc))) from exc.__cause__

    def get_by_signature(
        self, artist: str, title: str, album: str = "", duration: float | None = None
    ) -> Optional[Lyrics]:
        """The entry whose artist, title, album and length (within a couple of seconds, LRCLIB's rule)
        match, or None (HTTP 404). `album` and `duration` may be left out, which loosens the match."""
        params: dict[str, object] = {"artist_name": artist, "track_name": title, "album_name": album}
        if duration is not None and duration > 0:
            params["duration"] = int(round(duration))
        return lyrics_from_json(self._get("/get", params, ignore_404=True))

    def search(self, artist: str = "", title: str = "", query: str = "") -> list[Lyrics]:
        """Entries for a track: by artist and title together, by title alone, or by free text `query`
        when neither is given. Entries come back in LRCLIB's order."""
        if title:
            params: dict[str, object] = {"track_name": title, "artist_name": artist}
        elif query or artist:
            params = {"q": query or artist}
        else:
            return []
        data = self._get("/search", params)
        if not isinstance(data, list):
            return []
        return [item for item in (lyrics_from_json(e) for e in data) if item is not None]


# -- LRC parsing ----------------------------------------------------------------

@dataclass(frozen=True)
class LrcLine:
    start: float  # seconds
    text: str     # "" = a gap marker (instrumental break)


_STAMP = r"\[\s*(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\s*\]"
_STAMPS_AT_START = re.compile(r"^\s*((?:" + _STAMP + r"\s*)+)")
_STAMP_RE = re.compile(_STAMP)
_OFFSET_RE = re.compile(r"^\s*\[\s*offset\s*:\s*([+-]?\d+)\s*\]", re.IGNORECASE)
# Enhanced LRC word times <mm:ss.xx> inside a line.
_WORD_STAMP_RE = re.compile(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>")


def _stamp_seconds(minutes: str, seconds: str, fraction: str | None) -> float:
    value = int(minutes) * 60 + int(seconds)
    if fraction:
        value += int(fraction) / (10 ** len(fraction))
    return float(value)


def parse_lrc(text: str) -> list[LrcLine]:
    """LRC text -> LrcLine list sorted by start time (stable for equal times).

    Understood: [mm:ss.xx], [mm:ss.xxx], [mm:ss.x], [mm:ss]; several timestamps in front of one line (the
    line is repeated at each time); [offset:+/-ms] (positive = the lyrics appear that much earlier, the
    usual LRC meaning); lines with a timestamp and no text (kept, as a gap marker with text ""). Ignored:
    metadata tags ([ar:] [ti:] [al:] [by:] [length:] ...), untimed lines, enhanced-LRC word times."""
    offset = 0.0
    lines: list[LrcLine] = []
    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        m_offset = _OFFSET_RE.match(raw)
        if m_offset:
            offset = int(m_offset.group(1)) / 1000.0
            continue
        m = _STAMPS_AT_START.match(raw)
        if not m:
            continue
        body = _WORD_STAMP_RE.sub("", raw[m.end():]).strip()
        for s in _STAMP_RE.finditer(m.group(1)):
            lines.append(LrcLine(_stamp_seconds(*s.groups()), body))
    if offset:
        lines = [LrcLine(l.start - offset, l.text) for l in lines]
    lines.sort(key=lambda l: l.start)  # sorted() is stable
    return lines


# -- LRC -> SRT -------------------------------------------------------------------

def _srt_time(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def wrap_line(text: str, columns: int = WRAP_COLUMNS) -> str:
    """`text`, or (when longer than `columns`) the same words on two lines, broken at the space nearest the
    middle. A line without a usable space stays as it is."""
    text = " ".join(text.split())
    if len(text) <= columns:
        return text
    spaces = [i for i, ch in enumerate(text) if ch == " "]
    if not spaces:
        return text
    middle = len(text) / 2
    cut = min(spaces, key=lambda i: abs(i - middle))
    return text[:cut] + "\n" + text[cut + 1:]


def lrc_to_srt(
    lyrics_lrc: Union[str, Sequence[LrcLine]],
    media_duration: float | None = None,
    offset_seconds: float = 0.0,
    max_line_seconds: float = 7.0,
    min_line_seconds: float = 1.0,
    gap_seconds: float = 0.05,
) -> str:
    """LRC text (or parse_lrc() output) -> SRT text, numbered cues, LF line ends, "" when nothing is timed.

    Each lyric line starts at its timestamp and ends `gap_seconds` before the next entry's start (an empty
    LRC line is an entry, so it ends the previous line early), but never lasts longer than
    `max_line_seconds` (a long instrumental break does not keep a line on screen) and, where the next line
    allows, not shorter than `min_line_seconds`. The last line ends at start + `max_line_seconds`, or at
    `media_duration` if that comes first; cues starting at or after `media_duration` are dropped.
    `offset_seconds` shifts everything (positive = later); times are clamped at 0 and a cue that ends up
    entirely before 0 is dropped. Lines longer than WRAP_COLUMNS characters are broken in two."""
    entries = list(parse_lrc(lyrics_lrc) if isinstance(lyrics_lrc, str) else lyrics_lrc)
    entries = [LrcLine(e.start + offset_seconds, e.text) for e in entries]
    entries.sort(key=lambda e: e.start)

    cues: list[tuple[float, float, str]] = []
    for i, entry in enumerate(entries):
        if not entry.text:
            continue
        start = entry.start
        nxt = entries[i + 1].start if i + 1 < len(entries) else None
        limit = (nxt - gap_seconds) if nxt is not None else None
        end = start + max_line_seconds
        if limit is not None:
            end = min(end, limit)
            if end - start < min_line_seconds:
                end = min(start + min_line_seconds, limit)
        if media_duration is not None:
            if start >= media_duration:
                continue
            end = min(end, media_duration)
        if end <= start:
            end = start + 0.3  # simultaneous lines: a short cue beats a lost one
        if end <= 0:
            continue
        cues.append((max(0.0, start), max(0.0, end), wrap_line(entry.text)))

    blocks = [
        f"{n}\n{_srt_time(a)} --> {_srt_time(b)}\n{body}\n"
        for n, (a, b, body) in enumerate(cues, start=1)
    ]
    return "\n".join(blocks)


def srt_preview(srt_text: str, cues: int = 8) -> str:
    """The first `cues` cues of an SRT text, unchanged, for a preview."""
    parts = srt_text.strip().split("\n\n")
    return "\n\n".join(parts[:cues])


# -- matching a result to the local file ---------------------------------------------

_BRACKETED = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_FEAT = re.compile(r"\b(?:feat|ft|featuring)\b\.?.*$", re.IGNORECASE)
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)


def normalize_name(text: str) -> str:
    """A name for comparing: NFC, case-folded, bracketed parts ("(Remastered)", "[Live]") and a trailing
    "feat. X" dropped, punctuation turned into single spaces. Letters of every script are kept."""
    value = unicodedata.normalize("NFC", text or "")
    value = _BRACKETED.sub(" ", value)
    value = _FEAT.sub("", value)
    return _NON_WORD.sub(" ", value.casefold()).strip()


def _same_artist(a: str, b: str) -> bool:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta, tb = set(na.split()), set(nb.split())
    return ta <= tb or tb <= ta  # "Depeche Mode" vs "Depeche Mode & Friends"


def _ratio(a: str, b: str) -> float:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


@dataclass
class MatchScore:
    score: float            # 0..1
    kind: str               # "exact" | "close" | "different"
    label: str              # for the user
    duration_diff: Optional[float] = None  # video minus track, seconds (positive = video longer)

    @property
    def is_exact(self) -> bool:
        return self.kind == "exact"


def _seconds_text(diff: float) -> str:
    return f"{abs(diff):.0f} s"


def score_match(
    local_artist: str, local_title: str, local_duration: float | None, candidate: Lyrics
) -> MatchScore:
    """How well `candidate` fits the local file.

    kind "exact": artist and title equal after normalisation and the lengths within 2 s.
    kind "close": same names, lengths within 6 s.
    kind "different": anything else; the label then says how much longer or shorter the video is than the
    track (a music video often has an intro or outro, and lyrics timed to the album version are then off
    by about that much) or that the title differs / a length is unknown."""
    title_equal = bool(normalize_name(local_title)) and normalize_name(local_title) == normalize_name(candidate.track_name)
    artist_equal = (not normalize_name(local_artist)) or _same_artist(local_artist, candidate.artist_name)
    name_sim = _ratio(local_title, candidate.track_name)
    if normalize_name(local_artist):
        name_sim = (name_sim + (1.0 if artist_equal else _ratio(local_artist, candidate.artist_name))) / 2

    diff: Optional[float] = None
    if local_duration and candidate.duration:
        diff = float(local_duration) - float(candidate.duration)
    if diff is None:
        dur_factor = 0.6
    elif abs(diff) <= EXACT_SECONDS:
        dur_factor = 1.0
    elif abs(diff) <= CLOSE_SECONDS:
        dur_factor = 0.85
    else:
        dur_factor = max(0.3, 1.0 - abs(diff) / 120.0)
    score = round(min(1.0, name_sim * dur_factor), 3)

    names_equal = title_equal and artist_equal
    if diff is None:
        return MatchScore(score, "different", "length unknown: sync cannot be checked", None)
    longer = "longer" if diff > 0 else "shorter"
    if names_equal and abs(diff) <= EXACT_SECONDS:
        return MatchScore(score, "exact", "exact", diff)
    if names_equal and abs(diff) <= CLOSE_SECONDS:
        return MatchScore(score, "close", f"close: video {_seconds_text(diff)} {longer} than the track", diff)
    note = f"video {_seconds_text(diff)} {longer} than the track"
    if not names_equal:
        return MatchScore(score, "different", f"different title or artist; {note}", diff)
    return MatchScore(score, "different", f"different length: {note}", diff)


@dataclass
class Match:
    lyrics: Lyrics
    score: MatchScore


def rank_matches(
    local_artist: str, local_title: str, local_duration: float | None, results: Sequence[Lyrics]
) -> list[Match]:
    """`results` scored against the local file, best first: entries with timing before plain-only ones,
    then by kind (exact, close, different), then by score. Duplicate ids are dropped."""
    order = {"exact": 0, "close": 1, "different": 2}
    seen: set = set()
    matches: list[Match] = []
    for item in results:
        if item.id is not None:
            if item.id in seen:
                continue
            seen.add(item.id)
        matches.append(Match(item, score_match(local_artist, local_title, local_duration, item)))
    matches.sort(key=lambda m: (not m.lyrics.has_timing, order[m.score.kind], -m.score.score))
    return matches


# -- names from a file ----------------------------------------------------------------

_TRACK_NUMBER = re.compile(r"^\s*\d{1,3}\s*[-._)]\s*")


def split_artist_title(stem: str) -> tuple[str, str]:
    """("Artist", "Title") from a file name without extension like "Depeche Mode - Enjoy the Silence"
    (a leading track number is dropped; en/em dashes count as the separator). ("", stem) when there is no
    separator."""
    text = " ".join((stem or "").replace("–", "-").replace("—", "-").split())
    text = _TRACK_NUMBER.sub("", text)
    if " - " in text:
        artist, title = text.split(" - ", 1)
        return artist.strip(), title.strip()
    return "", text.strip()

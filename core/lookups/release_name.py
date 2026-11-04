"""
redactor_common/core/lookups/release_name.py (from videoredactor's core/release_name_parser.py, 2026-11-04)

Turns a scene/release-style video filename stem into a search-friendly
guess: a clean title (junk tags stripped) plus, when present, a year
(movies) or season/episode number(s) (TV) -- so TMDB/TheTVDB search
gets "Inception" instead of "Inception 2010 1080p BluRay x264-GROUP",
and the episode picker can be pre-selected on the right season/episode
instead of hunting through a season's episode list by hand every time.

This is deliberately a purpose-built heuristic parser, not a full
release-name grammar (no dependency on guessit/rebulk) -- consistent
with this project's "shell out to real tools, otherwise write it
ourselves" approach, and keeps the PyInstaller build dependency-light.
It only needs to get CLOSE: the result feeds a search box the user can
still edit, and season/episode selections the user still explicitly
confirms in the picker dialogs -- never applied without confirmation,
same principle as everywhere else TMDB/TVDB data lands in this app.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import re

# --- known "junk" tokens (case-insensitive) common in scene/release names --
# These accumulate over time in this genre; the goal is "common enough to
# be worth stripping," not an exhaustive list. Stripping them matters most
# for the fallback (no year, no season/episode marker) case -- when a year
# or S/E marker IS found, everything after it is already discarded.
_RESOLUTIONS = r"(?:480|576|720|1080|1440|2160|4320)[pi]"
_SOURCES = (
    r"(?:BluRay|Blu-Ray|BRRip|BDRip|WEB[-.]?DL|WEBRip|WEB|HDTV|PDTV|"
    r"DVDRip|DVDR|HDRip|CAM|TS|TC|SDTV|UHD)"
)
_CODECS = r"(?:x264|x265|h264|h265|HEVC|AVC|XviD|DivX|AV1)"
_AUDIO = r"(?:AAC(?:2\.0|5\.1)?|AC3|DDP?5\.1|DD5\.1|DTS(?:-HD)?|TrueHD|Atmos|FLAC|MP3)"
_MISC = (
    r"(?:PROPER|REPACK|EXTENDED|UNRATED|REMASTERED|LIMITED|INTERNAL|IMAX|"
    r"HDR10?\+?|DV|10bit|8bit|HC|SUBBED|DUBBED|MULTI|NF|AMZN|DSNP|ATVP|HULU|HMAX)"
)

_JUNK_TOKEN_RE = re.compile(
    rf"\b(?:{_RESOLUTIONS}|{_SOURCES}|{_CODECS}|{_AUDIO}|{_MISC})\b",
    re.IGNORECASE,
)

# Season/episode markers, checked in this order -- S01E05 (by far the most
# common convention) first, then "1x05", then the rare spelled-out form.
_SXXEXX_RE = re.compile(r"\bS(\d{1,2})E(\d{1,3})(?:E(\d{1,3}))?\b", re.IGNORECASE)
_NxNN_RE = re.compile(r"\b(\d{1,2})x(\d{1,3})\b", re.IGNORECASE)
_SEASON_EPISODE_WORDS_RE = re.compile(
    r"\bSeason\s*(\d{1,2})\s*Episode\s*(\d{1,3})\b", re.IGNORECASE
)

_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")

# A trailing "-GROUP" release-group tag: a dash immediately followed by a
# single alnum "word" (no spaces) at the very end of the string, e.g.
# "-RARBG", "-SPARKS", "-YIFY". Stripped up front so it never gets
# mistaken for part of a title or an episode-title tail.
_TRAILING_GROUP_RE = re.compile(r"-[A-Za-z0-9]+$")


@dataclass
class ReleaseGuess:
    kind: str  # 'movie', 'tv', or 'unknown'
    title: str  # cleaned title, ready to hand to a search box
    year: Optional[str] = None            # movies: 'YYYY' if found
    season: Optional[int] = None          # tv: season number if found
    episode: Optional[int] = None         # tv: first episode number if found
    extra_episodes: list[int] = field(default_factory=list)
    # tv: additional episode numbers for a multi-episode file (S01E05E06)


def _normalize_separators(text: str) -> str:
    """Scene names use '.' and '_' as word separators (and sometimes a
    run of spaces already); brackets/parens are typically used to wrap a
    tag rather than as part of a title (e.g. "Parasite (2019) [1080p]").
    Collapse all of that to single spaces."""
    for ch in "._()[]{}":
        text = text.replace(ch, " ")
    return re.sub(r"\s+", " ", text).strip()


def _strip_junk_tags(text: str) -> str:
    """Remove known resolution/source/codec/audio/misc release tags."""
    text = _JUNK_TOKEN_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_release_name(filename_stem: str) -> ReleaseGuess:
    """Best-effort guess at title (+ year, or + season/episode) from a
    video filename stem. Always returns a ReleaseGuess -- worst case
    `kind='unknown'` with a lightly-cleaned title, never raises. The
    caller (search dialogs) treats this as a starting point the user
    can still edit, not a final answer -- consistent with this
    project's "never auto-apply, always let the user confirm" rule.
    """
    stem = _normalize_separators(filename_stem or "")
    stem = _TRAILING_GROUP_RE.sub("", stem).strip()

    # --- TV: a season/episode marker wins if present -- everything
    # before it is the show title, everything after (episode title,
    # quality tags, group) is discarded rather than guessed at.
    for regex in (_SXXEXX_RE, _NxNN_RE, _SEASON_EPISODE_WORDS_RE):
        m = regex.search(stem)
        if m:
            season = int(m.group(1))
            episode = int(m.group(2))
            extra_episodes = []
            if regex is _SXXEXX_RE and m.group(3):
                extra_episodes.append(int(m.group(3)))
            title = _strip_junk_tags(stem[:m.start()].strip(" -_")).strip(" -_")
            return ReleaseGuess(
                kind="tv", title=title or stem, season=season, episode=episode,
                extra_episodes=extra_episodes,
            )

    # --- Movie: a bounded year token, if present -- everything before
    # it is the title, the year itself narrows the TMDB search.
    m = _YEAR_RE.search(stem)
    if m:
        title = _strip_junk_tags(stem[:m.start()].strip(" -_")).strip(" -_")
        if title:
            return ReleaseGuess(kind="movie", title=title, year=m.group(1))

    # --- Fallback: no year or season/episode marker found. Strip what
    # junk tags we can still recognize and hand back what's left,
    # unclassified -- better than nothing for the search box.
    title = _strip_junk_tags(stem).strip(" -_")
    return ReleaseGuess(kind="unknown", title=title or stem)

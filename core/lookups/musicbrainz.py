"""
redactor_common/core/lookups/musicbrainz.py (from mp3redactor's core/musicbrainz_lookup.py, 2026-11-04)

Finds the MusicBrainz release (album) a folder of MP3s belongs to, and
maps each file to its track on it -- "album first": search releases by
the folder's artist + album tags (or its folder name), then score each
candidate's track list against the files by track position, title and
duration. Built on a 2026-09-28 probe with the maintainer's own files:
two tracks of "Ring of Fire: The Legend of Johnny Cash" matched all three
editions of that compilation exactly this way, whereas searching song by
song failed outright -- a famous song has hundreds of recordings
("Big River": 267). Identifying files with junk tags by their audio
(AcoustID fingerprints) is a planned second stage.

MusicBrainz web service rules followed here
(https://musicbrainz.org/doc/MusicBrainz_API): a meaningful User-Agent
naming the app (each app calls configure() once), and no more than about
one request per second -- every request goes through one process-wide
throttle, whichever app module makes it.

Written tags follow MusicBrainz Picard's conventions, which other
taggers read too: the release's id as "MusicBrainz Album Id", each
track's recording id as "MusicBrainz Track Id". Qt-free.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote, urlencode

from redactor_common.core.lookup_client import Throttle, fetch_bytes, fetch_json, make_default_fetch, throttled

API_BASE = "https://musicbrainz.org/ws/2"
COVER_ART_BASE = "https://coverartarchive.org"
USER_AGENT = "redactor_common ( https://github.com/Erlbon/redactor_common )"  # replaced by the app's configure()
MIN_REQUEST_INTERVAL = 1.1  # seconds; MusicBrainz asks for at most ~1 request/second
_SOURCE_NAME = "MusicBrainz"


class MusicBrainzError(Exception):
    """Searching or fetching from MusicBrainz failed."""


# One process-wide pace for all MusicBrainz requests, whichever thread makes them.
_throttle = Throttle(MIN_REQUEST_INTERVAL)
_raw_fetch = make_default_fetch(USER_AGENT, timeout=30.0)
_default_fetch = throttled(_raw_fetch, _throttle)


def configure(user_agent: str) -> None:
    """The app's User-Agent ("mp3redactor/1.0 ( https://github.com/Erlbon/mp3redactor )"), set once at import of the
    app's own lookup setup. The pace stays the one process-wide throttle."""
    global USER_AGENT, _raw_fetch, _default_fetch
    USER_AGENT = user_agent
    _raw_fetch = make_default_fetch(user_agent, timeout=30.0)
    _default_fetch = throttled(_raw_fetch, _throttle)


def _get(path: str, fetch: Callable, **params) -> dict:
    params["fmt"] = "json"
    url = f"{API_BASE}/{path}?{urlencode(params, quote_via=quote)}"
    return fetch_json(
        url, fetch, error_cls=MusicBrainzError, source_name=_SOURCE_NAME,
        status_messages={503: "MusicBrainz is busy (rate limit) -- try again in a moment."},
    ) or {}


# ---------------------------------------------------------------------------
# What we know about the folder
# ---------------------------------------------------------------------------

@dataclass
class FileFacts:
    """One file as the matcher sees it."""

    title: str = ""
    track: Optional[int] = None
    disc: Optional[int] = None
    seconds: Optional[float] = None
    # MusicBrainz recording id -> AcoustID score, when the file was
    # fingerprinted (mp3redactor's core/acoustid_lookup.py); empty otherwise.
    recordings: dict[str, float] = field(default_factory=dict)


@dataclass
class AlbumQuery:
    artist: str = ""
    album: str = ""


def _leading_int(text: str) -> Optional[int]:
    match = re.match(r"\s*(\d+)", text or "")
    return int(match.group(1)) if match else None


def _most_common(values) -> str:
    values = [v.strip() for v in values if v and v.strip()]
    return Counter(values).most_common(1)[0][0] if values else ""


_FOLDER_RE = re.compile(r"^(?P<artist>.+?)\s+-\s+(?P<album>.+?)(?:\s*[\(\[]\d{4}[\)\]])?\s*$")


def album_query(album_tags: list[str], artist_tags: list[str], folder: Path) -> AlbumQuery:
    """The search for a folder: its most common album and (album) artist
    tags, falling back to an "Artist - Album (Year)" folder name."""
    query = AlbumQuery(artist=_most_common(artist_tags), album=_most_common(album_tags))
    if not query.album or not query.artist:
        match = _FOLDER_RE.match(folder.name)
        if match:
            query.artist = query.artist or match.group("artist").strip()
            query.album = query.album or match.group("album").strip()
        elif not query.album:
            query.album = folder.name.strip()
    return query


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------

@dataclass
class ReleaseTrack:
    disc: int
    position: int
    title: str
    seconds: Optional[float]
    recording_id: str
    artist: str


@dataclass
class Release:
    id: str
    title: str
    artist: str
    date: str = ""
    country: str = ""
    status: str = ""
    disambiguation: str = ""
    media_formats: str = ""
    track_count: int = 0
    tracks: list[ReleaseTrack] = field(default_factory=list)

    @property
    def disc_count(self) -> int:
        return len({t.disc for t in self.tracks}) or 1

    def label(self) -> str:
        bits = [f"{self.artist} - {self.title}"]
        if self.disambiguation:
            bits.append(f"({self.disambiguation})")
        details = ", ".join(b for b in (self.date, self.country, self.media_formats, f"{self.track_count} tracks") if b)
        return " ".join(bits) + f"  [{details}]"


def _credit(artist_credit) -> str:
    return "".join(f"{c.get('name', '')}{c.get('joinphrase', '')}" for c in artist_credit or [])


def _lucene(text: str) -> str:
    """Quoted Lucene phrase for MusicBrainz's search syntax."""
    return '"' + re.sub(r'(["\\])', r"\\\1", text) + '"'


def search_releases(query: AlbumQuery, fetch: Optional[Callable] = None, limit: int = 10) -> list[Release]:
    """Release summaries (no track lists) for the album query; with an
    artist first, then -- if that finds nothing -- by album alone."""
    fetch = fetch or _default_fetch
    if not query.album:
        raise MusicBrainzError("needs an album name (Album tag or an 'Artist - Album' folder name)")
    searches = []
    if query.artist:
        searches.append(f"release:{_lucene(query.album)} AND artist:{_lucene(query.artist)}")
    searches.append(f"release:{_lucene(query.album)}")
    for lucene in searches:
        data = _get("release", fetch, query=lucene, limit=limit)
        releases = [
            Release(
                id=r["id"], title=r.get("title") or "", artist=_credit(r.get("artist-credit")),
                date=r.get("date") or "", country=r.get("country") or "", status=r.get("status") or "",
                disambiguation=r.get("disambiguation") or "",
                media_formats=", ".join(dict.fromkeys(m.get("format", "") for m in r.get("media", []) if m.get("format"))),
                track_count=r.get("track-count", 0) or 0,
            )
            for r in data.get("releases", [])
            if r.get("id")
        ]
        if releases:
            return releases
    return []


def fetch_release(release_id: str, fetch: Optional[Callable] = None) -> Release:
    """A release with its full track list (recordings, lengths, artists)."""
    fetch = fetch or _default_fetch
    r = _get(f"release/{release_id}", fetch, inc="recordings+artist-credits")
    release = Release(
        id=r.get("id", release_id), title=r.get("title") or "", artist=_credit(r.get("artist-credit")),
        date=r.get("date") or "", country=r.get("country") or "", status=r.get("status") or "",
        disambiguation=r.get("disambiguation") or "",
    )
    formats = []
    for medium in r.get("media", []):
        disc = medium.get("position") or 1
        if medium.get("format"):
            formats.append(medium["format"])
        for t in medium.get("tracks", []):
            length = t.get("length") or (t.get("recording") or {}).get("length")
            release.tracks.append(ReleaseTrack(
                disc=disc,
                position=t.get("position") or _leading_int(str(t.get("number", ""))) or 0,
                title=t.get("title", "") or (t.get("recording") or {}).get("title", ""),
                seconds=length / 1000 if length else None,
                recording_id=(t.get("recording") or {}).get("id", ""),
                artist=_credit(t.get("artist-credit")) or release.artist,
            ))
    release.media_formats = ", ".join(dict.fromkeys(formats))
    release.track_count = len(release.tracks)
    return release


def fetch_front_cover(release_id: str, fetch: Optional[Callable] = None, size: int = 250) -> Optional[bytes]:
    """The release's front cover (`size` px: 250, 500 or 1200) from the
    Cover Art Archive, or None. The lookup dialog only previews the
    default 250; mp3redactor's Redact embeds a 500."""
    fetch = fetch or _raw_fetch
    try:
        return fetch_bytes(f"{COVER_ART_BASE}/release/{release_id}/front-{size}", fetch, error_cls=MusicBrainzError)
    except MusicBrainzError:
        return None


# ---------------------------------------------------------------------------
# Matching files to a release's tracks
# ---------------------------------------------------------------------------

def _norm_title(text: str) -> str:
    return re.sub(r"[^0-9a-z]+", " ", (text or "").casefold()).strip()


def _title_similarity(a: str, b: str) -> float:
    a, b = _norm_title(a), _norm_title(b)
    if not a or not b:
        return 0.0
    return 1.0 if a == b else difflib.SequenceMatcher(None, a, b).ratio()


def _duration_score(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    gap = abs(a - b)
    return 1.0 if gap <= 3 else 0.6 if gap <= 10 else 0.0


def _pair_score(f: FileFacts, t: ReleaseTrack, single_disc: bool) -> float:
    # The file's own sound identifies this very recording: certain,
    # whatever its (possibly junk) tags say.
    if t.recording_id and t.recording_id in f.recordings:
        return 1.0
    parts, weights = [], []
    parts.append(_title_similarity(f.title, t.title))
    weights.append(0.45)
    duration = _duration_score(f.seconds, t.seconds)
    if duration is not None:
        parts.append(duration)
        weights.append(0.35)
    if f.track is not None:
        same_disc = (f.disc or 1) == t.disc or (single_disc and f.disc is None)
        parts.append(1.0 if f.track == t.position and same_disc else 0.0)
        weights.append(0.2)
    return sum(p * w for p, w in zip(parts, weights)) / sum(weights)


@dataclass
class ReleaseMatch:
    release: Release
    score: float  # 0..1: average per-file match quality
    assignment: dict[int, ReleaseTrack]  # file index -> its track on the release
    matched: int  # files assigned with a good score

    def summary(self) -> str:
        return f"{self.matched} of {len(self.assignment) or self.matched} files matched, {round(self.score * 100)}%"


GOOD_PAIR = 0.6


def match_release(files: list[FileFacts], release: Release) -> ReleaseMatch:
    """Assigns each file to its best track (greedy, best pairs first,
    each track used once) and scores the whole folder against the
    release. A file with no good partner stays unassigned."""
    single_disc = release.disc_count == 1
    pairs = sorted(
        ((_pair_score(f, t, single_disc), fi, ti) for fi, f in enumerate(files) for ti, t in enumerate(release.tracks)),
        reverse=True,
    )
    used_files, used_tracks, assignment, scores = set(), set(), {}, {}
    for score, fi, ti in pairs:
        if fi in used_files or ti in used_tracks or score < GOOD_PAIR:
            continue
        used_files.add(fi)
        used_tracks.add(ti)
        assignment[fi] = release.tracks[ti]
        scores[fi] = score
    total = sum(scores.values()) / len(files) if files else 0.0
    return ReleaseMatch(release=release, score=total, assignment=assignment, matched=len(assignment))


def rank_matches(matches: list[ReleaseMatch], file_count: int) -> list[ReleaseMatch]:
    """Best match first: overall score, then official releases, then a
    track count equal to the folder's (a whole album), then oldest."""
    def key(m: ReleaseMatch):
        return (
            -round(m.score, 3),
            m.release.status.casefold() != "official",
            m.release.track_count != file_count,
            m.release.date or "9999",
        )
    return sorted(matches, key=key)


def fields_for(release: Release, track: ReleaseTrack) -> dict[str, str]:
    """Tag field values (mp3redactor's core/fields.py keys) for one matched file."""
    fields = {
        "title": track.title,
        "artist": track.artist,
        "albumartist": release.artist,
        "album": release.title,
        "track": str(track.position) if track.position else "",  # 0 = unknown
        "year": release.date[:4],
        "musicbrainz_albumid": release.id,
        "musicbrainz_trackid": track.recording_id,
    }
    if release.disc_count > 1:
        if track.disc:
            fields["discnumber"] = str(track.disc)
    return {k: v for k, v in fields.items() if v}


def find_album_by_recordings(
    files: list[FileFacts], release_ids: list[str], fetch: Optional[Callable] = None,
) -> list[ReleaseMatch]:
    """Stage 2: the releases AcoustID found for the folder's fingerprints
    (acoustid_lookup.candidate_releases), fetched and matched like the
    text search's -- ranked, best first."""
    fetch = fetch or _default_fetch
    matches = [match_release(files, fetch_release(release_id, fetch)) for release_id in release_ids]
    return rank_matches(matches, len(files))


def facts_from_tags(title: str, track: str, disc: str, seconds: Optional[float]) -> FileFacts:
    return FileFacts(title=title, track=_leading_int(track), disc=_leading_int(disc), seconds=seconds)


def find_album(
    files: list[FileFacts], query: AlbumQuery, fetch: Optional[Callable] = None, candidates: int = 4,
) -> list[ReleaseMatch]:
    """Searches, fetches the track lists of the top `candidates` releases
    (one request each -- MusicBrainz's pace makes this the costly part),
    and returns them ranked, best first."""
    fetch = fetch or _default_fetch
    summaries = search_releases(query, fetch)
    # Prefer summaries whose track count could hold the whole folder.
    summaries.sort(key=lambda r: (r.track_count < len(files), r.status.casefold() != "official"))
    matches = [match_release(files, fetch_release(r.id, fetch)) for r in summaries[:candidates]]
    return rank_matches(matches, len(files))

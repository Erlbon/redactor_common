"""
redactor_common/core/lookups/discogs.py (from mp3redactor's core/discogs_lookup.py, 2026-11-04)

Finds the Discogs release (album) a folder of MP3s belongs to, and maps
each file to its track on it -- the same "album first" shape as
lookups/musicbrainz.py: search releases by the folder's artist +
album (+ year) tags, or its "Artist - Album (Year)" folder name, fetch
the best few candidates' full release (track list, label, catalogue
number, genres/styles) and pair each file with a track by track number,
then by title. Discogs adds what MusicBrainz is thin on for physical
releases: the label, catalogue number, country and style tags.

Discogs API rules followed here (https://www.discogs.com/developers):
  - every request carries `Authorization: Discogs token=<token>` and a
    descriptive User-Agent;
  - at most 60 requests a minute with a token -- every request goes through
    one pace (MIN_REQUEST_INTERVAL), and an HTTP 429 stops all further
    requests of that client (DiscogsRateLimited) instead of hammering on;
  - data shown from Discogs carries a "Data from Discogs" attribution (see
    mp3redactor's gui/discogs_lookup_dialog.py).

The token is never put in a URL, a log line or an exception message: it
travels only in the Authorization header, and every error text is scrubbed
of it before it leaves this module. It is stored by redactor_common's
secret store as the family's shared key "discogs_token" (the app's own old
entry, configure(), is the fallback while the apps move over); see load_token().
A cover image is fetched with the token only from a discogs.com host, and a
redirect away from discogs.com drops the header.

Written from Discogs' documented response shapes, defensively: a missing
or oddly typed field reads as "nothing there", never as a crash. Qt-free;
every network call goes through an injectable `fetch` (tests use mocked
replies only).
"""

from __future__ import annotations

import difflib
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from redactor_common.core import secret_store
from redactor_common.core.local_db import normalize_words
from redactor_common.core.lookup_client import (
    Throttle, build_request, fetch_bytes, fetch_json, make_default_fetch, redact_text,
)

from redactor_common.core.lookups.musicbrainz import FileFacts, album_query

API_BASE = "https://api.discogs.com"
USER_AGENT = "redactor_common +https://github.com/Erlbon/redactor_common"  # replaced by the app's configure()
MIN_REQUEST_INTERVAL = 1.1  # seconds; 60 requests/minute with a token
SECRET_APP = ""  # the app whose credential entry holds the token, set by configure(); "" = the environment only
SECRET_NAME = "discogs_token"  # permanent: the name of the OS credential entry
TOKEN_ENV_VAR = "DISCOGS_TOKEN"
TOKEN_HELP_URL = "https://www.discogs.com/settings/developers"
TOKEN_HELP = "Set a Discogs token in Tools > API Keys (create one under Discogs Settings > Developers)."
MAX_COVER_BYTES = 8 * 1024 * 1024
GENRE_SEPARATOR = "; "  # between a file's Discogs genres and styles in the Genre tag
_SOURCE_NAME = "Discogs"
_TIMEOUT = 30.0
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
RATE_LIMIT_MESSAGE = "Discogs says too many requests were made (HTTP 429) -- wait a minute and try again."


class DiscogsError(Exception):
    """Searching or fetching from Discogs failed."""


class DiscogsRateLimited(DiscogsError):
    """Discogs answered HTTP 429; the client makes no further requests."""


def configure(user_agent: str, secret_app: str) -> None:
    """The app's User-Agent and the app name its token is stored under (secret_store), set once by the app."""
    global USER_AGENT, SECRET_APP
    USER_AGENT, SECRET_APP = user_agent, secret_app


def load_token() -> str:
    """The stored token ("" if none). DISCOGS_TOKEN in the environment wins, then the family's shared entry, then the
    app's own old entry (secret_store.get_key: the OS credential store, or the opt-in fallback file)."""
    if not SECRET_APP:
        return os.environ.get(TOKEN_ENV_VAR, "").strip()
    return secret_store.get_key(SECRET_NAME, SECRET_APP, env_var=TOKEN_ENV_VAR).strip()


def has_token() -> bool:
    return bool(load_token())


# ---------------------------------------------------------------------------
# Transport: pacing, headers, safe redirects
# ---------------------------------------------------------------------------

# Throttle is redactor_common's (re-exported here: tests and redact_steps
# pass a Throttle(interval, clock=, sleep=) of their own).
_shared_throttle = Throttle(MIN_REQUEST_INTERVAL)  # one pace for the whole process


def is_discogs_url(url: str) -> bool:
    """An https URL on discogs.com or one of its subdomains (api., i.)."""
    parts = urllib.parse.urlsplit(url or "")
    host = (parts.hostname or "").lower()
    return parts.scheme.lower() == "https" and (host == "discogs.com" or host.endswith(".discogs.com"))


class _TokenOnlyToDiscogs(urllib.request.HTTPRedirectHandler):
    """Follows redirects, but only over http(s), and never carries the
    Authorization header to a host that is not discogs.com."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme.lower() not in ("http", "https"):
            return None
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and not is_discogs_url(newurl):
            new.remove_header("Authorization")
        return new


def _make_default_fetch(timeout: float = _TIMEOUT) -> Callable:
    return make_default_fetch(
        USER_AGENT, timeout=timeout, opener=urllib.request.build_opener(_TokenOnlyToDiscogs),
        max_bytes=_MAX_RESPONSE_BYTES,
    )


class DiscogsClient:
    """Makes the paced, authenticated requests of one run or dialog.
    Once Discogs answers 429, `rate_limited` is set and every later call
    raises DiscogsRateLimited without touching the network."""

    def __init__(self, token: str, fetch: Optional[Callable] = None, throttle: Optional[Throttle] = None):
        self._token = (token or "").strip()
        self._fetch = fetch or _make_default_fetch()
        self._throttle = throttle or _shared_throttle
        self.rate_limited = False

    def __repr__(self) -> str:  # never the token
        return "DiscogsClient()"

    def _headers(self, url: str) -> dict[str, str]:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.discogs.v2.discogs+json"}
        if is_discogs_url(url):  # an image URL from a reply must never collect the token elsewhere
            headers["Authorization"] = f"Discogs token={self._token}"
        return headers

    def _call(self, run):
        if self.rate_limited:
            raise DiscogsRateLimited(RATE_LIMIT_MESSAGE)
        self._throttle.wait()
        # The shared fetch_json/fetch_bytes already redact the token (secrets=);
        # re-raising "from None" also drops the chained original exception,
        # whose own text could still carry it.
        try:
            return run()
        except DiscogsRateLimited:
            self.rate_limited = True
            raise DiscogsRateLimited(RATE_LIMIT_MESSAGE) from None
        except DiscogsError as exc:
            raise DiscogsError(redact_text(str(exc), (self._token,))) from None

    def _http_options(self) -> dict:
        """What every request passes to the shared fetch_json/fetch_bytes: an
        HTTP 429 becomes DiscogsRateLimited, and the token never appears in
        an error message."""
        return {"status_errors": {429: DiscogsRateLimited}, "secrets": (self._token,)}

    def get_json(self, path: str, **params) -> dict:
        url = f"{API_BASE}/{path}"
        request = build_request(url, params or None, self._headers(url))

        def run():
            data = fetch_json(
                request, self._fetch, error_cls=DiscogsError, source_name=_SOURCE_NAME,
                status_messages={
                    401: "Discogs rejected the token (HTTP 401) -- check it in Tools > API Keys.",
                    403: "Discogs refused the request (HTTP 403) -- check the token in Tools > API Keys.",
                    429: RATE_LIMIT_MESSAGE,
                },
                **self._http_options(),
            )
            return data if isinstance(data, dict) else {}

        return self._call(run)

    def get_image(self, url: str, max_bytes: int = MAX_COVER_BYTES) -> Optional[bytes]:
        """An image from a reply (a cover), or None: not an https URL,
        too big, or unreachable. The token goes along only to discogs.com."""
        if not url or urllib.parse.urlsplit(url).scheme.lower() != "https":
            return None
        request = build_request(url, None, self._headers(url))

        def run():
            return fetch_bytes(
                request, self._fetch, error_cls=DiscogsError, what="the cover image",
                status_messages={429: RATE_LIMIT_MESSAGE}, **self._http_options(),
            )

        try:
            data = self._call(run)
        except DiscogsRateLimited:
            raise
        except DiscogsError:
            return None
        return data if data and len(data) <= max_bytes else None


# ---------------------------------------------------------------------------
# What we know about the folder
# ---------------------------------------------------------------------------

@dataclass
class DiscogsQuery:
    artist: str = ""
    album: str = ""
    year: str = ""
    track: str = ""  # only used to search when there is no album name at all


_YEAR_IN_FOLDER_RE = re.compile(r"[\(\[](\d{4})[\)\]]\s*$")


def _four_digits(text: str) -> str:
    match = re.match(r"\s*(\d{4})", text or "")
    return match.group(1) if match else ""


def discogs_query(
    album_tags: list[str], artist_tags: list[str], year_tags: list[str], folder: Path, title_tags: Optional[list[str]] = None,
) -> DiscogsQuery:
    """The search for a folder: its most common album / (album) artist / year
    tags, with "Artist - Album (Year)" from the folder name filling what
    the tags lack (same rules as the MusicBrainz lookup's album_query)."""
    base = album_query(album_tags, artist_tags, folder)
    years = [_four_digits(y) for y in year_tags if _four_digits(y)]
    year = max(set(years), key=years.count) if years else ""
    if not year:
        match = _YEAR_IN_FOLDER_RE.search(folder.name)
        year = match.group(1) if match else ""
    titles = [t.strip() for t in (title_tags or []) if t and t.strip()]
    return DiscogsQuery(artist=base.artist, album=base.album, year=year, track=titles[0] if titles else "")


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

_DISAMBIGUATION_RE = re.compile(r"\s*\(\d+\)\s*$")


def clean_artist_name(name: str) -> str:
    """Discogs tells two artists of one name apart with a number ("Nirvana
    (2)"); that is not part of the name. "Various" is "Various Artists"."""
    name = _DISAMBIGUATION_RE.sub("", str(name or "")).strip()
    return "Various Artists" if name.casefold() == "various" else name


def join_artists(artists) -> str:
    """"A & B", "A, B" from Discogs' artists list ({name, anv, join})."""
    text = ""
    for entry in artists if isinstance(artists, list) else []:
        if not isinstance(entry, dict):
            continue
        name = clean_artist_name(entry.get("name", ""))
        if not name:
            continue
        text += name
        joiner = str(entry.get("join") or "").strip()
        if joiner == ",":
            text += ", "
        elif joiner:
            text += f" {joiner} "
    return text.strip().rstrip(",&").strip()


def _str(value) -> str:
    return value.strip() if isinstance(value, str) else (str(value) if isinstance(value, (int, float)) and value else "")


def _str_list(value) -> list[str]:
    return [v.strip() for v in value if isinstance(v, str) and v.strip()] if isinstance(value, list) else []


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    """One search hit -- a release summary, before its full track list."""

    id: int
    artist: str
    album: str
    year: str = ""
    country: str = ""
    label: str = ""
    catno: str = ""
    formats: str = ""
    score: float = 0.0

    def label_text(self) -> str:
        bits = [f"{self.artist} - {self.album}" if self.artist else self.album]
        details = ", ".join(b for b in (self.year, self.country, self.label, self.catno, self.formats) if b)
        return " ".join(bits) + (f"  [{details}]" if details else "")


def _split_title(title: str) -> tuple[str, str]:
    """Discogs search titles read "Artist - Album"."""
    artist, sep, album = title.partition(" - ")
    return (clean_artist_name(artist), album.strip()) if sep else ("", title.strip())


def _similar(a: str, b: str) -> Optional[float]:
    a, b = normalize_words(a), normalize_words(b)
    if not a or not b:
        return None
    return 1.0 if a == b else difflib.SequenceMatcher(None, a, b).ratio()


def _year_score(query_year: str, year: str) -> Optional[float]:
    if not (query_year.isdigit() and year.isdigit()):
        return None
    gap = abs(int(query_year) - int(year))
    return 1.0 if gap == 0 else 0.8 if gap == 1 else 0.5 if gap <= 3 else 0.2


def _count_score(wanted: Optional[int], found: Optional[int]) -> Optional[float]:
    if not wanted or not found:
        return None
    gap = abs(wanted - found)
    return 1.0 if gap == 0 else 0.7 if gap == 1 else 0.4 if gap <= 3 else 0.1


def score_candidate(
    query: DiscogsQuery, artist: str, album: str, year: str = "",
    track_count: Optional[int] = None, file_count: Optional[int] = None,
) -> float:
    """0..1: how well a release fits the query -- normalised artist and album
    (weight .4 each), year closeness (.1) and, once the track list is known,
    how close its track count is to the folder's (.1). A part the query or
    the release can't supply is left out rather than counted against it."""
    weighted = [
        (_similar(query.artist, artist), 0.4),
        (_similar(query.album, album), 0.4),
        (_year_score(query.year, year), 0.1),
        (_count_score(file_count, track_count), 0.1),
    ]
    weighted = [(score, weight) for score, weight in weighted if score is not None]
    if not weighted:
        return 0.0
    return sum(s * w for s, w in weighted) / sum(w for _s, w in weighted)


def _search_attempts(query: DiscogsQuery, limit: int) -> list[dict]:
    base = {"type": "release", "per_page": limit}
    attempts = []
    if query.album:
        exact = dict(base, release_title=query.album)
        if query.artist:
            exact["artist"] = query.artist
        if query.year:
            attempts.append(dict(exact, year=query.year))
        attempts.append(exact)
        attempts.append(dict(base, q=f"{query.artist} {query.album}".strip()))
    else:
        attempts.append(dict(base, artist=query.artist, track=query.track))
    return attempts


def parse_candidates(data: dict, query: DiscogsQuery) -> list[Candidate]:
    candidates = []
    results = data.get("results") if isinstance(data, dict) else None
    for r in results if isinstance(results, list) else []:
        if not isinstance(r, dict) or not isinstance(r.get("id"), int):
            continue
        artist, album = _split_title(_str(r.get("title")))
        labels = _str_list(r.get("label"))
        candidate = Candidate(
            id=r["id"], artist=artist, album=album, year=_str(r.get("year")), country=_str(r.get("country")),
            label=labels[0] if labels else "", catno=_clean_catno(r.get("catno")),
            formats=", ".join(dict.fromkeys(_str_list(r.get("format")))),
        )
        candidate.score = score_candidate(query, artist, album, candidate.year)
        candidates.append(candidate)
    return candidates


def search_releases(client: DiscogsClient, query: DiscogsQuery, limit: int = 10) -> list[Candidate]:
    """Release summaries for the query, best first: artist + album (+ year),
    then without the year, then as free text; an album-less query searches
    artist + track. Returns the first attempt that finds anything."""
    if not query.album and not (query.artist and query.track):
        raise DiscogsError("needs an album name (Album tag or an 'Artist - Album' folder name)")
    for params in _search_attempts(query, limit):
        candidates = parse_candidates(client.get_json("database/search", **params), query)
        if candidates:
            return sorted(candidates, key=lambda c: -c.score)
    return []


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------

@dataclass
class DiscogsTrack:
    position: str  # as Discogs lists it: "A1", "2-3", "7"
    disc: int
    number: int
    title: str
    artist: str
    duration: str = ""  # as Discogs writes it ("3:45"), "" when not given


@dataclass
class DiscogsRelease:
    id: int
    title: str
    artist: str
    year: str = ""
    country: str = ""
    label: str = ""
    catno: str = ""
    genres: list[str] = field(default_factory=list)
    styles: list[str] = field(default_factory=list)
    tracks: list[DiscogsTrack] = field(default_factory=list)
    cover_url: str = ""
    cover_fallbacks: list[str] = field(default_factory=list)  # other sizes of the same image, tried if the first fails
    uri: str = ""
    released: str = ""  # Discogs' own release date text ("1980-07-25", "1980-07", "1980")

    @property
    def track_count(self) -> int:
        return len(self.tracks)

    @property
    def disc_count(self) -> int:
        return len({t.disc for t in self.tracks}) or 1

    def label_text(self) -> str:
        details = ", ".join(b for b in (
            self.year, self.country, " ".join(b for b in (self.label, self.catno) if b), f"{self.track_count} tracks",
        ) if b)
        return f"{self.artist} - {self.title}  [{details}]"


def _clean_catno(value) -> str:
    text = _str(value)
    return "" if text.casefold() == "none" else text  # Discogs writes "none" for no number


_DISC_TRACK_RE = re.compile(r"^(?:cd|dvd|disc|disk|lp|mc|cass(?:ette)?|vinyl)?\s*(\d+)\s*[-.:/]\s*(\d+)$", re.IGNORECASE)
_PLAIN_RE = re.compile(r"^(\d+)$")


def number_positions(positions: list[str]) -> list[tuple[Optional[int], int]]:
    """(disc, track number) for each of a release's track positions, in order.
    "1-3", "2.4" and "CD1-5" carry a disc; plain numbers are the track
    number; anything else -- vinyl sides like "A1", "B2" -- is numbered
    1, 2, 3... down the whole release (no disc). A release whose numbers
    would repeat or contain 0 is numbered sequentially too, rather than
    guessed at."""
    count = len(positions)
    sequential: list[tuple[Optional[int], int]] = [(None, i + 1) for i in range(count)]
    stripped = [p.strip() for p in positions]
    matches = [_DISC_TRACK_RE.match(p) for p in stripped]
    if count and all(matches):
        pairs = [(int(m.group(1)), int(m.group(2))) for m in matches]
        return list(pairs) if len(set(pairs)) == count and all(d and t for d, t in pairs) else sequential
    plain = [_PLAIN_RE.match(p) for p in stripped]
    if count and all(plain):
        numbers = [int(m.group(1)) for m in plain]
        return [(None, n) for n in numbers] if len(set(numbers)) == count and all(numbers) else sequential
    return sequential


def _flatten_tracklist(tracklist) -> list[dict]:
    """The real tracks of a tracklist: headings are dropped and an index
    track ("Symphony" with movements) is replaced by its sub-tracks."""
    out = []
    for item in tracklist if isinstance(tracklist, list) else []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type_", "track")
        if kind == "heading":
            continue
        if kind == "index" and isinstance(item.get("sub_tracks"), list):
            out.extend(s for s in item["sub_tracks"] if isinstance(s, dict) and s.get("type_", "track") == "track")
        elif kind in ("track", "index"):
            out.append(item)
    return [t for t in out if _str(t.get("title"))]


def parse_release(data: dict, release_id: int = 0) -> DiscogsRelease:
    """A GET /releases/{id} reply as a DiscogsRelease."""
    data = data if isinstance(data, dict) else {}
    artist = join_artists(data.get("artists"))
    labels = [l for l in data.get("labels", []) if isinstance(l, dict)] if isinstance(data.get("labels"), list) else []
    release = DiscogsRelease(
        id=data.get("id") if isinstance(data.get("id"), int) else release_id,
        title=_str(data.get("title")),
        artist=artist,
        year=_str(data.get("year")),
        country=_str(data.get("country")),
        label=_str(labels[0].get("name")) if labels else "",
        catno=_clean_catno(labels[0].get("catno")) if labels else "",
        genres=_str_list(data.get("genres")),
        styles=_str_list(data.get("styles")),
        uri=_str(data.get("uri")),
        released=_str(data.get("released")),
    )
    images = [i for i in data.get("images", []) if isinstance(i, dict)] if isinstance(data.get("images"), list) else []
    images.sort(key=lambda i: i.get("type") != "primary")  # the primary image first (stable)
    for image in images:
        urls = [u for u in (_str(image.get(k)) for k in ("uri", "resource_url", "uri150")) if u]
        if urls:
            release.cover_url, release.cover_fallbacks = urls[0], urls[1:]
            break
    tracks = _flatten_tracklist(data.get("tracklist"))
    numbering = number_positions([_str(t.get("position")) for t in tracks])
    for raw, (disc, number) in zip(tracks, numbering):
        release.tracks.append(DiscogsTrack(
            position=_str(raw.get("position")), disc=disc or 1, number=number,
            title=_str(raw.get("title")), artist=join_artists(raw.get("artists")) or artist,
            duration=_str(raw.get("duration")),
        ))
    return release


def fetch_release(client: DiscogsClient, release_id: int, cache: Optional[dict] = None) -> DiscogsRelease:
    """The full release; with a `cache` dict (id -> release) each id is fetched once."""
    if cache is not None and release_id in cache:
        return cache[release_id]
    release = parse_release(client.get_json(f"releases/{release_id}"), release_id)
    if cache is not None:
        cache[release_id] = release
    return release


def fetch_cover(client: DiscogsClient, release: DiscogsRelease) -> Optional[bytes]:
    """The release's main image (size-capped), or None. Only called where a
    cover is actually wanted -- never as part of a plain search."""
    for url in [release.cover_url, *release.cover_fallbacks]:
        if url:
            data = client.get_image(url)
            if data:
                return data
    return None


# ---------------------------------------------------------------------------
# Matching files to a release's tracks
# ---------------------------------------------------------------------------

def assign_tracks(files: list[FileFacts], release: DiscogsRelease) -> dict[int, DiscogsTrack]:
    """file index -> its track: by track number (and disc, on a multi-disc
    release), then, for files left over, by exact normalised title. Each
    track is used once."""
    multi = release.disc_count > 1
    by_number = {(t.disc if multi else 1, t.number): i for i, t in enumerate(release.tracks)}
    used: set[int] = set()
    assignment: dict[int, DiscogsTrack] = {}
    for fi, f in enumerate(files):
        if f.track is None:
            continue
        ti = by_number.get(((f.disc or 1) if multi else 1, f.track))
        if ti is not None and ti not in used:
            used.add(ti)
            assignment[fi] = release.tracks[ti]
    for fi, f in enumerate(files):
        wanted = normalize_words(f.title)
        if fi in assignment or not wanted:
            continue
        for ti, t in enumerate(release.tracks):
            if ti not in used and normalize_words(t.title) == wanted:
                used.add(ti)
                assignment[fi] = t
                break
    return assignment


@dataclass
class DiscogsMatch:
    release: DiscogsRelease
    score: float  # 0..1, see score_candidate
    assignment: dict[int, DiscogsTrack]  # file index -> its track
    file_count: int
    exact: bool  # artist and album equal the query's, normalised

    @property
    def matched(self) -> int:
        return len(self.assignment)

    @property
    def count_equal(self) -> bool:
        return self.release.track_count == self.file_count

    def summary(self) -> str:
        return f"{self.matched} of {self.file_count} files matched, {round(self.score * 100)}%"


def _is_exact(query: DiscogsQuery, release: DiscogsRelease) -> bool:
    artist, album = normalize_words(query.artist), normalize_words(query.album)
    return bool(artist and album) and artist == normalize_words(release.artist) and album == normalize_words(release.title)


def build_match(query: DiscogsQuery, files: list[FileFacts], release: DiscogsRelease) -> DiscogsMatch:
    return DiscogsMatch(
        release=release,
        score=score_candidate(query, release.artist, release.title, release.year, release.track_count, len(files)),
        assignment=assign_tracks(files, release),
        file_count=len(files),
        exact=_is_exact(query, release),
    )


def rank_matches(matches: list[DiscogsMatch]) -> list[DiscogsMatch]:
    """Best first: score, then a track count equal to the folder's, then
    the most files placed, then the oldest release (the original pressing)."""
    return sorted(matches, key=lambda m: (-round(m.score, 3), not m.count_equal, -m.matched, m.release.year or "9999"))


def find_release(
    client: DiscogsClient, files: list[FileFacts], query: DiscogsQuery,
    candidates: int = 4, cache: Optional[dict] = None,
) -> list[DiscogsMatch]:
    """Searches, fetches the full release of the top `candidates` hits (one
    request each) and returns them as ranked matches. Stops with
    DiscogsRateLimited if Discogs says so."""
    hits = search_releases(client, query)
    return rank_matches([build_match(query, files, fetch_release(client, hit.id, cache)) for hit in hits[:candidates]])


# ---------------------------------------------------------------------------
# Confidence (used by the Redact step) and field mapping
# ---------------------------------------------------------------------------

CONFIDENT = 0.93  # exact artist+album, equal track count, one clear best release
EXACT_ONLY = 0.7  # exact artist+album, but the track count differs or several releases tie
FUZZY_CAP = 0.6  # anything looser is only ever a suggestion
CLEAR_MARGIN = 0.05  # how far the best release's score must lead the runner-up


def confidence(matches: list[DiscogsMatch]) -> float:
    """How far to trust the best match: 0.93 when artist and album are
    exactly the query's, the track count equals the folder's and no other
    release scores within CLEAR_MARGIN of it; 0.7 when artist and album are
    exact but the track count differs or the best release is not clearly
    ahead (several pressings of one album); at most 0.6 otherwise."""
    if not matches:
        return 0.0
    top = matches[0]
    if not top.exact:
        return min(FUZZY_CAP, top.score)
    clear = len(matches) == 1 or top.score - matches[1].score >= CLEAR_MARGIN
    return CONFIDENT if top.count_equal and clear else EXACT_ONLY


def genre_text(release: DiscogsRelease, include_styles: bool = True) -> str:
    names = release.genres + (release.styles if include_styles else [])
    return GENRE_SEPARATOR.join(dict.fromkeys(names))


def fields_for(release: DiscogsRelease, track: DiscogsTrack, include_styles: bool = True) -> dict[str, str]:
    """Tag field values (mp3redactor's core/fields.py keys) for one matched file.
    The track number is the plain number (as the MusicBrainz lookup
    writes it); the disc number only on a multi-disc release."""
    fields = {
        "title": track.title,
        "artist": track.artist,
        "albumartist": release.artist,
        "album": release.title,
        "track": str(track.number),
        "year": release.year,
        "genre": genre_text(release, include_styles),
        "publisher": release.label,
        "catalognumber": release.catno,
        "releasecountry": release.country,
    }
    if release.disc_count > 1:
        fields["discnumber"] = str(track.disc)
    return {k: v for k, v in fields.items() if v}

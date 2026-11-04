"""
redactor_common/core/lookups/tmdb.py (from videoredactor's core/tmdb_client.py, 2026-11-04)

TMDB (The Movie Database) API client.

The API key comes from the app's key source (configure(); videoredactor's
core/api_keys.py reads the TMDB_API_KEY env var first, then the secret
store). Without a configured source only TMDB_API_KEY is read. No key
baked in, none requested from the user in chat.

NOTE: not yet runnable/testable in this sandbox -- no network access.
Written against TMDB's documented v3 API
(https://developer.themoviedb.org/reference); needs a real functional
pass once a key + network access are available. Every function returns
a clear TMDBError (never a silent empty result) when the key is missing,
so the GUI layer can prompt the user rather than fail mysteriously.
"""

from __future__ import annotations
import os
from dataclasses import dataclass
from typing import Callable, Optional

from redactor_common.core.lookup_client import (
    as_dict, as_int, as_text, build_request, dict_items, fetch_bytes, fetch_json, json_object, make_default_fetch,
)

BASE_URL = "https://api.themoviedb.org/3"
USER_AGENT = "redactor_common"  # replaced by the app's configure()
KEY_ENV_VAR = "TMDB_API_KEY"
# The wording TMDB prescribes, with "application" as the product word. Kept identical to the sentence in CREDITS.md and
# ABOUT.md (a test checks that); the folder notes made from TMDB carry it too.
TMDB_NOTICE = (
    "This application uses TMDB and the TMDB APIs but is not endorsed, "
    "certified, or otherwise approved by TMDB."
)
TMDB_URL = "https://www.themoviedb.org/"
_STATUS_MESSAGES = {
    401: "TMDB rejected the API key (401 Unauthorized).",
    429: "TMDB rate limit hit -- try again shortly.",
}
# Injectable for tests; see redactor_common.core.lookup_client.
_fetch = make_default_fetch(USER_AGENT, timeout=10)
_image_fetch = make_default_fetch(USER_AGENT, timeout=15)
IMAGE_BASE_URL = "https://image.tmdb.org/t/p/"
POSTER_SIZE = "w500"  # reasonable balance of quality vs download size


class TMDBError(Exception):
    """Raised for missing API key, network failure, or a non-2xx response.
    Always carries a human-readable message meant to be shown directly to
    the user (e.g. in a QMessageBox), not just logged."""


@dataclass
class MovieCandidate:
    tmdb_id: int
    title: str
    release_date: str   # 'YYYY-MM-DD' or '' if unreleased/unknown
    overview: str
    poster_path: Optional[str]  # relative path; combine with IMAGE_BASE_URL

    @property
    def year(self) -> str:
        return self.release_date[:4] if self.release_date else ""


@dataclass
class TVCandidate:
    tmdb_id: int
    name: str
    first_air_date: str
    overview: str
    poster_path: Optional[str]

    @property
    def year(self) -> str:
        return self.first_air_date[:4] if self.first_air_date else ""


@dataclass
class SeasonInfo:
    season_number: int
    name: str
    episode_count: int

    @property
    def display_name(self) -> str:
        # TMDB uses season_number 0 for specials -- label it clearly
        # rather than showing a confusing "Season 0".
        if self.season_number == 0:
            return f"Specials ({self.episode_count} episodes)"
        return f"{self.name} ({self.episode_count} episodes)"


@dataclass
class EpisodeInfo:
    episode_number: int
    name: str
    overview: str
    air_date: str


def _path_or_none(value: object) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _env_key() -> Optional[str]:
    return os.environ.get(KEY_ENV_VAR, "").strip() or None


_key_source: Callable[[], Optional[str]] = _env_key


def configure(user_agent: str, key_source: Optional[Callable[[], Optional[str]]] = None) -> None:
    """The app's User-Agent and where its key is read (a callable returning the key or None), set once by the app."""
    global USER_AGENT, _fetch, _image_fetch, _key_source
    USER_AGENT = user_agent
    _fetch = make_default_fetch(user_agent, timeout=10)
    _image_fetch = make_default_fetch(user_agent, timeout=15)
    _key_source = key_source or _env_key


def get_api_key() -> Optional[str]:
    return _key_source()


def _require_api_key() -> str:
    key = get_api_key()
    if not key:
        raise TMDBError(
            "No TMDB API key configured. Add one under Tools > "
            "API Keys... (or set the TMDB_API_KEY environment variable)."
        )
    return key


def _get_json(path: str, params: dict) -> dict:
    """GET + JSON via redactor_common's lookup_client, which also turns a
    read timeout or a malformed response into a TMDBError -- both used
    to escape as an uncaught exception, since only HTTPError/URLError
    were handled here."""
    key = _require_api_key()
    request = build_request(f"{BASE_URL}{path}", {**params, "api_key": key})
    return json_object(fetch_json(
        request, _fetch, TMDBError, "TMDB", status_messages=_STATUS_MESSAGES,
    ), TMDBError, "TMDB")


def search_movies(query: str, year: Optional[str] = None) -> list[MovieCandidate]:
    """Search TMDB for movies matching `query`, optionally narrowed by
    release year. Always returns the full candidate list -- the picker
    dialog is responsible for letting the user choose, never this
    function auto-selecting a 'best' match (per explicit instruction:
    always show the picker, even for one strong match).
    """
    # A short ladder, stopping at the first step with results: the year as the FIRST release year (strict), then as
    # any release year (TMDB's looser `year`, which also counts regional releases), then no year at all.
    steps = [{"primary_release_year": year}, {"year": year}, {}] if year else [{}]
    for extra in steps:
        data = _get_json("/search/movie", {"query": query, "include_adult": "false", **extra})
        found = [
            MovieCandidate(
                tmdb_id=as_int(r.get("id")),
                title=as_text(r.get("title")),
                release_date=as_text(r.get("release_date")),
                overview=as_text(r.get("overview")),
                poster_path=_path_or_none(r.get("poster_path")),
            )
            for r in dict_items(data.get("results")) if as_int(r.get("id"))
        ]
        if found:
            return found
    return []


def search_tv(query: str, year: Optional[str] = None) -> list[TVCandidate]:
    """Search TMDB for TV shows matching `query` (show-level search --
    season/episode lookup is a separate step once a show is picked).
    With a `year` the first-air year is tried first, then no year."""
    for extra in ([{"first_air_date_year": year}, {}] if year else [{}]):
        data = _get_json("/search/tv", {"query": query, **extra})
        found = [
            TVCandidate(
                tmdb_id=as_int(r.get("id")),
                name=as_text(r.get("name")),
                first_air_date=as_text(r.get("first_air_date")),
                overview=as_text(r.get("overview")),
                poster_path=_path_or_none(r.get("poster_path")),
            )
            for r in dict_items(data.get("results")) if as_int(r.get("id"))
        ]
        if found:
            return found
    return []


def get_movie_details(tmdb_id: int) -> dict:
    """Full movie detail fetch, returned as a dict of VideoMetadata-shaped
    field names -- caller applies these onto a VideoMetadata instance
    (only overwriting fields the user confirms, same "only touched fields"
    principle as TagPanel's bulk-edit apply).
    """
    data = _get_json(f"/movie/{tmdb_id}", {"append_to_response": "credits"})

    credits = as_dict(data.get("credits"))
    director = ""
    for crew in dict_items(credits.get("crew")):
        if crew.get("job") == "Director":
            director = as_text(crew.get("name"))
            break

    cast_names = [as_text(c.get("name")) for c in dict_items(credits.get("cast"))[:10]]

    return {
        "title": as_text(data.get("title")),
        "description": as_text(data.get("overview")),
        "genre_tags": ", ".join(as_text(g.get("name")) for g in dict_items(data.get("genres"))),
        "release_date": as_text(data.get("release_date")),
        "language": as_text(data.get("original_language")),
        "director": director,
        "cast": ", ".join(cast_names),
        "studio": ", ".join(as_text(c.get("name")) for c in dict_items(data.get("production_companies"))),
        "_poster_path": _path_or_none(data.get("poster_path")),  # underscore: not a VideoMetadata field, handled specially by caller
        "_imdb_id": as_text(data.get("imdb_id")),   # TMDB's own record of the IMDb id (a source id when applied)
    }


def get_tv_show_details(tmdb_id: int) -> dict:
    """Show-level details (Network, general info) -- season/episode
    numbering is filled in separately since it's per-file, not per-show.
    """
    data = _get_json(f"/tv/{tmdb_id}", {"append_to_response": "external_ids"})
    external = as_dict(data.get("external_ids"))
    return {
        "show_title": as_text(data.get("name")),
        "description": as_text(data.get("overview")),
        "genre_tags": ", ".join(as_text(g.get("name")) for g in dict_items(data.get("genres"))),
        "release_date": as_text(data.get("first_air_date")),
        "network": ", ".join(as_text(n.get("name")) for n in dict_items(data.get("networks"))),
        "_poster_path": _path_or_none(data.get("poster_path")),
        "_imdb_id": as_text(external.get("imdb_id")),
        "_tvdb_id": as_text(external.get("tvdb_id")),
    }


def get_tv_seasons(tmdb_id: int) -> list[SeasonInfo]:
    """List a show's seasons, for the season/episode picker. Reuses the
    same /tv/{id} endpoint as get_tv_show_details (TMDB includes a
    'seasons' array in that response) rather than a second round-trip --
    called separately from get_tv_show_details anyway since the picker
    needs this before the user has necessarily committed to importing
    show-level fields.
    """
    data = _get_json(f"/tv/{tmdb_id}", {})
    return [
        SeasonInfo(
            season_number=as_int(s.get("season_number")),
            name=as_text(s.get("name")) or f"Season {as_text(s.get('season_number'), '?')}",
            episode_count=as_int(s.get("episode_count")),
        )
        for s in dict_items(data.get("seasons"))
    ]


def get_season_episodes(tmdb_id: int, season_number: int) -> list[EpisodeInfo]:
    """List episodes within a specific season, for the episode picker."""
    data = _get_json(f"/tv/{tmdb_id}/season/{season_number}", {})
    return [
        EpisodeInfo(
            episode_number=as_int(e.get("episode_number")),
            name=as_text(e.get("name")),
            overview=as_text(e.get("overview")),
            air_date=as_text(e.get("air_date")),
        )
        for e in dict_items(data.get("episodes"))
    ]


def get_tv_episode_details(tmdb_id: int, season: int, episode: int) -> dict:
    """Episode-level details for a specific season/episode of a show
    already identified via get_tv_show_details."""
    data = _get_json(f"/tv/{tmdb_id}/season/{season}/episode/{episode}", {})
    return {
        "title": as_text(data.get("name")),           # episode title
        "description": as_text(data.get("overview")),
        "release_date": as_text(data.get("air_date")),
        "season_number": season,
        "episode_number": episode,
    }


def find_poster_by_imdb(imdb_id: str) -> str:
    """The poster path of the film or show TMDB lists for an IMDb title id ("" when TMDB has none): the way from an
    IMDb id to a poster. Movies first, then shows."""
    data = _get_json(f"/find/{imdb_id}", {"external_source": "imdb_id"})
    for key in ("movie_results", "tv_results"):
        for result in dict_items(data.get(key)):
            path = _path_or_none(result.get("poster_path"))
            if path:
                return path
    return ""


def download_poster(poster_path: str) -> bytes:
    """Download poster image bytes at POSTER_SIZE resolution."""
    url = f"{IMAGE_BASE_URL}{POSTER_SIZE}{poster_path}"
    return fetch_bytes(url, _image_fetch, TMDBError, what="the poster")

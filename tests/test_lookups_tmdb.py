"""core/lookups/tmdb.py with the HTTP fetch replaced by a fake: nothing touches the network. Moved with the module from
videoredactor (2026-11-04), which keeps its own tests of how its Look Up and Redact use it."""

import io
import json
import socket
import urllib.error

import pytest

from redactor_common.core.lookups import tmdb


def _http_error(code):
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(b""))


def _record(monkeypatch, answers):
    """The next canned answer per request; records each request's URL."""
    seen = []

    def fetch(request):
        seen.append(request.full_url)
        return json.dumps(answers[min(len(seen), len(answers)) - 1]).encode()

    monkeypatch.setattr(tmdb, "_fetch", fetch)
    monkeypatch.setattr(tmdb, "get_api_key", lambda: "k")
    return seen


def test_friendly_401_and_query_string(monkeypatch):
    seen = []

    def fake(request):
        seen.append(request.full_url)
        raise _http_error(401)

    monkeypatch.setattr(tmdb, "_require_api_key", lambda: "k")
    monkeypatch.setattr(tmdb, "_fetch", fake)
    with pytest.raises(tmdb.TMDBError, match="rejected the API key"):
        tmdb._get_json("/search/movie", {"query": "Amélie"})
    assert "api_key=k" in seen[0] and "query=Am%C3%A9lie" in seen[0]


def test_timeout_and_bad_json_become_tmdb_errors(monkeypatch):
    monkeypatch.setattr(tmdb, "_require_api_key", lambda: "k")
    monkeypatch.setattr(tmdb, "_fetch", lambda r: (_ for _ in ()).throw(socket.timeout("timed out")))
    with pytest.raises(tmdb.TMDBError, match="Could not reach TMDB"):
        tmdb._get_json("/x", {})
    monkeypatch.setattr(tmdb, "_fetch", lambda r: b"<html>not json")
    with pytest.raises(tmdb.TMDBError, match="unreadable"):
        tmdb._get_json("/x", {})


def test_movie_year_ladder_goes_strict_then_loose_then_none(monkeypatch):
    seen = _record(monkeypatch, [{"results": []}, {"results": []}, {"results": [{"id": 5, "title": "T"}]}])
    found = tmdb.search_movies("T", "1999")
    assert [c.tmdb_id for c in found] == [5]
    assert "primary_release_year=1999" in seen[0] and "year=1999" in seen[1] and "primary_release_year" not in seen[1]
    assert "year" not in seen[2]


def test_tv_uses_the_first_air_year_then_no_year(monkeypatch):
    seen = _record(monkeypatch, [{"results": []}, {"results": [{"id": 2, "name": "S"}]}])
    assert [c.tmdb_id for c in tmdb.search_tv("S", "2008")] == [2]
    assert "first_air_date_year=2008" in seen[0] and "year" not in seen[1]


def test_movie_details_are_mapped_to_field_names(monkeypatch):
    _record(monkeypatch, [{
        "title": "Inception", "overview": "Dreams.", "genres": [{"name": "Action"}, {"name": "Sci-Fi"}],
        "release_date": "2010-07-16", "original_language": "en", "imdb_id": "tt1375666", "poster_path": "/p.jpg",
        "credits": {"crew": [{"job": "Director", "name": "Christopher Nolan"}], "cast": [{"name": "Leonardo DiCaprio"}]},
        "production_companies": [{"name": "Legendary"}],
    }])
    details = tmdb.get_movie_details(27205)
    assert details["director"] == "Christopher Nolan" and details["genre_tags"] == "Action, Sci-Fi"
    assert details["_imdb_id"] == "tt1375666" and details["_poster_path"] == "/p.jpg"


def test_without_a_key_every_call_says_how_to_add_one(monkeypatch):
    monkeypatch.setattr(tmdb, "get_api_key", lambda: None)
    with pytest.raises(tmdb.TMDBError, match="No TMDB API key"):
        tmdb.search_movies("T")


def test_unconfigured_the_key_comes_from_the_environment(monkeypatch):
    monkeypatch.setattr(tmdb, "_key_source", tmdb._env_key)
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    assert tmdb.get_api_key() is None
    monkeypatch.setenv("TMDB_API_KEY", " env-key ")
    assert tmdb.get_api_key() == "env-key"


def test_configure_sets_the_user_agent_and_the_key_source(monkeypatch):
    for name in ("USER_AGENT", "_fetch", "_image_fetch", "_key_source"):
        monkeypatch.setattr(tmdb, name, getattr(tmdb, name))
    tmdb.configure("TestRedactor", key_source=lambda: "from-the-app")
    assert tmdb.USER_AGENT == "TestRedactor" and tmdb.get_api_key() == "from-the-app"


def test_the_notice_is_tmdbs_prescribed_wording():
    assert tmdb.TMDB_NOTICE == ("This application uses TMDB and the TMDB APIs but is not endorsed, "
                                "certified, or otherwise approved by TMDB.")

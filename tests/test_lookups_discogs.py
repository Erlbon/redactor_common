"""core/lookups/discogs.py against MOCKED Discogs replies only -- these tests
never reach the real API and never use a real token (TOKEN below is a
made-up string whose only job is to be found, or not found, in texts).
Moved with the module from mp3redactor (2026-11-04); the app name here is a
test app's, set through configure() as each app does."""

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from redactor_common.core.lookups import discogs as dl
from redactor_common.core.lookups.musicbrainz import FileFacts
from redactor_common.core import secret_store

TOKEN = "tok_SECRET_0123456789_abcdef"
APP = "testredactor"


class FakeKeyring:
    """In-memory stand-in for keyring's get/set/delete_password API."""

    def __init__(self):
        self.items = {}

    def get_password(self, service, name):
        return self.items.get((service, name))

    def set_password(self, service, name, value):
        self.items[(service, name)] = value

    def delete_password(self, service, name):
        self.items.pop((service, name), None)


@pytest.fixture(autouse=True)
def fake_keyring(tmp_path, monkeypatch):
    """The module configured as an app configures it, an in-memory secret store, and no DISCOGS_TOKEN from the
    developer's own environment; all put back afterwards."""
    monkeypatch.delenv("DISCOGS_TOKEN", raising=False)
    monkeypatch.setattr(dl, "USER_AGENT", dl.USER_AGENT)
    monkeypatch.setattr(dl, "SECRET_APP", dl.SECRET_APP)
    dl.configure(f"{APP}/1.0 +https://example.invalid/{APP}", APP)
    backend = FakeKeyring()
    secret_store.set_backend(backend)
    secret_store.set_fallback_dir(tmp_path / "secret_fallback")
    secret_store.set_allow_unencrypted_fallback(False)
    yield backend
    secret_store.set_backend(None)
    secret_store.set_fallback_dir(None)
    secret_store.set_allow_unencrypted_fallback(False)


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def _fast_throttle() -> dl.Throttle:
    return dl.Throttle(interval=0.0)


class FakeDiscogs:
    """Stands in for the fetch callable: routes by URL path, records requests."""

    def __init__(self, search_results=None, releases=None, raises=None, images=None):
        self.search_results = search_results if search_results is not None else []
        self.releases = releases or {}
        self.raises = raises  # an exception to raise on every call
        self.images = images or {}
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        url = request.full_url
        path = urllib.parse.urlsplit(url).path
        if path.endswith("/database/search"):
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            results = self.search_results
            if callable(results):
                results = results(params)
            return json.dumps({"results": results}).encode()
        if "/releases/" in path:
            rid = int(path.rsplit("/", 1)[1])
            if rid not in self.releases:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            return json.dumps(self.releases[rid]).encode()
        if url in self.images:
            return self.images[url]
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    def searches(self):
        return [urllib.parse.parse_qs(urllib.parse.urlsplit(r.full_url).query)
                for r in self.requests if r.full_url.startswith(dl.API_BASE + "/database/search")]


def _client(fake, throttle=None) -> dl.DiscogsClient:
    return dl.DiscogsClient(TOKEN, fetch=fake, throttle=throttle or _fast_throttle())


def _hit(rid, title, year="1959", country="US", label=("Columbia",), catno="CL 1355", formats=("Vinyl", "LP")):
    return {"id": rid, "title": title, "year": year, "country": country, "label": list(label),
            "catno": catno, "format": list(formats), "type": "release"}


KIND_OF_BLUE = [
    {"position": "A1", "title": "So What", "type_": "track", "duration": "9:22"},
    {"position": "A2", "title": "Freddie Freeloader", "type_": "track"},
    {"position": "B1", "title": "Blue In Green", "type_": "track"},
    {"position": "B2", "title": "All Blues", "type_": "track"},
    {"position": "B3", "title": "Flamenco Sketches", "type_": "track"},
]


def _release(rid=1, title="Kind Of Blue", artists=(("Miles Davis", ""),), year=1959, country="US",
             label="Columbia", catno="CL 1355", tracklist=None, genres=("Jazz",), styles=("Modal",), images=None):
    return {
        "id": rid, "title": title,
        "artists": [{"name": n, "join": j} for n, j in artists],
        "year": year, "country": country,
        "labels": [{"name": label, "catno": catno}] if label else [],
        "genres": list(genres), "styles": list(styles),
        "tracklist": KIND_OF_BLUE if tracklist is None else tracklist,
        "images": images if images is not None else [
            {"type": "secondary", "uri": "https://i.discogs.com/second.jpg"},
            {"type": "primary", "uri": "https://i.discogs.com/primary.jpg"},
        ],
        "uri": f"https://www.discogs.com/release/{rid}",
    }


def _facts(n=5, titles=None):
    return [FileFacts(title=(titles[i] if titles else ""), track=i + 1) for i in range(n)]


# ---------------------------------------------------------------------------
# Query building
# ---------------------------------------------------------------------------

def test_query_from_tags_uses_the_most_common_values():
    q = dl.discogs_query(["Kind of Blue"] * 3 + [""], ["Miles Davis"] * 2 + [""], ["1959-08-17", "1959", ""], Path("x"))
    assert (q.artist, q.album, q.year) == ("Miles Davis", "Kind of Blue", "1959")


def test_query_falls_back_to_the_folder_name_with_its_year():
    q = dl.discogs_query(["", ""], ["", ""], ["", ""], Path("Miles Davis - Kind of Blue (1959)"))
    assert (q.artist, q.album, q.year) == ("Miles Davis", "Kind of Blue", "1959")


def test_query_keeps_tag_values_and_fills_only_the_gaps_from_the_folder():
    q = dl.discogs_query(["Kind of Blue"], [""], [""], Path("Miles Davis - Something Else (1958)"))
    assert q.album == "Kind of Blue" and q.artist == "Miles Davis" and q.year == "1958"


def test_query_remembers_a_track_title_for_album_less_searches():
    q = dl.discogs_query([""], ["Miles Davis"], [""], Path("Downloads"), ["", "So What"])
    assert q.track == "So What"


def test_search_asks_discogs_for_artist_title_and_year_first():
    fake = FakeDiscogs(search_results=[_hit(1, "Miles Davis - Kind Of Blue")])
    dl.search_releases(_client(fake), dl.DiscogsQuery("Miles Davis", "Kind of Blue", "1959"))
    params = fake.searches()[0]
    assert params["type"] == ["release"]
    assert params["artist"] == ["Miles Davis"] and params["release_title"] == ["Kind of Blue"]
    assert params["year"] == ["1959"] and params["per_page"] == ["10"]


def test_search_loosens_step_by_step_until_something_is_found():
    def results(params):
        return [_hit(1, "Miles Davis - Kind Of Blue")] if "q" in params else []

    fake = FakeDiscogs(search_results=results)
    hits = dl.search_releases(_client(fake), dl.DiscogsQuery("Miles Davis", "Kind of Blue", "1959"))
    assert [h.id for h in hits] == [1]
    attempts = fake.searches()
    assert "year" in attempts[0] and "year" not in attempts[1]
    assert attempts[2]["q"] == ["Miles Davis Kind of Blue"]


def test_search_ends_with_the_title_alone_and_never_repeats_an_attempt():
    fake = FakeDiscogs(search_results=[])
    assert dl.search_releases(_client(fake), dl.DiscogsQuery("Various Artists", "Bloodstains Across Norway")) == []
    attempts = fake.searches()
    # "Various Artists" is searched as Discogs' "Various", so the Various attempt is not made twice
    assert attempts == [
        {"type": ["release"], "per_page": ["10"], "release_title": ["Bloodstains Across Norway"], "artist": ["Various"]},
        {"type": ["release"], "per_page": ["10"], "q": ["Various Artists Bloodstains Across Norway"]},
        {"type": ["release"], "per_page": ["10"], "release_title": ["Bloodstains Across Norway"]},
    ]


@pytest.mark.parametrize("name", ["Various", "Various Artists", "VA", "V/A", "V.A.", "various-artists"])
def test_various_artist_names(name):
    assert dl.is_various(name)


@pytest.mark.parametrize("name", ["", "Vanilla Fudge", "Bloodstains Across", "Various Positions"])
def test_real_artist_names_are_not_various(name):
    assert not dl.is_various(name)


def test_search_without_an_album_searches_artist_and_track_or_refuses():
    fake = FakeDiscogs(search_results=[_hit(1, "Miles Davis - Kind Of Blue")])
    dl.search_releases(_client(fake), dl.DiscogsQuery(artist="Miles Davis", track="So What"))
    assert fake.searches()[0]["track"] == ["So What"]
    with pytest.raises(dl.DiscogsError):
        dl.search_releases(_client(fake), dl.DiscogsQuery(artist="Miles Davis"))


def test_request_carries_the_token_only_in_the_header():
    fake = FakeDiscogs(search_results=[_hit(1, "Miles Davis - Kind Of Blue")])
    dl.search_releases(_client(fake), dl.DiscogsQuery("Miles Davis", "Kind of Blue"))
    request = fake.requests[0]
    assert request.get_header("Authorization") == f"Discogs token={TOKEN}"
    assert APP in request.get_header("User-agent")
    assert TOKEN not in request.full_url


# ---------------------------------------------------------------------------
# Candidate scoring
# ---------------------------------------------------------------------------

def test_exact_artist_and_album_outscore_a_fuzzy_one():
    q = dl.DiscogsQuery("Miles Davis", "Kind of Blue", "1959")
    exact = dl.score_candidate(q, "Miles Davis", "Kind Of Blue", "1959")
    fuzzy = dl.score_candidate(q, "Miles Davis Quintet", "Kind Of Blue (Legacy Edition)", "2009")
    other = dl.score_candidate(q, "Someone Else", "Another Album", "1959")
    assert exact == pytest.approx(1.0) and other < fuzzy < exact


def test_scoring_normalises_case_punctuation_and_ampersands():
    q = dl.DiscogsQuery("Simon & Garfunkel", "Bridge Over Troubled Water")
    assert dl.score_candidate(q, "Simon And Garfunkel", "bridge over troubled water!") == pytest.approx(1.0)


def test_year_and_track_count_closeness_count_and_unknowns_are_ignored():
    q = dl.DiscogsQuery("A", "B", "2000")
    base = dl.score_candidate(q, "A", "B", "2000")
    assert dl.score_candidate(q, "A", "B", "2001") < base
    assert dl.score_candidate(q, "A", "B", "2000", track_count=10, file_count=10) == pytest.approx(1.0)
    assert dl.score_candidate(q, "A", "B", "2000", track_count=20, file_count=10) < 1.0
    no_year = dl.DiscogsQuery("A", "B")
    assert dl.score_candidate(no_year, "A", "B", "1999") == pytest.approx(1.0)


def test_candidates_are_parsed_defensively_and_sorted_best_first():
    data = {"results": [
        _hit(2, "Miles Davis Quintet - Kind Of Blue (Remastered)", year="2009"),
        _hit(1, "Miles Davis - Kind Of Blue"),
        {"title": "no id"}, "junk", {"id": "x"},
        {"id": 3, "title": "Miles Davis (2) - Kind Of Blue", "label": "not-a-list", "catno": "none", "year": 1959},
    ]}
    hits = dl.parse_candidates(data, dl.DiscogsQuery("Miles Davis", "Kind of Blue", "1959"))
    assert {h.id for h in hits} == {1, 2, 3}
    three = next(h for h in hits if h.id == 3)
    assert three.artist == "Miles Davis" and three.label == "" and three.catno == "" and three.year == "1959"
    assert dl.parse_candidates({}, dl.DiscogsQuery()) == [] and dl.parse_candidates({"results": None}, dl.DiscogsQuery()) == []


# ---------------------------------------------------------------------------
# Names and positions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, clean", [
    ("Nirvana (2)", "Nirvana"), ("Prince", "Prince"), ("Various", "Various Artists"),
    ("Various Artists", "Various Artists"), ("Blink-182 (3) ", "Blink-182"), ("Band (feat. Guest)", "Band (feat. Guest)"),
])
def test_artist_disambiguation_numbers_are_stripped(raw, clean):
    assert dl.clean_artist_name(raw) == clean


def test_artist_credits_join_like_the_release_page():
    artists = [{"name": "Simon (4)", "join": "&"}, {"name": "Garfunkel", "join": ""}]
    assert dl.join_artists(artists) == "Simon & Garfunkel"
    assert dl.join_artists([{"name": "A", "join": ","}, {"name": "B", "join": "feat."}, {"name": "C"}]) == "A, B feat. C"
    assert dl.join_artists(None) == "" and dl.join_artists(["junk"]) == ""


@pytest.mark.parametrize("positions, expected", [
    (["1", "2", "3"], [(None, 1), (None, 2), (None, 3)]),
    (["1-1", "1-2", "2-1", "2-2"], [(1, 1), (1, 2), (2, 1), (2, 2)]),
    (["1.1", "1.2", "2.1"], [(1, 1), (1, 2), (2, 1)]),
    (["CD1-1", "CD1-2", "CD2-1"], [(1, 1), (1, 2), (2, 1)]),
    (["cd2-5"], [(2, 5)]),
    (["A1", "A2", "B1", "B2"], [(None, 1), (None, 2), (None, 3), (None, 4)]),  # vinyl sides: sequential
    (["A", "B", "C", "D"], [(None, 1), (None, 2), (None, 3), (None, 4)]),
    (["A1", "B1", "C1", "D1"], [(None, 1), (None, 2), (None, 3), (None, 4)]),
    (["1", "2", "1", "2"], [(None, 1), (None, 2), (None, 3), (None, 4)]),  # would repeat: sequential
    (["1", "", "3"], [(None, 1), (None, 2), (None, 3)]),  # unparseable: sequential
    (["1-1", "2", "3"], [(None, 1), (None, 2), (None, 3)]),  # mixed shapes: sequential
    (["0", "1"], [(None, 1), (None, 2)]),
    ([], []),
])
def test_positions_become_disc_and_track_numbers(positions, expected):
    assert dl.number_positions(positions) == expected


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------

def test_release_parsing_maps_the_documented_fields():
    release = dl.parse_release(_release())
    assert (release.artist, release.title, release.year, release.country) == ("Miles Davis", "Kind Of Blue", "1959", "US")
    assert (release.label, release.catno) == ("Columbia", "CL 1355")
    assert release.genres == ["Jazz"] and release.styles == ["Modal"]
    assert release.track_count == 5 and release.disc_count == 1
    assert [t.number for t in release.tracks] == [1, 2, 3, 4, 5]  # vinyl sides A1..B3 numbered down the release
    assert release.cover_url == "https://i.discogs.com/primary.jpg"  # the primary image, not the first


def test_release_parsing_drops_headings_and_unfolds_index_tracks():
    tracklist = [
        {"position": "", "title": "Side One", "type_": "heading"},
        {"position": "1", "title": "Intro", "type_": "track"},
        {"position": "2", "title": "Suite", "type_": "index", "sub_tracks": [
            {"position": "2.1", "title": "Part I", "type_": "track"},
            {"position": "2.2", "title": "Part II", "type_": "track"},
        ]},
        {"position": "3", "title": "", "type_": "track"},
    ]
    release = dl.parse_release(_release(tracklist=tracklist))
    assert [t.title for t in release.tracks] == ["Intro", "Part I", "Part II"]
    assert [t.number for t in release.tracks] == [1, 2, 3]


def test_a_two_disc_release_keeps_its_discs():
    tracklist = [{"position": f"{d}-{n}", "title": f"T{d}{n}", "type_": "track"} for d in (1, 2) for n in (1, 2)]
    release = dl.parse_release(_release(tracklist=tracklist))
    assert release.disc_count == 2 and (release.tracks[2].disc, release.tracks[2].number) == (2, 1)


def test_various_artists_release_keeps_per_track_artists():
    tracklist = [{"position": "1", "title": "Song", "type_": "track", "artists": [{"name": "Nirvana (2)"}]},
                 {"position": "2", "title": "Other", "type_": "track"}]
    release = dl.parse_release(_release(artists=(("Various", ""),), tracklist=tracklist))
    assert release.artist == "Various Artists"
    assert [t.artist for t in release.tracks] == ["Nirvana", "Various Artists"]


def test_missing_and_oddly_typed_fields_never_crash():
    for data in ({}, None, [], {"tracklist": "x", "labels": "x", "images": 5, "artists": 3, "genres": "Rock"},
                 {"labels": [None, 3], "images": [None, {"uri": []}], "tracklist": [None, 4, {"title": []}]}):
        release = dl.parse_release(data, 7)
        assert isinstance(release, dl.DiscogsRelease) and release.tracks == []


def test_catalog_number_none_means_no_number():
    assert dl.parse_release(_release(catno="none")).catno == ""


def test_fields_map_to_the_mp3_tags():
    release = dl.parse_release(_release())
    fields = dl.fields_for(release, release.tracks[2])
    assert fields == {
        "title": "Blue In Green", "artist": "Miles Davis", "albumartist": "Miles Davis", "album": "Kind Of Blue",
        "track": "3", "year": "1959", "genre": "Jazz; Modal", "publisher": "Columbia",
        "catalognumber": "CL 1355", "releasecountry": "US",
    }
    assert dl.fields_for(release, release.tracks[2], include_styles=False)["genre"] == "Jazz"


def test_disc_number_only_on_a_multi_disc_release_and_empty_values_are_left_out():
    tracklist = [{"position": f"{d}-1", "title": f"T{d}", "type_": "track"} for d in (1, 2)]
    release = dl.parse_release(_release(tracklist=tracklist, label="", country="", year=0, genres=(), styles=()))
    fields = dl.fields_for(release, release.tracks[1])
    assert fields["discnumber"] == "2" and fields["track"] == "1"
    for empty in ("year", "genre", "publisher", "catalognumber", "releasecountry"):
        assert empty not in fields


def test_genres_and_styles_are_deduplicated():
    release = dl.parse_release(_release(genres=("Rock", "Pop"), styles=("Pop", "Prog Rock")))
    assert dl.genre_text(release) == "Rock; Pop; Prog Rock"


# ---------------------------------------------------------------------------
# Matching files to tracks
# ---------------------------------------------------------------------------

def test_files_are_paired_by_track_number_first():
    release = dl.parse_release(_release())
    files = [FileFacts(title="junk", track=2), FileFacts(title="", track=5)]
    assignment = dl.assign_tracks(files, release)
    assert assignment[0].title == "Freddie Freeloader" and assignment[1].title == "Flamenco Sketches"


def test_files_without_a_usable_number_are_paired_by_normalised_title():
    release = dl.parse_release(_release())
    files = [FileFacts(title="so what"), FileFacts(title="ALL BLUES!"), FileFacts(title="Unknown Song")]
    assignment = dl.assign_tracks(files, release)
    assert assignment[0].title == "So What" and assignment[1].title == "All Blues" and 2 not in assignment


def test_a_track_is_never_used_twice():
    release = dl.parse_release(_release())
    files = [FileFacts(track=1), FileFacts(track=1, title="So What")]
    assignment = dl.assign_tracks(files, release)
    assert len(assignment) == 1 and 0 in assignment


def test_multi_disc_pairing_uses_the_disc():
    tracklist = [{"position": f"{d}-{n}", "title": f"T{d}{n}", "type_": "track"} for d in (1, 2) for n in (1, 2)]
    release = dl.parse_release(_release(tracklist=tracklist))
    assignment = dl.assign_tracks([FileFacts(track=1, disc=2), FileFacts(track=1)], release)
    assert assignment[0].title == "T21" and assignment[1].title == "T11"


# ---------------------------------------------------------------------------
# Finding the release, confidence
# ---------------------------------------------------------------------------

def _two_pressings_fake(second_tracks=None):
    return FakeDiscogs(
        search_results=[_hit(1, "Miles Davis - Kind Of Blue"), _hit(2, "Miles Davis - Kind Of Blue", year="1997", label=("Legacy",))],
        releases={1: _release(1), 2: _release(2, year=1997, label="Legacy", tracklist=second_tracks)},
    )


def test_find_release_fetches_details_once_per_release_with_a_cache():
    fake = _two_pressings_fake()
    cache: dict = {}
    query = dl.DiscogsQuery("Miles Davis", "Kind of Blue", "1959")
    matches = dl.find_release(_client(fake), _facts(), query, cache=cache)
    assert matches[0].release.id == 1  # the 1959 pressing wins on year
    detail_requests = len([r for r in fake.requests if "/releases/" in r.full_url])
    dl.find_release(_client(fake), _facts(), query, cache=cache)
    assert len([r for r in fake.requests if "/releases/" in r.full_url]) == detail_requests == 2


def test_one_clear_exact_release_with_the_same_track_count_is_confident():
    fake = FakeDiscogs(search_results=[_hit(1, "Miles Davis - Kind Of Blue")], releases={1: _release(1)})
    matches = dl.find_release(_client(fake), _facts(5), dl.DiscogsQuery("Miles Davis", "Kind of Blue", "1959"))
    assert matches[0].exact and matches[0].count_equal
    assert dl.confidence(matches) == dl.CONFIDENT == 0.93


def test_a_clear_winner_among_several_pressings_is_still_confident():
    matches = dl.find_release(_client(_two_pressings_fake()), _facts(5), dl.DiscogsQuery("Miles Davis", "Kind of Blue", "1959"))
    assert dl.confidence(matches) == 0.93


def test_tied_pressings_are_not_confident():
    matches = dl.find_release(_client(_two_pressings_fake()), _facts(5), dl.DiscogsQuery("Miles Davis", "Kind of Blue"))
    assert matches[0].score == matches[1].score
    assert dl.confidence(matches) == dl.EXACT_ONLY == 0.7


def test_exact_artist_and_album_with_a_different_track_count_is_seventy_percent():
    fake = FakeDiscogs(search_results=[_hit(1, "Miles Davis - Kind Of Blue")], releases={1: _release(1)})
    matches = dl.find_release(_client(fake), _facts(3), dl.DiscogsQuery("Miles Davis", "Kind of Blue", "1959"))
    assert matches[0].exact and not matches[0].count_equal
    assert dl.confidence(matches) == 0.7


# "Bloodstains Across Norway" (2026-11-25): a punk compilation whose files carry the
# series name "Bloodstains Across" as album artist, the bands as track artists and
# the songs' own years. Discogs lists it under "Various", without a year.
NORWAY_TRACKS = [
    {"position": "1", "title": "C.I.A. - K.G.B.", "artists": [{"name": "Akutt Innleggelse"}]},
    {"position": "2", "title": "Superstjerne", "artists": [{"name": "Akutt Innleggelse"}]},
    {"position": "3", "title": "Pop Star", "artists": [{"name": "Børres Kork"}]},
    {"position": "4", "title": "Uknown", "artists": [{"name": "De Press"}]},
    {"position": "5", "title": "Diggin' T.V.", "artists": [{"name": "Rough Trade"}]},
]
NORWAY_FILE_TITLES = ["CIA-KGB", "Superstjerne", "Pop Star", "Unknown", "Diggin' Tv"]


def _norway_fake(year_found=False):
    """Discogs as it answered on 2026-11-25: nothing under the artist
    "Bloodstains Across", the release under "Various"."""
    def results(params):
        if params.get("artist") == ["Bloodstains Across"] or ("year" in params and not year_found):
            return []
        return [_hit(1815416, "Various - Bloodstains Across Norway", year="", country="",
                     label=("Bloodstains",), catno="BSN1", formats=("CD", "Compilation"))]

    release = _release(1815416, title="Bloodstains Across Norway", artists=(("Various", ""),), year=0, country="",
                       label="Bloodstains", catno="BSN1", tracklist=NORWAY_TRACKS, genres=("Rock",), styles=("Punk",))
    return FakeDiscogs(search_results=results, releases={1815416: release})


def test_a_compilation_tagged_with_its_series_name_is_found_under_various():
    fake = _norway_fake()
    query = dl.discogs_query(["Bloodstains Across Norway"] * 5, ["Bloodstains Across"] * 5,
                             ["1983", "1983", "1979", "1980", "1980"], Path("[1993] - Bloodstains Across Norway"))
    matches = dl.find_release(_client(fake), _facts(5, NORWAY_FILE_TITLES), query)
    assert fake.searches()[-1]["q"] == ["Bloodstains Across Bloodstains Across Norway"]
    top = matches[0]
    assert top.release.id == 1815416 and top.matched == 5 and top.exact
    assert dl.confidence(matches) == dl.CONFIDENT
    assert dl.fields_for(top.release, top.assignment[0])["albumartist"] == "Various Artists"
    assert dl.fields_for(top.release, top.assignment[0])["artist"] == "Akutt Innleggelse"


def test_the_search_order_after_artist_and_title_fail():
    fake = FakeDiscogs(search_results=[])
    dl.search_releases(_client(fake), dl.DiscogsQuery("Bloodstains Across", "Bloodstains Across Norway"))
    assert fake.searches()[1:] == [
        {"type": ["release"], "per_page": ["10"], "q": ["Bloodstains Across Bloodstains Across Norway"]},
        {"type": ["release"], "per_page": ["10"], "release_title": ["Bloodstains Across Norway"], "artist": ["Various"]},
        {"type": ["release"], "per_page": ["10"], "release_title": ["Bloodstains Across Norway"]},
    ]


def test_a_series_name_split_over_album_artist_and_album_is_found_and_exact():
    """"Bloodstains Across Switzerland" (2026-11-25) is tagged album artist
    "Bloodstains Across", album "  Switzerland". A title + "Various" search
    finds Swiss folk LPs called "Switzerland"; the free text finds it."""
    tracks = [{"position": str(i + 1), "title": t} for i, t in enumerate(["Mayday", "Mental Disaster", "Cry For Fame"])]

    def results(params):
        if params.get("q") == ["Bloodstains Across Switzerland"]:
            return [_hit(1815291, "Various - Bloodstains Across Switzerland", year="", label=("Bloodstains",)),
                    _hit(13488008, "Various - Bloodstains Across The World #3", year="2019", label=("Bloodstains",))]
        if params.get("artist") == ["Various"]:
            return [_hit(8554332, "Various - Switzerland", year="", label=("Philips",))]
        return []

    fake = FakeDiscogs(search_results=results, releases={
        1815291: _release(1815291, title="Bloodstains Across Switzerland", artists=(("Various", ""),), year=0,
                          tracklist=tracks),
        13488008: _release(13488008, title="Bloodstains Across The World #3", artists=(("Various", ""),), year=2019,
                           tracklist=[{"position": str(i + 1), "title": f"Other {i}"} for i in range(3)]),
    })
    query = dl.discogs_query(["  Switzerland"] * 3, ["Bloodstains Across"] * 3, ["2005"] * 3,
                             Path("[2005] - Bloodstains Across Switzerland"))
    assert (query.artist, query.album) == ("Bloodstains Across", "Switzerland")
    matches = dl.find_release(_client(fake), _facts(3, ["Mayday", "Mental Disaster", "Cry For Fame"]), query)
    assert [s.get("artist") for s in fake.searches()][-1] is None  # stopped at the free text, before "Various"
    assert matches[0].release.id == 1815291 and matches[0].exact
    assert not matches[1].exact
    assert dl.confidence(matches) == dl.CONFIDENT
    # The Swiss folk LP "Various - Switzerland" has the album name but none of the songs: never exact
    folk = dl.parse_release(_release(8554332, title="Switzerland", artists=(("Various", ""),),
                                     tracklist=[{"position": "1", "title": "Alpenrose"}]))
    assert not dl.build_match(query, _facts(3, ["Mayday", "Mental Disaster", "Cry For Fame"]), folk).exact


def test_a_compilation_is_judged_by_its_track_titles_not_the_series_name():
    tracks = [{"position": str(i + 1), "title": t} for i, t in enumerate(["A", "B", "C", "D", "E"])]
    release = dl.parse_release(_release(7, title="Bloodstains Across Norway", artists=(("Various", ""),), tracklist=tracks))
    query = dl.DiscogsQuery("Bloodstains Across", "Bloodstains Across Norway")
    # Same album name, same track count, but none of the songs: never exact
    wrong = dl.build_match(query, _facts(5, NORWAY_FILE_TITLES), release)
    assert not wrong.exact and dl.confidence([wrong]) <= dl.FUZZY_CAP
    # Files without titles give no evidence either way: not exact
    blind = dl.build_match(query, _facts(5), release)
    assert not blind.exact


def test_a_band_album_never_turns_into_a_various_artists_one_by_name_alone():
    """"The Clash - Greatest Hits" must not be taken for "Various - Greatest Hits"."""
    release = dl.parse_release(_release(8, title="Greatest Hits", artists=(("Various", ""),),
                                        tracklist=[{"position": str(i + 1), "title": f"Song {i}"} for i in range(5)]))
    match = dl.build_match(dl.DiscogsQuery("The Clash", "Greatest Hits"),
                           _facts(5, ["London Calling", "Train in Vain", "Clampdown", "Bankrobber", "Rudie"]), release)
    assert not match.exact


@pytest.mark.parametrize("tagged", ["VA", "Various", "Various Artists", "V/A"])
def test_a_various_artists_tag_matches_discogs_various(tagged):
    release = dl.parse_release(_release(9, title="Bloodstains Across Norway", artists=(("Various", ""),),
                                        tracklist=NORWAY_TRACKS))
    match = dl.build_match(dl.DiscogsQuery(tagged, "Bloodstains Across Norway"), _facts(5), release)
    assert match.exact and match.score == 1.0


def test_title_agreement_counts_near_identical_titles_and_ignores_untitled_files():
    release = dl.parse_release(_release(1, tracklist=NORWAY_TRACKS))
    files = _facts(5, NORWAY_FILE_TITLES)
    agreement = dl.title_agreement(files, dl.assign_tracks(files, release))
    assert agreement == 0.8  # "CIA-KGB" vs "C.I.A. - K.G.B." is too far apart; "Uknown" and "Diggin' T.V." pass
    assert dl.title_agreement(_facts(5), dl.assign_tracks(_facts(5), release)) is None


def test_a_fuzzy_match_never_exceeds_sixty_percent():
    fake = FakeDiscogs(search_results=[_hit(1, "Miles Davis - Kind Of Blue")], releases={1: _release(1)})
    matches = dl.find_release(_client(fake), _facts(5), dl.DiscogsQuery("Miles Davis", "Kind of Blu", "1959"))
    assert not matches[0].exact
    assert 0 < dl.confidence(matches) <= 0.6
    assert dl.confidence([]) == 0.0


# ---------------------------------------------------------------------------
# Rate limit (HTTP 429) and throttling
# ---------------------------------------------------------------------------

def _http_error(code, reason="x"):
    return urllib.error.HTTPError("https://api.discogs.com/x", code, reason, {}, None)


def test_http_429_raises_a_clear_error_and_stops_all_further_requests():
    fake = FakeDiscogs(raises=_http_error(429, "Too Many Requests"))
    client = _client(fake)
    with pytest.raises(dl.DiscogsRateLimited) as info:
        client.get_json("database/search", q="x")
    assert "429" in str(info.value) and client.rate_limited
    calls = len(fake.requests)
    with pytest.raises(dl.DiscogsRateLimited):
        client.get_json("releases/1")
    with pytest.raises(dl.DiscogsRateLimited):
        client.get_image("https://i.discogs.com/a.jpg")
    assert len(fake.requests) == calls  # the batch stopped: nothing more went out


def test_other_http_errors_are_plain_discogs_errors_with_a_friendly_message():
    for code, word in ((401, "token"), (403, "token"), (500, "500")):
        with pytest.raises(dl.DiscogsError) as info:
            _client(FakeDiscogs(raises=_http_error(code))).get_json("releases/1")
        assert not isinstance(info.value, dl.DiscogsRateLimited) and word in str(info.value)


def test_network_failures_and_garbage_replies_become_discogs_errors():
    with pytest.raises(dl.DiscogsError):
        _client(FakeDiscogs(raises=urllib.error.URLError("offline"))).get_json("releases/1")

    class Garbage:
        def __call__(self, request):
            return b"<html>not json</html>"

    with pytest.raises(dl.DiscogsError):
        _client(Garbage()).get_json("releases/1")


def test_requests_are_paced_with_the_injected_clock():
    clock = FakeClock()
    throttle = dl.Throttle(1.1, clock=clock, sleep=clock.sleep)
    client = _client(FakeDiscogs(search_results=[], releases={1: _release(1)}), throttle)
    client.get_json("releases/1")
    assert clock.slept == []  # the first request goes straight out
    client.get_json("releases/1")
    assert clock.slept == [pytest.approx(1.1)]
    clock.now += 0.4
    client.get_json("releases/1")
    assert clock.slept[-1] == pytest.approx(0.7)
    clock.now += 5
    client.get_json("releases/1")
    assert len(clock.slept) == 2  # long enough since the last one: no wait


def test_the_default_pace_is_under_sixty_requests_a_minute():
    assert dl.MIN_REQUEST_INTERVAL >= 1.0


def test_pacing_and_transport_are_the_shared_lookup_client():
    from redactor_common.core import lookup_client

    assert dl.Throttle is lookup_client.Throttle
    assert dl._shared_throttle.interval == dl.MIN_REQUEST_INTERVAL
    with pytest.raises(ValueError) as info:
        dl._make_default_fetch()(f"file:///etc/passwd?token={TOKEN}")
    assert TOKEN not in str(info.value)


# ---------------------------------------------------------------------------
# The token never leaks
# ---------------------------------------------------------------------------

def test_token_is_scrubbed_from_error_text_and_never_in_a_url_or_repr():
    leaky = [
        OSError(f"connection reset while sending Authorization: Discogs token={TOKEN}"),
        urllib.error.URLError(f"proxy said {TOKEN}"),
        _http_error(500, f"bad token {TOKEN}"),
    ]
    for exc in leaky:
        fake = FakeDiscogs(raises=exc)
        client = _client(fake)
        with pytest.raises(dl.DiscogsError) as info:
            client.get_json("database/search", q="x", artist="y")
        assert TOKEN not in str(info.value) and TOKEN not in repr(info.value)
        assert info.value.__cause__ is None and (info.value.__suppress_context__ is True)
        assert all(TOKEN not in r.full_url for r in fake.requests)
        assert TOKEN not in repr(client) and TOKEN not in str(client)


def test_search_and_release_urls_never_contain_the_token():
    fake = _two_pressings_fake()
    dl.find_release(_client(fake), _facts(), dl.DiscogsQuery("Miles Davis", "Kind of Blue", "1959"))
    assert fake.requests and all(TOKEN not in r.full_url for r in fake.requests)
    assert all(r.get_header("Authorization") == f"Discogs token={TOKEN}" for r in fake.requests)


def test_the_token_goes_only_to_discogs_hosts():
    assert dl.is_discogs_url("https://api.discogs.com/releases/1")
    assert dl.is_discogs_url("https://i.discogs.com/x.jpg") and dl.is_discogs_url("https://discogs.com/x")
    for url in ("http://i.discogs.com/x.jpg", "https://evil.example/x.jpg", "https://discogs.com.evil.example/x",
                "https://notdiscogs.com/x", "file:///etc/passwd", ""):
        assert not dl.is_discogs_url(url)
    fake = FakeDiscogs(images={"https://cdn.example.org/cover.jpg": b"img"})
    assert _client(fake).get_image("https://cdn.example.org/cover.jpg") == b"img"
    assert not fake.requests[0].has_header("Authorization")


def test_a_redirect_away_from_discogs_drops_the_authorization_header():
    handler = dl._TokenOnlyToDiscogs()
    request = urllib.request.Request("https://i.discogs.com/a.jpg", headers={"Authorization": f"Discogs token={TOKEN}"})
    away = handler.redirect_request(request, None, 302, "Found", {}, "https://cdn.example.org/a.jpg")
    assert away is not None and not away.has_header("Authorization")
    home = handler.redirect_request(request, None, 302, "Found", {}, "https://img.discogs.com/a.jpg")
    assert home is not None and home.has_header("Authorization")
    assert handler.redirect_request(request, None, 302, "Found", {}, "ftp://example.org/a.jpg") is None


# ---------------------------------------------------------------------------
# Cover images
# ---------------------------------------------------------------------------

def test_cover_is_fetched_with_the_header_from_discogs_and_size_capped():
    release = dl.parse_release(_release())
    fake = FakeDiscogs(images={"https://i.discogs.com/primary.jpg": b"\xff\xd8jpeg"})
    assert dl.fetch_cover(_client(fake), release) == b"\xff\xd8jpeg"
    assert fake.requests[0].get_header("Authorization") == f"Discogs token={TOKEN}"
    big = FakeDiscogs(images={"https://i.discogs.com/primary.jpg": b"x" * (dl.MAX_COVER_BYTES + 1)})
    assert dl.fetch_cover(_client(big), release) is None
    assert dl.fetch_cover(_client(FakeDiscogs()), release) is None  # 404: no cover, no crash


def test_a_release_with_no_image_makes_no_request():
    release = dl.parse_release(_release(images=[]))
    fake = FakeDiscogs()
    assert dl.fetch_cover(_client(fake), release) is None and fake.requests == []


def test_an_http_cover_url_is_refused():
    fake = FakeDiscogs(images={"http://i.discogs.com/a.jpg": b"img"})
    assert _client(fake).get_image("http://i.discogs.com/a.jpg") is None and fake.requests == []


# ---------------------------------------------------------------------------
# Where the token lives
# ---------------------------------------------------------------------------

def test_token_comes_from_the_secret_store_under_its_permanent_name(fake_keyring):
    assert dl.load_token() == "" and not dl.has_token()
    secret_store.set_secret(APP, "discogs_token", f"  {TOKEN}  ")
    assert (dl.SECRET_APP, dl.SECRET_NAME) == (APP, "discogs_token")
    assert (f"redactor/{APP}", "discogs_token") in fake_keyring.items
    assert dl.load_token() == TOKEN and dl.has_token()


def test_the_environment_variable_is_honoured(monkeypatch):
    monkeypatch.setenv("DISCOGS_TOKEN", "env-token")
    assert dl.load_token() == "env-token"


def test_unconfigured_the_token_comes_only_from_the_environment(monkeypatch):
    monkeypatch.setattr(dl, "SECRET_APP", "")
    secret_store.set_secret(APP, "discogs_token", TOKEN)
    assert dl.load_token() == ""
    monkeypatch.setenv("DISCOGS_TOKEN", " env-token ")
    assert dl.load_token() == "env-token"


# ---------------------------------------------------------------------------
# A pasted link (2026-11-25)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("https://www.discogs.com/release/1234567-Miles-Davis-Kind-Of-Blue", ("release", 1234567)),
    ("https://www.discogs.com/de/release/42-Some-Title?foo=1", ("release", 42)),
    ("http://discogs.com/Miles-Davis-Kind-Of-Blue/release/99", ("release", 99)),
    ("https://www.discogs.com/master/5460-Miles-Davis-Kind-Of-Blue", ("master", 5460)),
    ("  1234  ", ("release", 1234)),
    ("[r77]", ("release", 77)),
    ("m88", ("master", 88)),
    ("https://www.discogs.com/artist/23755-Miles-Davis", None),
    ("Kind of Blue", None),
    ("", None),
    ("9" * 5000, None),  # pasted junk: no ValueError (Python's int() refuses over 4300 digits)
    ("²1", None),  # a superscript two passes str.isdigit() but int() refuses it
    ("https://www.discogs.com/release/" + "9" * 5000, None),
    ("[r" + "9" * 20 + "]", None),
])
def test_parse_discogs_link(text, expected):
    assert dl.parse_discogs_link(text) == expected


class _FakeWithMaster(FakeDiscogs):
    def __call__(self, request):
        if "/masters/" in request.full_url:
            self.requests.append(request)
            return json.dumps({"id": 5460, "main_release": 2}).encode()
        return super().__call__(request)


def test_release_from_link_fetches_the_release_or_a_masters_main_release():
    fake = _FakeWithMaster(releases={1: _release(1), 2: _release(2, year=1997)})
    client = _client(fake)
    assert dl.release_from_link(client, "https://www.discogs.com/release/1-Kind-Of-Blue").id == 1
    assert dl.release_from_link(client, "https://www.discogs.com/master/5460").id == 2
    assert not any("/database/search" in r.full_url for r in fake.requests)  # no search at all
    with pytest.raises(dl.DiscogsError, match="Not a Discogs release"):
        dl.release_from_link(client, "Kind of Blue")
    with pytest.raises(dl.DiscogsError, match="Not a Discogs release"):
        dl.release_from_link(client, "9" * 5000)

"""core/lookups/musicbrainz.py offline: canned MusicBrainz responses shaped like the real web service's JSON. Moved
with the module from mp3redactor (2026-11-04); the tag round trip and the lookup dialog stay tested there."""

import json
from pathlib import Path
from urllib.parse import unquote

import pytest

from redactor_common.core.lookup_client import Throttle
from redactor_common.core.lookups import musicbrainz as mb


def _release_json(release_id, title, country, tracks, date="2005", status="Official"):
    return {
        "id": release_id, "title": title, "date": date, "country": country, "status": status,
        "artist-credit": [{"name": "Johnny Cash", "joinphrase": ""}],
        "media": [{"position": 1, "format": "CD", "tracks": [
            {"position": pos, "number": str(pos), "title": t, "length": secs * 1000,
             "recording": {"id": f"rec-{pos}", "title": t, "length": secs * 1000}}
            for pos, t, secs in tracks
        ]}],
    }


RING_OF_FIRE = [(i, f"Song {i}", 150 + i) for i in range(1, 22)]
RING_OF_FIRE[5] = (6, "Big River", 151)
RING_OF_FIRE[17] = (18, "Personal Jesus", 199)


class FakeMusicBrainz:
    def __init__(self, releases, search_hits=None):
        self.releases = {r["id"]: r for r in releases}
        self.search_hits = search_hits if search_hits is not None else list(self.releases.values())
        self.urls = []

    def __call__(self, url):
        url = getattr(url, "full_url", url)
        self.urls.append(unquote(url))
        path = url.split("/ws/2/", 1)[1].split("?", 1)[0]
        if path == "release":
            hits = [dict(r, **{"track-count": sum(len(m["tracks"]) for m in r["media"])}) for r in self.search_hits]
            return json.dumps({"releases": hits}).encode()
        release_id = path.split("/", 1)[1]
        return json.dumps(self.releases[release_id]).encode()


def _files():
    return [mb.FileFacts(title="Big River", track=6, seconds=151.2), mb.FileFacts(title="Personal Jesus", track=18, seconds=199.4)]


def test_album_query_prefers_tags_then_folder_name():
    q = mb.album_query(["Ring of Fire", "Ring of Fire", ""], ["Johnny Cash"], Path("x/whatever"))
    assert (q.artist, q.album) == ("Johnny Cash", "Ring of Fire")
    q = mb.album_query(["", ""], ["", ""], Path("x/Johnny Cash - At Folsom Prison (1968)"))
    assert (q.artist, q.album) == ("Johnny Cash", "At Folsom Prison")
    q = mb.album_query([], [], Path("x/Greatest Hits"))
    assert (q.artist, q.album) == ("", "Greatest Hits")


def test_files_are_assigned_to_their_tracks():
    release = mb.Release(id="r1", title="Ring of Fire", artist="Johnny Cash", tracks=[
        mb.ReleaseTrack(1, pos, t, secs, f"rec-{pos}", "Johnny Cash") for pos, t, secs in RING_OF_FIRE
    ])
    match = mb.match_release(_files(), release)
    assert match.matched == 2 and match.score > 0.95
    assert match.assignment[0].position == 6 and match.assignment[1].position == 18


def test_titles_and_lengths_find_a_track_even_without_track_numbers():
    release = mb.Release(id="r1", title="T", artist="A", tracks=[
        mb.ReleaseTrack(1, pos, t, secs, f"rec-{pos}", "A") for pos, t, secs in RING_OF_FIRE
    ])
    match = mb.match_release([mb.FileFacts(title="big river", seconds=150)], release)
    assert match.assignment[0].position == 6


def test_a_recording_id_from_the_files_sound_is_a_certain_match():
    release = mb.Release(id="r1", title="T", artist="A", tracks=[mb.ReleaseTrack(1, 1, "Something Else", 300, "rec-x", "A")])
    match = mb.match_release([mb.FileFacts(title="junk", recordings={"rec-x": 0.9})], release)
    assert match.matched == 1 and match.score == 1.0


def test_a_wrong_album_scores_low():
    other = mb.Release(id="r2", title="Other", artist="Other", tracks=[
        mb.ReleaseTrack(1, i, f"Different {i}", 300 + i, f"x{i}", "Other") for i in range(1, 22)
    ])
    assert mb.match_release(_files(), other).matched == 0


def test_find_album_searches_then_ranks_editions():
    fake = FakeMusicBrainz([
        _release_json("za", "Ring of Fire: The Legend of Johnny Cash", "ZA", RING_OF_FIRE),
        _release_json("xe", "Ring of Fire: The Legend of Johnny Cash", "XE", RING_OF_FIRE, date="2005-11-21"),
        _release_json("bad", "Ring of Fire (bootleg)", "XE", [(1, "Nope", 10)], status="Bootleg"),
    ])
    matches = mb.find_album(_files(), mb.AlbumQuery("Johnny Cash", "Ring of Fire: The Legend of Johnny Cash"), fake)
    assert [m.release.id for m in matches][:2] == ["za", "xe"]
    assert matches[-1].release.id == "bad" and matches[-1].matched == 0
    assert 'release:"Ring of Fire: The Legend of Johnny Cash" AND artist:"Johnny Cash"' in fake.urls[0]


def test_search_falls_back_to_album_only():
    class Empty(FakeMusicBrainz):
        def __call__(self, url):
            url_text = unquote(getattr(url, "full_url", url))
            if "AND artist" in url_text:
                self.urls.append(url_text)
                return json.dumps({"releases": []}).encode()
            return super().__call__(url)

    fake = Empty([_release_json("za", "Ring of Fire", "ZA", RING_OF_FIRE)])
    assert [r.id for r in mb.search_releases(mb.AlbumQuery("Wrong Artist", "Ring of Fire"), fake)] == ["za"]


class _Searches(FakeMusicBrainz):
    """Answers a search only when its Lucene query is the one given; records every query."""

    def __init__(self, releases, answer_to):
        super().__init__(releases)
        self.answer_to = answer_to

    def __call__(self, url):
        url_text = unquote(getattr(url, "full_url", url))
        if "/ws/2/release?" in url_text and f"query={self.answer_to}&" not in url_text:
            self.urls.append(url_text)
            return json.dumps({"releases": []}).encode()
        return super().__call__(url)

    def queries(self):
        return [u.split("query=", 1)[1].split("&", 1)[0] for u in self.urls if "query=" in u]


def test_a_series_name_split_over_album_artist_and_album_is_searched_as_one_title():
    """"Bloodstains Across Switzerland" (2026-11-25): album artist "Bloodstains Across",
    album "  Switzerland". The album alone finds 371 releases called "Switzerland"
    (Electric Six first); the name + album as one title finds the compilation."""
    fake = _Searches([_release_json("ch", "Bloodstains Across Switzerland", "XE", RING_OF_FIRE)],
                     'release:"Bloodstains Across Switzerland"')
    query = mb.album_query(["  Switzerland"], ["Bloodstains Across"], Path("x"))
    assert [r.id for r in mb.search_releases(query, fake)] == ["ch"]
    assert fake.queries() == [
        'release:"Switzerland" AND artist:"Bloodstains Across"',
        'release:"Bloodstains Across Switzerland"',
    ]


def test_search_order_when_nothing_is_found():
    fake = _Searches([], "nothing")
    assert mb.search_releases(mb.AlbumQuery("Bloodstains Across", "Bloodstains Across Norway"), fake) == []
    # the album already starts with the artist name: no "name + album" search
    assert fake.queries() == [
        'release:"Bloodstains Across Norway" AND artist:"Bloodstains Across"',
        'release:"Bloodstains Across Norway" AND artist:"Various Artists"',
        'release:"Bloodstains Across Norway"',
    ]


@pytest.mark.parametrize("tagged", ["VA", "V/A", "Various", "Various Artists"])
def test_a_various_artists_tag_is_searched_as_various_artists(tagged):
    fake = _Searches([], "nothing")
    mb.search_releases(mb.AlbumQuery(tagged, "Bloodstains Across Norway"), fake)
    assert fake.queries() == [
        'release:"Bloodstains Across Norway" AND artist:"Various Artists"',
        'release:"Bloodstains Across Norway"',
    ]


def test_a_busy_503_is_retried_after_a_pause(monkeypatch):
    import urllib.error
    slept = []
    monkeypatch.setattr(mb, "_sleep", slept.append)
    calls = []

    def busy_twice(url):
        calls.append(url)
        if len(calls) <= 2:
            raise urllib.error.HTTPError("https://musicbrainz.org/ws/2/x", 503, "Service Temporarily Unavailable", {}, None)
        return b'{"id": "abc", "title": "T"}'

    assert mb.fetch_release("abc", busy_twice).title == "T"
    assert slept == list(mb.RETRY_503[:2])

    def always_busy(url):
        raise urllib.error.HTTPError("https://musicbrainz.org/ws/2/x", 503, "Service Temporarily Unavailable", {}, None)

    slept.clear()
    with pytest.raises(mb.MusicBrainzError, match="busy"):
        mb.fetch_release("abc", always_busy)
    assert slept == list(mb.RETRY_503)


def test_find_album_by_recordings_fetches_and_ranks_the_given_releases():
    fake = FakeMusicBrainz([_release_json("za", "Ring of Fire", "ZA", RING_OF_FIRE)])
    matches = mb.find_album_by_recordings(_files(), ["za"], fake)
    assert [m.release.id for m in matches] == ["za"] and matches[0].matched == 2


def test_fields_for_a_matched_file():
    release = mb.fetch_release("za", FakeMusicBrainz([_release_json("za", "Ring of Fire", "ZA", RING_OF_FIRE)]))
    fields = mb.fields_for(release, release.tracks[5])
    assert fields == {
        "title": "Big River", "artist": "Johnny Cash", "albumartist": "Johnny Cash", "album": "Ring of Fire",
        "track": "6", "year": "2005", "musicbrainz_albumid": "za", "musicbrainz_trackid": "rec-6",
    }


def test_fields_for_drops_an_unknown_track_position_and_disc():
    release = mb.fetch_release("za", FakeMusicBrainz([_release_json("za", "Ring of Fire", "ZA", RING_OF_FIRE)]))
    track = release.tracks[5]
    track.position = 0
    track.disc = 0
    release.tracks[0].disc = 2  # makes it a multi-disc release
    fields = mb.fields_for(release, track)
    assert "track" not in fields and "discnumber" not in fields


def test_null_status_and_date_in_a_release_do_not_crash():
    release = mb.fetch_release("abc", fetch=lambda url: '{"id": "abc", "title": "T", "status": null, "date": null, "country": null}')
    assert (release.status, release.date, release.country) == ("", "", "")


def test_requests_go_through_the_shared_throttle():
    assert isinstance(mb._throttle, Throttle) and mb._throttle.interval == mb.MIN_REQUEST_INTERVAL


def test_configure_sets_the_user_agent_and_keeps_the_one_pace(monkeypatch):
    for name in ("USER_AGENT", "_raw_fetch", "_default_fetch"):
        monkeypatch.setattr(mb, name, getattr(mb, name))
    throttle = mb._throttle
    mb.configure("testredactor/1.0 ( https://example.invalid )")
    assert mb.USER_AGENT == "testredactor/1.0 ( https://example.invalid )"
    assert mb._throttle is throttle


# A pasted link (2026-11-25)
MBID = "5c2b4e7a-1f3d-4a8e-9c0b-1234567890ab"


def test_parse_musicbrainz_link():
    assert mb.parse_musicbrainz_link(f"https://musicbrainz.org/release/{MBID}") == ("release", MBID)
    assert mb.parse_musicbrainz_link(f"https://musicbrainz.org/release/{MBID.upper()}/discids") == ("release", MBID)
    assert mb.parse_musicbrainz_link(f"https://beta.musicbrainz.org/release-group/{MBID}") == ("release-group", MBID)
    assert mb.parse_musicbrainz_link(f" {MBID} ") == ("release", MBID)
    assert mb.parse_musicbrainz_link(f"https://musicbrainz.org/artist/{MBID}") is None
    assert mb.parse_musicbrainz_link("Ring of Fire") is None


def test_release_ids_from_link_lists_a_release_groups_releases():
    fake = FakeMusicBrainz([_release_json("za", "Ring of Fire", "ZA", RING_OF_FIRE),
                            _release_json("xe", "Ring of Fire", "XE", RING_OF_FIRE)])
    assert mb.release_ids_from_link(f"https://musicbrainz.org/release/{MBID}", fake) == [MBID]
    assert fake.urls == []  # a release link needs no request
    assert mb.release_ids_from_link(f"https://musicbrainz.org/release-group/{MBID}", fake) == ["za", "xe"]
    assert f"release-group={MBID}" in fake.urls[0]
    with pytest.raises(mb.MusicBrainzError, match="Not a MusicBrainz"):
        mb.release_ids_from_link("Ring of Fire", fake)

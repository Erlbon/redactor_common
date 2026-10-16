"""Tests for the shared source-id vocabulary (core/source_ids.py): ids, links, wire form, hostile input."""

import pytest

from redactor_common.core.source_ids import (
    LinkError, SERVICES, SourceIdError, SourceRef, build_link, explain_link, format_label, from_wire, is_valid_id,
    kinds, make_ref, normalise_id, parse_link, services, wire,
)

U = "2bf704b3-1d6f-4a3c-9b11-0c5b1c3a7e55"
U2 = "d284e2a0-789d-4e6c-beb9-6e61c24e8b84"

GOOD_IDS = [
    ("musicbrainz", "artist", U.upper(), U), ("musicbrainz", "recording", f" {U} ", U),
    ("discogs", "release", "249504", "249504"), ("wikidata", "item", "q42", "Q42"),
    ("wikipedia", "article", "EN:Leningrad_(band)", "en:Leningrad (band)"),
    ("wikipedia", "article", "en:Category:Rock", "en:Category:Rock"),
    ("theporndb", "scene", U2.upper(), U2), ("theporndb", "performer", "Jessica-Jaymes", "jessica-jaymes"),
    ("stashdb", "studio", U2, U2), ("tmdb", "movie", "603", "603"), ("tmdb", "season", "1396/0", "1396/0"),
    ("tmdb", "episode", "1396/1/2", "1396/1/2"), ("imdb", "title", "TT0133093", "tt0133093"),
    ("imdb", "name", "nm0000206", "nm0000206"), ("tvdb", "series", "73244", "73244"),
    ("openlibrary", "work", "ol45883w", "OL45883W"), ("openlibrary", "edition", "OL7353617M", "OL7353617M"),
    ("googlebooks", "volume", "zyTCAlFPjgYC", "zyTCAlFPjgYC"),
    ("isfdb", "publication", "12345", "12345"), ("comicvine", "issue", "12345", "4000-12345"),
    ("comicvine", "volume", "4050-678", "4050-678"), ("gcd", "issue", "99", "99"),
]


@pytest.mark.parametrize("service,kind,text,canonical", GOOD_IDS)
def test_valid_ids_are_canonicalised(service, kind, text, canonical):
    assert normalise_id(service, kind, text) == canonical
    ref = SourceRef(service, kind, text)
    assert ref.id == canonical
    assert from_wire(wire(ref)) == ref          # wire round trip
    assert parse_link(build_link(ref)) == ref   # link round trip (every built link parses back, except slug-less tvdb: see below)


BAD_IDS = [
    ("musicbrainz", "artist", U[:-1]), ("musicbrainz", "artist", U + "0"), ("musicbrainz", "artist", "x" * 36),
    ("discogs", "release", "0"), ("discogs", "release", "12a"), ("discogs", "release", "-5"), ("discogs", "release", "1" * 40),
    ("wikidata", "item", "P31"), ("wikidata", "item", "Q"), ("wikipedia", "article", "Title"), ("wikipedia", "article", "en:"),
    ("wikipedia", "article", "en:A|B"), ("wikipedia", "article", "en:Special:Random"), ("wikipedia", "article", "e:Title"),
    ("tmdb", "movie", "tt1"), ("tmdb", "season", "1396"), ("tmdb", "episode", "1/2"), ("tmdb", "season", "1396/x"),
    ("imdb", "title", "nm1"), ("imdb", "name", "tt1"), ("imdb", "title", "tt"), ("openlibrary", "work", "OL1M"),
    ("openlibrary", "author", "OL1"), ("googlebooks", "volume", "short"), ("comicvine", "issue", "4050-1"),
    ("comicvine", "volume", "4000-1"), ("stashdb", "scene", "a-slug"), ("theporndb", "scene", "Not A Slug"),
    ("theporndb", "scene", "../etc/passwd"), ("tvdb", "series", "the-office"),
]


@pytest.mark.parametrize("service,kind,text", BAD_IDS)
def test_invalid_ids_are_rejected(service, kind, text):
    with pytest.raises(SourceIdError):
        normalise_id(service, kind, text)
    assert not is_valid_id(service, kind, text)
    assert make_ref(service, kind, text) is None
    with pytest.raises(SourceIdError):
        SourceRef(service, kind, text)


def test_unknown_service_kind_and_non_text():
    for args in (("nope", "x", "1"), ("tmdb", "episodes", "1"), (None, "movie", "1"), ("tmdb", None, "1")):
        with pytest.raises(SourceIdError):
            normalise_id(*args)
    with pytest.raises(SourceIdError):
        normalise_id("tmdb", "movie", 603)  # type: ignore[arg-type]


def test_vocabulary_is_complete_and_labelled():
    assert set(services()) == {"musicbrainz", "discogs", "wikipedia", "wikidata", "theporndb", "stashdb", "tmdb", "imdb",
                               "tvdb", "openlibrary", "googlebooks", "isfdb", "comicvine", "gcd"}
    assert kinds("musicbrainz") == ["artist", "release-group", "release", "recording", "label"]
    assert kinds("tmdb") == ["movie", "tv", "season", "episode", "person"]
    assert kinds("nope") == []
    assert format_label("tmdb") == "TMDB" and format_label("imdb") == "IMDb" and format_label("theporndb") == "ThePornDB"
    assert format_label("unknown-thing") == "unknown-thing"
    assert all(SERVICES[s]["label"] for s in SERVICES)


def test_wikipedia_properties():
    ref = SourceRef("wikipedia", "article", "de:Köln: Dom")
    assert (ref.lang, ref.title) == ("de", "Köln: Dom")
    assert SourceRef("tmdb", "movie", "1").lang == ""
    assert parse_link(build_link(ref)) == ref


LINKS = [
    ("https://musicbrainz.org/artist/" + U, "musicbrainz", "artist", U),
    ("http://www.musicbrainz.org/release-group/" + U + "/", "musicbrainz", "release-group", U),
    ("musicbrainz.org/release/" + U + "?x=1#frag", "musicbrainz", "release", U),
    ("https://musicbrainz.org/recording/" + U.upper(), "musicbrainz", "recording", U),
    ("https://beta.musicbrainz.org/label/" + U, "musicbrainz", "label", U),
    ("https://www.discogs.com/artist/1234-Some-Name", "discogs", "artist", "1234"),
    ("https://www.discogs.com/release/249504-Never-Gonna-Give-You-Up", "discogs", "release", "249504"),
    ("https://www.discogs.com/master/view/55", "discogs", "master", "55"),
    ("https://www.discogs.com/fr/master/55-Name", "discogs", "master", "55"),
    ("https://en.wikipedia.org/wiki/Leningrad_(band)", "wikipedia", "article", "en:Leningrad (band)"),
    ("https://en.m.wikipedia.org/wiki/Leningrad_(band)#History", "wikipedia", "article", "en:Leningrad (band)"),
    ("http://no.wikipedia.org/wiki/K%C3%B8benhavn?oldid=1", "wikipedia", "article", "no:København"),
    ("https://en.wikipedia.org/w/index.php?title=Foo_Bar&action=edit", "wikipedia", "article", "en:Foo Bar"),
    ("https://www.wikidata.org/wiki/Q42", "wikidata", "item", "Q42"),
    ("https://www.wikidata.org/entity/Q42", "wikidata", "item", "Q42"),
    ("https://m.wikidata.org/wiki/Special:EntityData/Q42.json", "wikidata", "item", "Q42"),
    ("https://theporndb.net/scenes/" + U2, "theporndb", "scene", U2),
    ("https://theporndb.net/scenes/some-scene-slug/", "theporndb", "scene", "some-scene-slug"),
    ("https://theporndb.net/performers/jessica-jaymes", "theporndb", "performer", "jessica-jaymes"),
    ("https://theporndb.net/sites/" + U2, "theporndb", "site", U2),
    ("https://stashdb.org/scenes/" + U2, "stashdb", "scene", U2),
    ("https://stashdb.org/performers/" + U2 + "/", "stashdb", "performer", U2),
    ("https://stashdb.org/studios/" + U2, "stashdb", "studio", U2),
    ("https://www.themoviedb.org/movie/603-the-matrix", "tmdb", "movie", "603"),
    ("https://www.themoviedb.org/movie/603", "tmdb", "movie", "603"),
    ("https://www.themoviedb.org/tv/1396-breaking-bad/", "tmdb", "tv", "1396"),
    ("https://www.themoviedb.org/tv/1396-breaking-bad/season/1", "tmdb", "season", "1396/1"),
    ("https://www.themoviedb.org/tv/1396/season/1/episode/2?language=en-US", "tmdb", "episode", "1396/1/2"),
    ("https://www.themoviedb.org/person/6384-keanu-reeves", "tmdb", "person", "6384"),
    ("https://www.themoviedb.org/nb-NO/movie/603-the-matrix", "tmdb", "movie", "603"),
    ("https://www.imdb.com/title/tt0133093/", "imdb", "title", "tt0133093"),
    ("https://m.imdb.com/title/tt0133093/reviews?ref_=x", "imdb", "title", "tt0133093"),
    ("imdb.com/name/nm0000206", "imdb", "name", "nm0000206"),
    ("https://www.imdb.com/de/title/tt0133093/", "imdb", "title", "tt0133093"),
    ("https://thetvdb.com/dereferrer/series/73244", "tvdb", "series", "73244"),
    ("https://thetvdb.com/dereferrer/movie/12", "tvdb", "movie", "12"),
    ("https://thetvdb.com/series/the-office/episodes/9876", "tvdb", "episode", "9876"),
    ("https://thetvdb.com/?tab=series&id=73244", "tvdb", "series", "73244"),
    ("https://openlibrary.org/works/OL45883W/Title_Here", "openlibrary", "work", "OL45883W"),
    ("https://openlibrary.org/books/OL7353617M", "openlibrary", "edition", "OL7353617M"),
    ("https://openlibrary.org/authors/OL23919A/J._K._Rowling", "openlibrary", "author", "OL23919A"),
    ("https://books.google.com/books?id=zyTCAlFPjgYC&hl=en", "googlebooks", "volume", "zyTCAlFPjgYC"),
    ("https://books.google.co.uk/books/about/Title.html?id=zyTCAlFPjgYC", "googlebooks", "volume", "zyTCAlFPjgYC"),
    ("https://books.google.no/books?id=zyTCAlFPjgYC", "googlebooks", "volume", "zyTCAlFPjgYC"),
    ("https://www.isfdb.org/cgi-bin/title.cgi?12345", "isfdb", "title", "12345"),
    ("http://isfdb.org/cgi-bin/pl.cgi?777", "isfdb", "publication", "777"),
    ("https://www.isfdb.org/cgi-bin/ea.cgi?55", "isfdb", "author", "55"),
    ("https://www.isfdb.org/cgi-bin/seriesgrid.cgi?9", "isfdb", "series", "9"),
    ("https://comicvine.gamespot.com/batman-1/4000-12345/", "comicvine", "issue", "4000-12345"),
    ("https://comicvine.gamespot.com/batman/4050-796/", "comicvine", "volume", "4050-796"),
    ("https://www.comics.org/issue/12345/", "gcd", "issue", "12345"),
    ("https://www.comics.org/series/678/", "gcd", "series", "678"),
]


@pytest.mark.parametrize("url,service,kind,ident", LINKS)
def test_links_parse(url, service, kind, ident):
    ref = parse_link(url)
    assert ref == SourceRef(service, kind, ident), url
    assert parse_link(build_link(ref)) == ref
    assert parse_link(f"  {url}  ") == ref  # surrounding blanks are fine


HOSTILE = [
    "https://musicbrainz.org.evil.com/artist/" + U, "https://evilmusicbrainz.org/artist/" + U,
    "https://musicbrainz.org@evil.com/artist/" + U, "https://evil.com/?u=musicbrainz.org/artist/" + U,
    "https://evil.com/musicbrainz.org/artist/" + U, "https://musicbrainz.org:8080/artist/" + U,
    "https://user:pw@musicbrainz.org/artist/" + U, "https://evil.com" + chr(92) + "@musicbrainz.org/artist/" + U,
    "https://musicbrainz.org.evil.com", "https://test.musicbrainz.org/artist/" + U,
    "https://musicbra\u0131nz.org/artist/" + U, "https://xn--musicbrainz-x.org/artist/" + U,
    "https://themoviedb.org.attacker.net/movie/603", "https://imdb.com.evil.io/title/tt0133093/",
    "https://books.google.evil.com/books?id=zyTCAlFPjgYC", "https://books.google.com.evil.com/books?id=zyTCAlFPjgYC",
    "https://notbooks.google.com/books?id=zyTCAlFPjgYC", "https://thetvdb.com.evil.com/dereferrer/series/1",
    "https://wikipedia.org.evil.com/wiki/X", "https://en.wikipedia.org.evil.com/wiki/X",
    "javascript:alert(1)", "file:///C:/Windows/win.ini", "ftp://musicbrainz.org/artist/" + U, "data:text/html,<b>x</b>",
    "//musicbrainz.org/artist/" + U, "musicbrainz.org", "", "   ", "https://", "not a link at all",
    "https://musicbrainz.org/artist/" + U + "\nhttps://evil.com", "https://musicbrainz.org/artist/" + U + "%0Ainjected",
    "https://en.wikipedia.org/wiki/Foo%0ABar", "https://en.wikipedia.org/wiki/Foo%00Bar",
    "https://en.wikipedia.org/wiki/Foo%23Bar", "https://en.wikipedia.org/wiki/Special:Random",
    "https://www.wikipedia.org/wiki/Foo", "https://en.wikipedia.org/", "https://a.b.wikipedia.org/wiki/Foo",
    "https://www.themoviedb.org/movie/abc", "https://www.themoviedb.org/movie/0", "https://www.themoviedb.org/",
    "https://www.imdb.com/title/tt/", "https://www.imdb.com/title/nm0000206/", "https://www.imdb.com/name/tt0133093/",
    "https://thetvdb.com/series/the-office", "https://theporndb.net/scenes/Not%20A%20Slug", "https://theporndb.net/scenes/",
    "https://stashdb.org/scenes/some-slug", "https://comicvine.gamespot.com/batman/4005-12/",
    "https://openlibrary.org/works/OL1M", "https://books.google.com/books", "https://www.isfdb.org/cgi-bin/title.cgi",
    "https://www.isfdb.org/cgi-bin/title.cgi?abc", "https://musicbrainz.org/artist/" + "z" * 36,
    "https://musicbrainz.org/place/" + U, "http://[::1/artist/" + U, "https://musicbrainz.org/artist/" + U + "a" * 3000,
]


@pytest.mark.parametrize("url", HOSTILE)
def test_hostile_and_malformed_links_are_refused(url):
    assert parse_link(url) is None
    with pytest.raises(LinkError):
        explain_link(url)


def test_non_text_input_is_refused():
    assert parse_link(None) is None            # type: ignore[arg-type]
    assert parse_link(603) is None             # type: ignore[arg-type]
    assert parse_link(b"https://imdb.com/title/tt1/") is None  # type: ignore[arg-type]


def test_bare_uuid_needs_bare_as():
    assert parse_link(U) is None
    assert parse_link(U.upper(), bare_as=("musicbrainz", "release-group")) == SourceRef("musicbrainz", "release-group", U)
    assert parse_link(U, bare_as=("musicbrainz", "artist")).kind == "artist"
    assert parse_link("12345", bare_as=("musicbrainz", "artist")) is None


def test_error_messages_are_plain_words():
    with pytest.raises(LinkError, match="Paste a link"):
        explain_link("")
    with pytest.raises(LinkError, match="evil.com is not one of them"):
        explain_link("https://evil.com/x")
    with pytest.raises(LinkError, match="artist, release group"):
        explain_link("https://musicbrainz.org/place/" + U)
    with pytest.raises(LinkError, match="not an article"):
        explain_link("https://en.wikipedia.org/wiki/")


MP3_CASES = [  # what mp3redactor's core/notes_link.py accepted: the same answers here (kind names mapped)
    ("https://musicbrainz.org/artist/" + U, ("musicbrainz", "artist", U)),
    ("https://musicbrainz.org/release-group/" + U.upper(), ("musicbrainz", "release-group", U)),
    ("musicbrainz.org/artist/" + U, ("musicbrainz", "artist", U)),
    ("https://www.discogs.com/artist/123-X", ("discogs", "artist", "123")),
    ("https://www.discogs.com/release/456-X", ("discogs", "release", "456")),
    ("https://www.discogs.com/master/789-X", ("discogs", "master", "789")),
    ("https://en.wikipedia.org/wiki/Leningrad_(band)", ("wikipedia", "article", "en:Leningrad (band)")),
    ("https://sv.wikipedia.org/wiki/Foo_Bar", ("wikipedia", "article", "sv:Foo Bar")),
    ("https://www.wikidata.org/wiki/Q123", ("wikidata", "item", "Q123")),
    ("https://www.wikidata.org/entity/Q123", ("wikidata", "item", "Q123")),
    ("https://www.wikidata.org/something/Q123", ("wikidata", "item", "Q123")),
]


@pytest.mark.parametrize("url,expected", MP3_CASES)
def test_superset_of_the_mp3redactor_parser(url, expected):
    assert parse_link(url) == SourceRef(*expected)


def test_wire_form():
    assert wire(SourceRef("tmdb", "movie", "603")) == "tmdb:movie:603"
    assert wire(SourceRef("wikipedia", "article", "en:Foo: Bar")) == "wikipedia:article:en:Foo: Bar"
    assert str(SourceRef("imdb", "title", "tt1")) == "imdb:title:tt1"
    assert from_wire("wikipedia:article:en:Foo: Bar") == SourceRef("wikipedia", "article", "en:Foo: Bar")
    assert from_wire(" TMDB : Movie : 603 ") == SourceRef("tmdb", "movie", "603")
    for bad in ("", "tmdb", "tmdb:movie", "tmdb:movie:abc", "x:y:z", "tmdb:movie:6\n03", "tmdb:movie:603\x00", None, 5,
                "tmdb:movie:" + "9" * 600):
        assert from_wire(bad) is None, bad  # type: ignore[arg-type]


def test_wire_is_one_line_for_unicode_titles():
    ref = SourceRef("wikipedia", "article", "ja:東京_タワー")
    text = wire(ref)
    assert "\n" not in text and from_wire(text) == ref
    assert parse_link(build_link(ref)) == ref
    assert build_link(ref).startswith("https://ja.wikipedia.org/wiki/")


def test_every_kind_round_trips_a_sample_through_link_and_wire():
    samples = {"uuid": U, "digits": "123", "tt": "tt1234567", "nm": "nm1234567"}
    done = 0
    for service, spec in SERVICES.items():
        for kind in spec["kinds"]:
            candidates = [U, "123", "tt1234567", "nm1234567", "Q5", "en:Foo", "OL5M", "OL5W", "OL5A", "zyTCAlFPjgYC", "123/1", "123/1/2",
                          "4000-5", "4050-5"]
            ref = next((make_ref(service, kind, c) for c in candidates if is_valid_id(service, kind, c)), None)
            assert ref is not None, (service, kind)
            assert from_wire(wire(ref)) == ref
            link = build_link(ref)
            assert link.startswith("https://")
            if service != "tvdb" or True:
                assert parse_link(link) == ref, link
            done += 1
    assert done == sum(len(s["kinds"]) for s in SERVICES.values())


def test_trailing_line_break_from_a_paste_is_ignored():
    assert parse_link("https://musicbrainz.org/artist/" + U + "\n") == SourceRef("musicbrainz", "artist", U)

"""The music section of core/wikipedia.py (folded in from mp3redactor's core/wikipedia_lookup.py, 2026-11-04): which
article is the band, the album or the genre. Canned replies only."""

import json
from urllib.parse import parse_qs, unquote, urlsplit

from redactor_common.core import wikipedia as wp


def _summary(title, extract, description="", kind="standard"):
    return {"title": title, "extract": extract, "description": description, "type": kind,
            "content_urls": {"desktop": {"page": f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}"}}}


class FakeWikipedia:
    def __init__(self, pages=None, searches=None):
        self.pages = pages or {}
        self.searches = searches or {}
        self.urls = []

    def __call__(self, url):
        url = getattr(url, "full_url", url)
        self.urls.append(unquote(url))
        parts = urlsplit(url)
        if "/page/summary/" in parts.path:
            title = unquote(parts.path.rsplit("/", 1)[1]).replace("_", " ")
            if title not in self.pages:
                raise _not_found(url)
            return json.dumps(self.pages[title]).encode()
        query = parse_qs(parts.query).get("q", [""])[0]
        return json.dumps({"pages": [{"title": t} for t in self.searches.get(query, [])]}).encode()


def _not_found(url):
    import io
    import urllib.error
    return urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b""))


VIENNA = _summary("Vienna (Ultravox album)", "Vienna is the fourth studio album by British new wave band Ultravox.",
                  "1980 studio album by Ultravox")
ULTRAVOX = _summary("Ultravox", "Ultravox were a British new wave band formed in London in 1974. Their album Vienna ...",
                    "British band")


def test_an_album_page_must_say_album_and_name_the_artist():
    page = wp.Page(**{k: VIENNA[k] for k in ("title", "extract", "description")})
    assert wp.looks_like_music(page, "album", "Vienna", "Ultravox")
    assert not wp.looks_like_music(page, "album", "Vienna", "Billy Joel")
    single = wp.Page("Vienna (song)", "Vienna is a song by Ultravox.", "1981 single by Ultravox")
    assert not wp.looks_like_music(single, "album", "Vienna", "Ultravox")


def test_an_artist_page_needs_a_music_word_and_the_name_as_title_or_an_album_in_the_lead():
    page = wp.Page("Ultravox", ULTRAVOX["extract"], "British band")
    assert wp.looks_like_music(page, "artist", "Ultravox")
    city = wp.Page("Leningrad", "Leningrad was the name of Saint Petersburg from 1924 to 1991.", "city in Russia")
    assert not wp.looks_like_music(city, "artist", "Leningrad")
    other = wp.Page("Ultravox (US band)", "Ultravox were an American rock band.", "band")
    assert not wp.looks_like_music(other, "artist", "Ultravox (UK)", context_terms=["Vienna"])


def test_the_compare_form_is_the_callers():
    page = wp.Page("Maroon 5", "Maroon 5 is an American pop rock band. Their debut album was Songs About Jane.",
                   "American band")
    albums = ["Songs About Jane"]
    assert not wp.looks_like_music(page, "artist", "Maroon Five", context_terms=albums)
    as_digits = lambda text: wp.fold(text).replace("five", "5")  # noqa: E731
    assert wp.looks_like_music(page, "artist", "Maroon Five", context_terms=albums, compare=as_digits)
    assert wp.mentions("The band Maroon 5", "Maroon Five", as_digits)


def test_find_music_page_trusts_the_musicbrainz_link_first():
    fake = FakeWikipedia(pages={"Vienna (Ultravox album)": VIENNA})
    page = wp.find_music_page("album", "Vienna", artist="Ultravox", linked_title="Vienna (Ultravox album)", fetch=fake)
    assert page.title == "Vienna (Ultravox album)" and page.via == "musicbrainz link"


def test_find_music_page_searches_and_verifies_when_there_is_no_link():
    fake = FakeWikipedia(pages={"Vienna (Ultravox album)": VIENNA},
                         searches={"Vienna (Ultravox album)": ["Vienna (Ultravox album)"]})
    notes = []
    page = wp.find_music_page("album", "Vienna", artist="Ultravox", fetch=fake, notes=notes)
    assert page.via == "search" and notes == []
    nothing = wp.find_music_page("album", "Nowhere", artist="Nobody", fetch=FakeWikipedia(), notes=notes)
    assert nothing is None and notes == ["no Wikipedia page was found and verified"]


def test_find_artist_page_tries_the_band_title_first():
    band = _summary("Leningrad (band)", "Leningrad is a Russian ska punk band from Saint Petersburg.", "Russian band")
    fake = FakeWikipedia(pages={"Leningrad (band)": band})
    page = wp.find_artist_page("Leningrad", [], fake)
    assert page.title == "Leningrad (band)" and page.via == "the title 'Leningrad (band)'"


def test_music_identifiers_reads_the_musicbrainz_and_discogs_ids():
    def fake(url):
        return json.dumps({"entities": {"Q1": {"claims": {
            "P434": [{"mainsnak": {"datavalue": {"value": "mbid-1"}}}],
            "P1953": [{"mainsnak": {"datavalue": {"value": "123"}}}],
        }}}}).encode()

    assert wp.music_identifiers(title="Ultravox", fetch=fake) == {"qid": "Q1", "musicbrainz": "mbid-1", "discogs": "123"}

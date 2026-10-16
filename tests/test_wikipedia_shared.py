"""The shared Wikipedia client (core/wikipedia.py) on injected fetchers: nothing reaches the network."""

import json
import urllib.error

import pytest

from redactor_common.core import wikipedia as wp


def fetcher(routes):
    """fetch(url) -> bytes answering the first route whose text is in the URL; None answers 404."""
    urls = []

    def fetch(request):
        url = request if isinstance(request, str) else request.full_url
        urls.append(url)
        for part, body in routes:
            if part in url:
                if body is None:
                    raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
                return json.dumps(body).encode()
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    fetch.urls = urls
    return fetch


SUMMARY = {"title": "Breaking Bad", "extract": "Breaking Bad is an American crime drama television series. " * 3,
           "description": "American TV series", "type": "standard",
           "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Breaking_Bad"}}}


def test_get_summary_reads_the_lead_and_the_link():
    page = wp.get_summary("Breaking Bad", fetcher([("page/summary/Breaking_Bad", SUMMARY)]))
    assert page.title == "Breaking Bad" and page.url.endswith("/wiki/Breaking_Bad") and "crime drama" in page.extract
    assert not page.is_disambiguation


def test_a_missing_article_is_none_not_an_error():
    assert wp.get_summary("No Such Show", fetcher([])) is None
    assert wp.get_summary("", fetcher([])) is None


def test_a_disambiguation_page_is_not_usable():
    page = wp.get_summary("Mercury", fetcher([("Mercury", {"title": "Mercury", "extract": "Mercury may refer to", "type": "disambiguation"})]))
    assert page.is_disambiguation and wp.usable(page) is None


def test_search_titles_strips_the_excerpt_markup():
    hits = wp.search_titles("breaking bad", fetcher([("search/title", {"pages": [
        {"title": "Breaking Bad", "description": "TV series", "excerpt": "<b>Breaking</b> Bad"}]})]))
    assert [(h.title, h.excerpt) for h in hits] == [("Breaking Bad", "Breaking Bad")]


def test_wikidata_claims_returns_the_first_string_value_per_property():
    entity = {"entities": {"Q1079": {"claims": {
        "P345": [{"mainsnak": {"datavalue": {"value": "tt0903747"}}}],
        "P4947": [{"mainsnak": {"datavalue": {"value": "1396"}}}]}}}}
    found = wp.wikidata_claims({"P345": "imdb", "P4947": "tmdb_movie", "P9": "other"}, title="Breaking Bad",
                               fetch=fetcher([("wbgetentities", entity)]))
    assert found == {"qid": "Q1079", "imdb": "tt0903747", "tmdb_movie": "1396", "other": ""}
    assert wp.wikidata_claims({"P345": "imdb"}, entity_id="not-a-qid", fetch=fetcher([]))["qid"] == ""


def test_title_from_wikidata_and_urls():
    answer = {"entities": {"Q1079": {"sitelinks": {"enwiki": {"title": "Breaking Bad"}}}}}
    assert wp.title_from_wikidata("Q1079", fetcher([("sitelinks", answer)])) == "Breaking Bad"
    assert wp.title_from_url("https://en.wikipedia.org/wiki/Vienna_(Ultravox_album)") == "Vienna (Ultravox album)"
    assert wp.language_of_url("https://nb.wikipedia.org/wiki/Oslo") == "nb"
    assert wp.title_from_url("https://example.org/wiki/X") == ""


def test_unreachable_service_raises_wikipedia_error():
    def down(request):
        raise urllib.error.URLError("no network")

    with pytest.raises(wp.WikipediaError):
        wp.get_summary("Anything", down)


def test_text_helpers():
    assert wp.fold("Café & Co.") == "cafe and co"
    assert wp.strip_parenthetical("Vienna (Ultravox album)") == "Vienna"
    assert wp.trim_extract("One. Two. Three.", 10) == "One. Two."
    assert wp.trim_extract("short") == "short"


def test_qid_by_statement_finds_the_one_item_with_that_identifier():
    one = {"query": {"search": [{"title": "Q1079"}]}}
    two = {"query": {"search": [{"title": "Q1"}, {"title": "Q2"}]}}
    assert wp.qid_by_statement("P345", "tt0903747", fetcher([("haswbstatement", one)])) == "Q1079"
    assert wp.qid_by_statement("P345", "tt0903747", fetcher([("haswbstatement", two)])) == ""  # not unique: no evidence
    assert wp.qid_by_statement("P345", "tt0903747", fetcher([("haswbstatement", {"query": {"search": []}})])) == ""
    assert wp.qid_by_statement("not-a-property", "x", fetcher([])) == "" and wp.qid_by_statement("P345", " ", fetcher([])) == ""

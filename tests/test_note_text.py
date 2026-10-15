import pytest

from redactor_common.core.note_text import (clean_discogs_markup, credit_footer, discogs_credit, html_to_markdown,
                                            safe_folder_label, wiki_link, wikipedia_credit)


def test_discogs_names_and_disambiguation():
    assert clean_discogs_markup("Formed as [a=Tiger Lily (2)] by [a=John Foxx].") == "Formed as Tiger Lily by John Foxx."
    assert clean_discogs_markup("Signed to [l=ZE Records].") == "Signed to ZE Records."
    assert clean_discogs_markup("[a=Ten (10)] and [a=Name (Real)]") == "Ten and Name (Real)"


def test_discogs_id_only_references_are_removed_and_tidied():
    assert clean_discogs_markup("with [a112841], [a18547] and [m=123] [r=456]") == "with, and"
    assert clean_discogs_markup("duo of [a=A] and [a112841].") == "duo of A and."


def test_discogs_urls():
    assert clean_discogs_markup("See [url=https://example.org/x]the site[/url].") == "See [the site](https://example.org/x)."
    assert clean_discogs_markup("[url=javascript:alert(1)]click[/url]") == "click"
    assert clean_discogs_markup("[url=https://e.org/a b)]t[/url]") == "[t](https://e.org/a%20b%29)"
    assert clean_discogs_markup("[url]https://e.org/[/url]") == "[https://e.org/](https://e.org/)"
    assert clean_discogs_markup("broken [url=https://e.org/ text") == "broken [url=https://e.org/ text"


def test_discogs_emphasis_and_blank_lines():
    assert clean_discogs_markup("[b]Bold[/b] and [i]italic[/i] [u]u[/u]") == "**Bold** and *italic* u"
    text = "one\r\n\r\n\r\n\r\ntwo  \r\n  three   spaces"
    assert clean_discogs_markup(text) == "one\n\ntwo\nthree spaces"
    assert clean_discogs_markup("") == ""
    assert clean_discogs_markup("[sic] stays") == "[sic] stays"


def test_html_to_markdown():
    html = '<p>One <b>bold</b> and <em>it</em>.</p><p>Two<br/>lines &amp; <a href="https://e.org/">link</a></p><ul><li>a</li><li>b</li></ul>'
    assert html_to_markdown(html) == "One **bold** and *it*.\n\nTwo\nlines & [link](https://e.org/)\n\n- a\n- b"
    assert html_to_markdown("&lt;p&gt;Escaped &lt;i&gt;html&lt;/i&gt;&lt;/p&gt;") == "Escaped *html*"
    assert html_to_markdown("plain &amp; simple") == "plain & simple"
    assert "alert" not in html_to_markdown("<script>alert(1)</script>text")
    assert html_to_markdown('<a href="javascript:x">y</a>') == "y"


def test_credits():
    assert wikipedia_credit("Tiger Lily (band)", "nb") == (
        "Introduction from Wikipedia ([Tiger Lily (band)](https://nb.wikipedia.org/wiki/Tiger_Lily_%28band%29), CC BY-SA 4.0)")
    assert "https://en.wikipedia.org/wiki/A%C3%A5" in wikipedia_credit("Aå")
    assert discogs_credit("release", 12).startswith("Profile from [Discogs](https://www.discogs.com/release/12)")
    assert credit_footer([]) == "---\n*Draft.*"
    assert credit_footer([], draft=False) == ""
    assert credit_footer(["A.", "B"]) == "---\n*Draft. A. B.*"


@pytest.mark.parametrize("name,label,expected", [
    ("Vienna", "", "[[Vienna]]"),
    ("A | B [C]", "", "[[A B C]]"),
    ("Name", "shown", "[[Name|shown]]"),
    ("Name", "Name", "[[Name]]"),
    ("Name", "a|b", "[[Name|a b]]"),
    ("  ||  ", "", ""),
    ("Two\nlines", "", "[[Two lines]]"),
])
def test_wiki_link(name, label, expected):
    assert wiki_link(name, label) == expected


@pytest.mark.parametrize("text,expected", [
    ("AC/DC: Live?", "AC DC Live"),
    ("Name...", "Name"),
    ("CON", "_CON"),
    ("nul.txt", "_nul.txt"),
    ("  a   b  ", "a b"),
    ("???", ""),
    ("Æsir & Ørn", "Æsir & Ørn"),
])
def test_safe_folder_label(text, expected):
    assert safe_folder_label(text) == expected

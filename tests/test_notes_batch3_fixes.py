"""2026-10-16 review batch 3: the notes cache stores only real answers (and heals old poison), title_from_url checks
the host properly, the review preview shows raw HTML as text."""

import os
import sqlite3
import urllib.error

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from redactor_common.core import wikipedia as wp  # noqa: E402
from redactor_common.core.notes_cache import CachedFetch, ResponseCache, storable  # noqa: E402

HTML = b"<html><body>Please log in to the wifi</body></html>"
ERR = b'{"error": {"code": "maxlag", "info": "Waiting for a database server"}}'
ERRS = b'{"errors": [{"code": "ratelimited"}], "docref": "x"}'
GOOD = b'{"entities": {"Q1": {"id": "Q1"}}}'


def test_storable_classification():
    assert storable(GOOD)
    assert storable(b'{"pages": []}') and storable(b"{}") and storable(b"[]")
    assert not storable(b"") and not storable(b"   \n")
    assert not storable(HTML) and not storable(ERR) and not storable(ERRS)
    assert not storable(b"not json at all")  # a JSON endpoint: a truncated or odd body is no answer
    assert storable(b"plain text answer", expect_json=False) and not storable(HTML, expect_json=False)


@pytest.mark.parametrize("body", [HTML, ERR, ERRS, b""], ids=["html", "error", "errors", "empty"])
def test_bad_200_bodies_are_not_stored_or_remembered(tmp_path, body):
    cache = ResponseCache(tmp_path / "c.db")
    calls = []

    def inner(url):
        calls.append(url)
        return body if len(calls) == 1 else GOOD

    fetch = CachedFetch(inner, "wiki", cache)
    assert fetch("https://www.wikidata.org/w/api.php?x=1") == body
    assert cache.count() == 0
    assert fetch("https://www.wikidata.org/w/api.php?x=1") == GOOD  # asked again, not served the error
    assert len(calls) == 2 and cache.count() == 1


def test_valid_json_is_stored_and_served_from_cache(tmp_path):
    cache = ResponseCache(tmp_path / "c.db")
    calls = []
    fetch = CachedFetch(lambda u: calls.append(u) or GOOD, "wiki", cache)
    fetch("https://x/y"), CachedFetch(lambda u: calls.append(u) or GOOD, "wiki", cache)("https://x/y")
    assert len(calls) == 1


def test_empty_json_answer_is_stored_for_the_short_time_only(tmp_path):
    now = [1000.0]
    cache = ResponseCache(tmp_path / "c.db", clock=lambda: now[0])
    cache.put("k", b"{}")
    assert cache.get("k") is not None
    now[0] += 2 * 24 * 3600  # past the one-day negative TTL
    assert cache.get("k") is None


@pytest.mark.parametrize("body", [HTML, ERR], ids=["html", "error"])
def test_poisoned_entry_already_in_the_db_is_healed_on_read(tmp_path, body):
    path = tmp_path / "c.db"
    cache = ResponseCache(path)
    cache.put("wiki|https://x/y", GOOD)  # creates the table
    db = sqlite3.connect(str(path))
    db.execute("UPDATE cache SET body=? WHERE key=?", (body, "wiki|https://x/y"))  # what the old version stored
    db.commit()
    db.close()
    cache.close()
    cache = ResponseCache(path)
    calls = []
    fetch = CachedFetch(lambda u: calls.append(u) or GOOD, "wiki", cache)
    assert fetch("https://x/y") == GOOD  # refetched, not the poison
    assert len(calls) == 1 and cache.get("wiki|https://x/y") == (200, GOOD)


def test_404_is_still_remembered(tmp_path):
    cache = ResponseCache(tmp_path / "c.db")

    def inner(url):
        raise urllib.error.HTTPError(url, 404, "nf", {}, None)

    fetch = CachedFetch(inner, "wiki", cache)
    with pytest.raises(urllib.error.HTTPError):
        fetch("https://x/none")
    assert cache.get("wiki|https://x/none")[0] == 404


def test_title_from_url_host_must_be_wikipedia():
    assert wp.title_from_url("https://en.wikipedia.org/wiki/Queen_(band)") == "Queen (band)"
    assert wp.title_from_url("https://wikipedia.org/wiki/Queen") == "Queen"
    assert wp.title_from_url("https://evilwikipedia.org/wiki/Queen") == ""
    assert wp.title_from_url("https://notwikipedia.org/wiki/Queen") == ""
    assert wp.title_from_url("https://en.wikipedia.org.evil.com/wiki/Queen") == ""


def test_preview_shows_raw_html_as_text(tmp_path):
    from PyQt6.QtWidgets import QApplication
    from redactor_common.core.notes_model import Confidence, Proposal
    from redactor_common.core.notes_review import PENDING, ReviewItem
    from redactor_common.gui.notes_review_dialog import ProposalPanel
    from tests.test_notes_shared import FakeTarget

    app = QApplication.instance() or QApplication([])  # noqa: F841 (kept alive for the test)
    folder = tmp_path / "A"
    folder.mkdir()
    text = "---\ntitle: A\n---\n\n# A\n\nHello <b>bold</b> <img src=x onerror=y>\n"
    p = Proposal(FakeTarget(folder, "series", "series"), Confidence.CONFIDENT, text, title="A", reasons=["r"])
    panel = ProposalPanel(lambda: True)
    panel.show_item(ReviewItem(p, PENDING))
    plain = panel.preview.toPlainText()
    assert "<b>bold</b>" in plain and "<img" in plain

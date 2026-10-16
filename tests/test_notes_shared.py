"""The shared Create Notes machinery (notes_cache, notes_phases, notes_timing, notes_model, notes_review), tested
without any app: hit counters on injected fetchers, fake proposals, a fake write function."""

import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

from redactor_common.core.notes import NoteWriteResult
from redactor_common.core.notes_cache import (
    NEGATIVE_TTL_SECONDS, TTL_SECONDS, CachedFetch, RequestStats, ResponseCache, clear_default_cache, default_cache,
    looks_empty,
)
from redactor_common.core.notes_model import (
    STATUS_SKIPPED, Confidence, Proposal, summarize, text_sha, undo_note_entry, write_proposal,
)
from redactor_common.core.notes_phases import PhaseSet, PhaseTimer, mmss
from redactor_common.core.notes_review import (
    ERROR, EXISTS, PENDING, SKIPPED, VIEW_LIST, VIEW_ONE, WRITTEN, ReviewSession, choose_view, note_diff,
)
from redactor_common.core.notes_timing import clock, format_duration, lookup_sentence, progress_suffix


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def cache(tmp_path):
    clock_ = Clock()
    c = ResponseCache(tmp_path / "cache.sqlite", clock=clock_)
    c.clock = clock_
    yield c
    c.close()


# -- the disk cache -------------------------------------------------------------------------------------------------

def test_put_get_and_ttl(cache):
    cache.put("k", b'{"a": 1}')
    assert cache.get("k") == (200, b'{"a": 1}')
    cache.clock.now += TTL_SECONDS - 10
    assert cache.get("k") is not None
    cache.clock.now += 20
    assert cache.get("k") is None and cache.count() == 0


def test_not_found_and_empty_results_live_one_day_errors_never(cache):
    cache.put("gone", b"", 404)
    cache.put("empty", b'{"results": []}')
    cache.put("busy", b"busy", 503)
    assert cache.count() == 2
    assert looks_empty(b'{"pages": []}') and not looks_empty(b'{"pages": [1]}') and not looks_empty(b"not json")
    cache.clock.now += NEGATIVE_TTL_SECONDS + 1
    assert cache.get("gone") is None and cache.get("empty") is None


def test_a_broken_cache_file_is_dropped_and_started_again(tmp_path):
    path = tmp_path / "broken.sqlite"
    path.write_bytes(b"this is not a database at all" * 50)
    c = ResponseCache(path)
    assert c.get("k") is None
    c.put("k", b"{}")
    assert c.get("k") == (200, b"{}")
    c.close()


def test_the_cache_is_thread_safe(cache):
    errors = []

    def worker(n):
        try:
            for i in range(40):
                cache.put(f"t{n}-{i}", f"[{n}, {i}]".encode())
                assert cache.get(f"t{n}-{i}") == (200, f"[{n}, {i}]".encode())
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == [] and cache.count() == 6 * 40


def test_the_default_cache_lives_where_the_app_says_and_clears(tmp_path):
    path = tmp_path / "app_cache.sqlite"
    assert default_cache(path, enabled=False) is None
    assert clear_default_cache(path) == 0  # no file yet
    default_cache(path).put("a", b"1")
    assert clear_default_cache(path) == 1


def test_cached_fetch_asks_the_network_once_per_url_and_the_disk_serves_the_next_run(cache):
    urls = []

    def inner(request):
        url = CachedFetch.url_of(request)
        urls.append(url)
        if url.endswith("/gone"):
            raise urllib.error.HTTPError(url, 404, "nf", {}, None)
        return b'{"x": 1}'

    stats = RequestStats()
    fetch = CachedFetch(inner, "tmdb", cache, stats)
    assert fetch("https://example.org/a") == fetch("https://example.org/a")
    for _ in range(2):
        with pytest.raises(urllib.error.HTTPError) as err:
            fetch("https://example.org/gone")
        assert err.value.code == 404
    assert len(urls) == 2 and stats.requests == 2 and stats.from_memory == 2
    urls.clear()
    stats2 = RequestStats()
    fetch2 = CachedFetch(inner, "tmdb", cache, stats2)
    assert json.loads(fetch2("https://example.org/a")) == {"x": 1}
    assert urls == [] and stats2.from_cache == 1


def test_the_key_is_the_url_never_a_header(cache):
    request = urllib.request.Request("https://example.org/q", headers={"Authorization": "Bearer SECRET-TOKEN"})
    CachedFetch(lambda r: b'{"a": 1}', "wiki", cache)(request)
    cache.close()
    raw = open(cache.path, "rb").read()
    assert b"SECRET-TOKEN" not in raw and b"Authorization" not in raw


def test_a_secret_query_parameter_is_never_stored_and_a_new_key_still_hits_the_cache(cache):
    calls = []

    def inner(request):
        calls.append(CachedFetch.url_of(request))
        return b'{"ok": 1}'

    CachedFetch(inner, "tmdb", cache, secret_params=("api_key",))("https://api.example.org/3/tv/1?api_key=SECRET-KEY&x=2")
    cache.close()
    assert b"SECRET-KEY" not in open(cache.path, "rb").read()
    again = CachedFetch(inner, "tmdb", cache, secret_params=("api_key",))
    assert again("https://api.example.org/3/tv/1?x=2&api_key=ANOTHER-KEY") == b'{"ok": 1}'
    assert len(calls) == 1  # the second key was answered from the disk


# -- phases and timing ----------------------------------------------------------------------------------------------

PHASES = PhaseSet({"scan": "scan", "tmdb": "TMDB", "wikipedia": "Wikipedia"},
                  {"scan": "folders", "tmdb": "requests", "wikipedia": "requests"}, busy_phase="tmdb")


def test_the_phase_timer_names_what_was_slow():
    timer = PhaseTimer(PHASES)
    timer.add("scan", 12, 40)
    timer.add("tmdb", 70.4, 30)
    timer.add("busy", 20)
    assert timer.time_line() == "Time: scan 0:12, TMDB 1:10 (of it 0:20 waiting for a busy service, 1x)"
    assert "scan 0:12 (40 folders)" in timer.detail() and "Wikipedia" not in timer.detail()
    assert timer.to_dict()["busy"]["count"] == 1
    assert mmss(3661) == "1:01:01"


def test_durations_and_the_progress_suffix():
    assert [format_duration(s) for s in (35, 240, 6060)] == ["35 s", "4 min", "1 h 41 min"]
    assert clock(6612) == "1:50:12"
    assert lookup_sentence(656, 6612, 5120, 800) == "Looked up 656 folders in 1:50:12 (5120 requests, 800 from the cache)."
    assert progress_suffix(2, 10, 5, [1, 1]) == ""  # too early for a guess
    assert progress_suffix(5, 10, 300, [60] * 5) == ", 5 min elapsed, about 5 min left"
    assert progress_suffix(9, 10, 300, [1] * 9).endswith("almost done")


# -- proposals, writing, review -------------------------------------------------------------------------------------

@dataclass
class FakeTarget:
    folder: Path
    kind: str = "series"
    note_kind: str = "series"


def proposal(tmp_path, name, confidence=Confidence.CONFIDENT, kind="series", text="---\ntitle: x\n---\n\nBody\n"):
    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    return Proposal(FakeTarget(folder, kind, "series" if kind == "series" else "about"), confidence,
                    text if confidence == Confidence.CONFIDENT else "", title=name)


def test_write_proposal_writes_a_confident_draft_and_never_overwrites(tmp_path):
    seen = []
    p = proposal(tmp_path, "Show")
    result = write_proposal(p, record=lambda path, text, replaced: seen.append((path, replaced)))
    assert result.ok and (tmp_path / "Show" / "series.md").read_text(encoding="utf-8").endswith("Body\n")
    assert seen == [(str(tmp_path / "Show" / "series.md"), False)]
    again = write_proposal(p)
    assert again.status == "exists"


def test_an_uncertain_proposal_is_refused_unless_a_person_chose_it(tmp_path):
    p = proposal(tmp_path, "Maybe", Confidence.UNCERTAIN)
    assert write_proposal(p).status == STATUS_SKIPPED
    p.note_text = "---\ntitle: Maybe\n---\n\nText\n"
    assert write_proposal(p, allow_uncertain=True).ok


def test_summarize_counts_by_confidence_and_result(tmp_path):
    a, b = proposal(tmp_path, "A"), proposal(tmp_path, "B", Confidence.NONE)
    b.problems = ["Could not reach TMDB"]
    summary = summarize([a, b], {a.path: write_proposal(a, dry_run=True)})
    assert (summary.total, summary.confident, summary.none, summary.would_write) == (2, 1, 1, 1)
    assert summary.problems == ["Could not reach TMDB"]


def test_undo_moves_a_note_to_the_bin_only_while_it_is_as_written(tmp_path):
    p = proposal(tmp_path, "Undo")
    path = write_proposal(p).path

    class Entry:
        id, kind, note = 1, "note", f"sha256:{text_sha(p.note_text)}"
    Entry.path = path
    binned, journal = [], type("J", (), {"mark_restored": lambda self, i, x: binned.append(("restored", i))})()
    Path(path).write_text("edited by a person\n", encoding="utf-8")
    assert "changed" in undo_note_entry(Entry, journal, trash=binned.append)
    Path(path).write_bytes(p.note_text.encode("utf-8"))  # bytes: text mode would write CRLF on Windows
    assert undo_note_entry(Entry, journal, trash=lambda x: binned.append(("trash", x))) == ""
    assert ("trash", path) in binned and ("restored", 1) in binned


def test_choose_view_one_owner_with_children_is_one_page_several_owners_a_list(tmp_path):
    show = FakeTarget(tmp_path / "Show", "series")
    season = FakeTarget(tmp_path / "Show" / "Season 1", "season")
    other = FakeTarget(tmp_path / "Other", "series")
    kw = dict(owner_kinds=("series",), child_kind="season")
    assert choose_view([show, season], **kw) == VIEW_ONE
    assert choose_view([show, season, other], **kw) == VIEW_LIST
    assert choose_view([]) == VIEW_ONE


class FakeWriter:
    def __init__(self, status="written"):
        self.calls, self.status = [], status

    def __call__(self, p):
        self.calls.append(p.path)
        return NoteWriteResult(self.status, p.path, "" if self.status == "written" else "an existing note")


def session_of(tmp_path, writer):
    return ReviewSession([proposal(tmp_path, "A"), proposal(tmp_path, "B"), proposal(tmp_path, "C", Confidence.UNCERTAIN)],
                         write_fn=writer)


def test_confident_are_preticked_and_uncertain_cannot_be_ticked(tmp_path):
    s = session_of(tmp_path, FakeWriter())
    assert [i.ticked for i in s.items] == [True, True, False]
    assert s.set_ticked(2, True) is False and s.set_ticked(0, False) and not s.items[0].ticked


def test_write_ticked_writes_only_ticked_confident_ones_through_the_write_function(tmp_path):
    w = FakeWriter()
    s = session_of(tmp_path, w)
    s.set_ticked(0, False)
    s.write_ticked()
    assert len(w.calls) == 1 and s.items[0].state == PENDING and s.items[1].state == WRITTEN
    assert s.write(2).status == STATUS_SKIPPED and len(w.calls) == 1
    assert s.totals().written == 1 and s.totals().uncertain_left == 1


def test_skip_exists_and_error_states(tmp_path):
    s = session_of(tmp_path, FakeWriter(status="exists"))
    s.skip(0)
    s.write_ticked()
    totals = s.totals()
    assert s.items[0].state == SKIPPED and s.items[1].state == EXISTS and totals.blocked == 1
    assert any("an existing note" in m for m in totals.messages)
    e = session_of(tmp_path, FakeWriter(status="error"))
    e.write(0)
    assert e.items[0].state == ERROR


def test_next_pending_wraps_and_ends(tmp_path):
    s = session_of(tmp_path, FakeWriter())
    assert s.next_pending(-1) == 0
    for i in range(3):
        s.skip(i)
    assert s.next_pending(0) is None


def test_replace_makes_a_resolved_proposal_tickable(tmp_path):
    s = session_of(tmp_path, FakeWriter())
    item = s.replace(2, proposal(tmp_path, "C"))
    assert item.tickable and item.ticked and item.user_resolved


def test_note_diff_shows_before_and_after():
    assert note_diff("a\n", "a\n") == ""
    assert "+b" in note_diff("a\n", "a\nb\n")


def test_body_helpers_skip_empty_parts_and_values():
    from redactor_common.core.note_text import fact_lines, join_blocks

    assert join_blocks("# T", "", "  ", "text\n") == "# T\n\ntext"
    assert fact_lines([("Network", "AMC"), ("Status", ""), ("Seasons", "5")]) == "**Network:** AMC  \n**Seasons:** 5"


def test_build_each_builds_in_order_reports_progress_and_stops_on_cancel(tmp_path):
    from redactor_common.core.notes_model import build_each

    targets = [FakeTarget(tmp_path / n) for n in "abc"]
    seen, steps = [], []
    got = build_each(targets, lambda t: Proposal(t, Confidence.CONFIDENT), progress=lambda d, n, label: steps.append((d, n, label)),
                     on_proposal=seen.append)
    assert [p.target for p in got] == targets and len(seen) == 3
    assert steps[0] == (0, 3, "series: a") and steps[-1] == (3, 3, "")
    stop = []
    got = build_each(targets, lambda t: stop.append(1) or Proposal(t), cancel=lambda: len(stop) >= 2)
    assert len(got) == 2

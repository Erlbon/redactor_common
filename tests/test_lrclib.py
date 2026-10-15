"""core/lrclib.py: LRC parsing, LRC -> SRT, match scoring and the client with an injected fetch.
Fixtures in tests/fixtures are trimmed copies of real api responses from lrclib.net (2026-10-15)."""

import json
import re
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from redactor_common.core.lrclib import (
    LrcLine,
    LrclibClient,
    LrclibError,
    Lyrics,
    lrc_to_srt,
    normalize_name,
    parse_lrc,
    rank_matches,
    score_match,
    split_artist_title,
    srt_preview,
    wrap_line,
)

FIX = Path(__file__).parent / "fixtures"


def _fixture(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


# -- parse_lrc ---------------------------------------------------------------------

def test_parse_timestamp_forms():
    lines = parse_lrc("[00:01.50] a\n[00:02.250] b\n[00:03] c\n[01:02.5] d\n[1:05:10] e")
    assert [(l.start, l.text) for l in lines] == [(1.5, "a"), (2.25, "b"), (3.0, "c"), (62.5, "d"), (65.1, "e")]


def test_parse_several_timestamps_on_one_line_and_sorting():
    lines = parse_lrc("[00:30.00][01:00.00] chorus\n[00:10.00] verse")
    assert [(l.start, l.text) for l in lines] == [(10.0, "verse"), (30.0, "chorus"), (60.0, "chorus")]


def test_parse_ignores_metadata_and_untimed_lines():
    text = "[ar:Artist]\n[ti:Title]\n[al:Album]\n[length:03:45]\n[by:someone]\nuntimed words\n[00:05.00] real"
    assert parse_lrc(text) == [LrcLine(5.0, "real")]


def test_parse_blank_lyric_is_a_gap_marker():
    assert parse_lrc("[00:10.00] a\n[00:15.00]\n[00:20.00] b") == [
        LrcLine(10.0, "a"), LrcLine(15.0, ""), LrcLine(20.0, "b")]


def test_parse_offset_tag_positive_means_earlier():
    lines = parse_lrc("[offset:+500]\n[00:10.00] a")
    assert lines[0].start == pytest.approx(9.5)
    lines = parse_lrc("[offset:-250]\n[00:10.00] a")
    assert lines[0].start == pytest.approx(10.25)


def test_parse_enhanced_word_times_and_crlf():
    assert parse_lrc("[00:01.00] <00:01.00>Hel<00:01.40>lo\r\n[00:02.00] x") == [
        LrcLine(1.0, "Hello"), LrcLine(2.0, "x")]


def test_parse_empty_and_none():
    assert parse_lrc("") == [] and parse_lrc(None) == []


# -- lrc_to_srt --------------------------------------------------------------------

def _cues(srt):
    out = []
    for block in srt.strip().split("\n\n"):
        n, timing, *body = block.split("\n")
        out.append((int(n), timing, "\n".join(body)))
    return out


def test_srt_basic_timing_gap_and_numbering():
    srt = lrc_to_srt("[00:10.00] one\n[00:13.00] two\n[00:15.00] three", media_duration=100)
    cues = _cues(srt)
    assert [c[0] for c in cues] == [1, 2, 3]
    assert cues[0][1] == "00:00:10,000 --> 00:00:12,950"
    assert cues[1][1] == "00:00:13,000 --> 00:00:14,950"
    assert cues[2][1] == "00:00:15,000 --> 00:00:22,000"  # last line: start + 7 s
    assert "\r" not in srt and srt.endswith("\n")


def test_srt_empty_lrc_line_ends_previous_early():
    srt = lrc_to_srt("[00:10.00] sung\n[00:12.00]\n[00:30.00] later")
    assert _cues(srt)[0][1] == "00:00:10,000 --> 00:00:11,950"
    assert len(_cues(srt)) == 2


def test_srt_line_never_longer_than_max_line_seconds():
    srt = lrc_to_srt("[00:10.00] a\n[01:00.00] b")
    assert _cues(srt)[0][1] == "00:00:10,000 --> 00:00:17,000"


def test_srt_last_line_limited_by_media_duration_and_late_cues_dropped():
    srt = lrc_to_srt("[00:10.00] a\n[00:20.00] b\n[00:50.00] beyond", media_duration=22)
    cues = _cues(srt)
    assert len(cues) == 2
    assert cues[1][1] == "00:00:20,000 --> 00:00:22,000"


def test_srt_min_line_seconds_when_room():
    srt = lrc_to_srt("[00:10.00] a\n[00:10.20] b\n[00:20.00] c")
    # "a" cannot reach 1 s without running into "b": it ends just before b
    assert _cues(srt)[0][1] == "00:00:10,000 --> 00:00:10,150"
    srt = lrc_to_srt("[00:10.00] a\n[00:11.50] b")
    assert _cues(srt)[0][1] == "00:00:10,000 --> 00:00:11,450"


def test_srt_offset_shifts_and_clamps_at_zero():
    srt = lrc_to_srt("[00:01.00] a\n[00:10.00] b", offset_seconds=-2.0)
    cues = _cues(srt)
    assert cues[0][1].startswith("00:00:00,000 -->")  # 1.0 - 2.0 clamped
    assert cues[1][1].startswith("00:00:08,000 -->")
    srt = lrc_to_srt("[00:10.00] a", offset_seconds=2.5)
    assert _cues(srt)[0][1].startswith("00:00:12,500 -->")


def test_srt_cue_entirely_before_zero_is_dropped():
    srt = lrc_to_srt("[00:01.00] gone\n[00:03.00] kept\n[00:20.00] z", offset_seconds=-10)
    assert [c[2] for c in _cues(srt)] == ["z"]  # "kept" also ends before 0 (3.0 - 10)


def test_srt_wraps_long_lines_in_two():
    long = "All I ever wanted all I ever needed is here in my arms tonight"
    srt = lrc_to_srt(f"[00:01.00] {long}")
    body = _cues(srt)[0][2].split("\n")
    assert len(body) == 2 and " ".join(body) == long
    assert abs(len(body[0]) - len(body[1])) < 12


def test_wrap_line_short_and_unbreakable():
    assert wrap_line("short line") == "short line"
    assert wrap_line("x" * 60) == "x" * 60


def test_srt_unicode_kept():
    srt = lrc_to_srt("[00:01.00] Привет, мир\n[00:05.00] 你好")
    assert "Привет, мир" in srt and "你好" in srt


def test_srt_empty_when_nothing_timed():
    assert lrc_to_srt("just words") == "" and lrc_to_srt("[00:01.00]") == ""


def test_srt_accepts_parsed_lines_and_roundtrips_shape():
    srt = lrc_to_srt([LrcLine(1.0, "a"), LrcLine(4.0, "b")])
    assert re.fullmatch(r"(\d+\n\d\d:\d\d:\d\d,\d{3} --> \d\d:\d\d:\d\d,\d{3}\n[^\n]+(\n[^\n]+)?\n\n?)+", srt)


def test_srt_hours_format():
    srt = lrc_to_srt("[61:01.00] late")
    assert _cues(srt)[0][1].startswith("01:01:01,000 -->")


def test_srt_preview_first_cues():
    srt = lrc_to_srt("\n".join(f"[00:{i * 3:02d}.00] line {i}" for i in range(1, 15)))
    preview = srt_preview(srt, 8)
    assert len(preview.split("\n\n")) == 8 and "line 9" not in preview


# -- real fixture ---------------------------------------------------------------------

def test_real_fixture_converts():
    entry = _fixture("lrclib_get_enjoy_the_silence.json")
    srt = lrc_to_srt(entry["syncedLyrics"], media_duration=399)
    cues = _cues(srt)
    assert cues[0] == (1, "00:00:00,730 --> 00:00:07,730", "Words like violence")
    assert cues[1][2] == "Break the silence"
    assert cues[1][1].startswith("00:00:27,890")


# -- normalisation and scoring -----------------------------------------------------------

def test_normalize_keeps_unicode_and_drops_decoration():
    assert normalize_name("Enjoy The Silence (2006 Remaster)") == "enjoy the silence"
    assert normalize_name("Ленинград feat. Someone") == "ленинград"
    assert normalize_name("  Mötley  Crüe! ") == "mötley crüe"
    assert normalize_name("Й") == "й"  # no ASCII folding, no mark stripping


def _cand(title="Enjoy the Silence", artist="Depeche Mode", duration=399.0, synced="[00:01.00] x", **kw):
    return Lyrics(id=kw.get("id"), track_name=title, artist_name=artist, duration=duration, synced=synced)


def test_score_exact_close_different():
    exact = score_match("Depeche Mode", "Enjoy The Silence", 400.5, _cand())
    assert exact.kind == "exact" and exact.label == "exact" and exact.score > 0.95
    close = score_match("Depeche Mode", "Enjoy the Silence", 403.0, _cand())
    assert close.kind == "close" and "4 s longer" in close.label
    diff = score_match("Depeche Mode", "Enjoy the Silence", 380.0, _cand())
    assert diff.kind == "different" and "19 s shorter" in diff.label and diff.label.startswith("different length")
    assert exact.score > close.score > diff.score


def test_score_other_title_never_exact():
    s = score_match("Depeche Mode", "Personal Jesus", 399.0, _cand())
    assert s.kind == "different" and "title" in s.label


def test_score_unknown_length_never_exact():
    s = score_match("Depeche Mode", "Enjoy the Silence", None, _cand())
    assert s.kind == "different" and "unknown" in s.label


def test_score_cyrillic():
    c = Lyrics(track_name="WWW", artist_name="Ленинград", duration=167.0, synced="[00:01.00] x")
    assert score_match("ленинград", "www", 167.4, c).kind == "exact"


def test_score_artist_subset_and_missing_local_artist():
    c = _cand(artist="Depeche Mode & Friends")
    assert score_match("Depeche Mode", "Enjoy the Silence", 399, c).kind == "exact"
    assert score_match("", "Enjoy the Silence", 399, _cand()).kind == "exact"


def test_rank_prefers_timed_then_kind_and_drops_duplicate_ids():
    plain = Lyrics(id=1, track_name="Enjoy the Silence", artist_name="Depeche Mode", duration=399.0, plain="p")
    far = Lyrics(id=2, track_name="Enjoy the Silence", artist_name="Depeche Mode", duration=300.0, synced="[00:01.00] x")
    near = Lyrics(id=3, track_name="Enjoy the Silence", artist_name="Depeche Mode", duration=399.0, synced="[00:01.00] x")
    ranked = rank_matches("Depeche Mode", "Enjoy the Silence", 399.0, [plain, far, near, near])
    assert [m.lyrics.id for m in ranked] == [3, 2, 1]
    assert not ranked[2].lyrics.has_timing


def test_has_timing():
    assert Lyrics(synced="[00:01.00] a").has_timing
    assert not Lyrics(synced="").has_timing
    assert not Lyrics(synced="[00:01.00]").has_timing
    assert not Lyrics(synced="[00:01.00] a", instrumental=True).has_timing


def test_split_artist_title():
    assert split_artist_title("Depeche Mode - Enjoy the Silence") == ("Depeche Mode", "Enjoy the Silence")
    assert split_artist_title("01 - Ленинград \u2013 WWW") == ("Ленинград", "WWW")
    assert split_artist_title("Just a title") == ("", "Just a title")
    assert split_artist_title("A - B - C") == ("A", "B - C")


# -- the client with an injected fetch ----------------------------------------------------

class _Fetch:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, bytes) else json.dumps(response).encode("utf-8")


def _http(code):
    return urllib.error.HTTPError("https://lrclib.net/api/get?artist_name=SECRET", code, "x", {}, None)


def _query(request):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(request.full_url).query))


def test_get_by_signature_builds_the_request_and_parses():
    fetch = _Fetch(_fixture("lrclib_get_enjoy_the_silence.json"))
    client = LrclibClient("UA/1", fetch=fetch)
    hit = client.get_by_signature("Depeche Mode", "Enjoy the Silence", "Violator", 399.2)
    req = fetch.requests[0]
    assert urllib.parse.urlsplit(req.full_url).path == "/api/get"
    assert _query(req) == {"artist_name": "Depeche Mode", "track_name": "Enjoy the Silence",
                           "album_name": "Violator", "duration": "399"}
    assert req.get_header("User-agent") == "UA/1"
    assert hit.id == 36918879 and hit.album_name == "Violator" and hit.duration == 399.0 and hit.has_timing


def test_get_by_signature_404_is_none_and_omits_empty_params():
    fetch = _Fetch(_http(404))
    assert LrclibClient(fetch=fetch).get_by_signature("A", "B") is None
    assert _query(fetch.requests[0]) == {"artist_name": "A", "track_name": "B"}


def test_search_parses_list_and_plain_only_entry():
    fetch = _Fetch(_fixture("lrclib_search_enjoy_the_silence.json"))
    results = LrclibClient(fetch=fetch).search("Depeche Mode", "Enjoy the Silence")
    assert _query(fetch.requests[0]) == {"track_name": "Enjoy the Silence", "artist_name": "Depeche Mode"}
    assert len(results) == 4
    assert results[0].has_timing and not results[3].has_timing and results[3].plain


def test_search_by_free_text_and_nothing():
    fetch = _Fetch([])
    client = LrclibClient(fetch=fetch)
    assert client.search(query="enjoy the silence") == []
    assert _query(fetch.requests[0]) == {"q": "enjoy the silence"}
    assert client.search() == [] and len(fetch.requests) == 1


def test_search_unusable_reply_is_empty():
    assert LrclibClient(fetch=_Fetch({"message": "odd"})).search("a", "b") == []


def test_429_is_retried_then_succeeds_and_never_really_sleeps():
    sleeps = []
    fetch = _Fetch(_http(429), _http(429), [])
    LrclibClient(fetch=fetch, retry_429=(1.0, 2.0), sleep=sleeps.append).search("a", "b")
    assert sleeps == [1.0, 2.0] and len(fetch.requests) == 3


def test_429_exhausted_message_has_no_url_or_query():
    client = LrclibClient(fetch=_Fetch(_http(429), _http(429)), retry_429=(0.0,), sleep=lambda s: None)
    with pytest.raises(LrclibError) as err:
        client.search("SECRET", "title")
    assert "rate-limiting" in str(err.value) and "SECRET" not in str(err.value)


def test_unreachable_message():
    client = LrclibClient(fetch=_Fetch(urllib.error.URLError("no route")))
    with pytest.raises(LrclibError, match="Could not reach LRCLIB: .*no route.*internet"):
        client.search("a", "b")


def test_http_500_and_bad_json_are_lrclib_errors():
    with pytest.raises(LrclibError, match="HTTP 500"):
        LrclibClient(fetch=_Fetch(_http(500))).search("a", "b")
    with pytest.raises(LrclibError, match="unreadable"):
        LrclibClient(fetch=_Fetch(b"<html>")).search("a", "b")


def test_default_fetch_is_throttled(monkeypatch):
    calls = []
    import redactor_common.core.lrclib as mod

    class _T:
        def wait(self):
            calls.append("wait")

    monkeypatch.setattr(mod, "make_default_fetch", lambda ua, timeout=15: (lambda req: b"[]"))
    client = LrclibClient("UA", throttle=_T())
    client.search("a", "b")
    client.search("a", "c")
    assert calls == ["wait", "wait"]

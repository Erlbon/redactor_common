"""Throttle, 429 retry, status-to-exception mapping, opener/max_bytes and
message redaction in core/lookup_client.py: the behaviour the apps' own
copies had (mp3 MusicBrainz/Discogs/AcoustID, cbz manga/GCD, epub Google
Books/Open Library)."""

import threading
import urllib.error
import urllib.request

import pytest

from redactor_common.core.lookup_client import (
    LookupError,
    Throttle,
    fetch_bytes,
    fetch_json,
    make_default_fetch,
    redact_text,
    redact_url,
    throttled,
)


class _SourceError(LookupError):
    pass


class _RateLimited(_SourceError):
    pass


class _AuthError(_SourceError):
    pass


def _http_error(code, reason="Nope", url="https://api.example.com/x?api_key=SECRET"):
    return urllib.error.HTTPError(url, code, reason, {}, None)


class _FakeClock:
    def __init__(self, start=100.0):
        self.now = start
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


# -- Throttle ----------------------------------------------------------------

def test_throttle_first_call_never_waits():
    clock = _FakeClock()
    Throttle(1.1, clock=clock, sleep=clock.sleep).wait()
    assert clock.sleeps == []


def test_throttle_waits_the_rest_of_the_interval():
    clock = _FakeClock()
    throttle = Throttle(1.1, clock=clock, sleep=clock.sleep)
    throttle.wait()
    clock.now += 0.6
    throttle.wait()
    assert clock.sleeps == [pytest.approx(0.5)]


def test_throttle_no_wait_after_the_interval_passed():
    clock = _FakeClock()
    throttle = Throttle(1.0, clock=clock, sleep=clock.sleep)
    throttle.wait()
    clock.now += 5
    throttle.wait()
    assert clock.sleeps == []


def test_throttle_back_to_back_calls_are_spaced():
    clock = _FakeClock()
    throttle = Throttle(2.0, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        throttle.wait()
    assert clock.sleeps == [2.0, 2.0]


def test_throttle_reset_forgets_the_last_request():
    clock = _FakeClock()
    throttle = Throttle(2.0, clock=clock, sleep=clock.sleep)
    throttle.wait()
    throttle.reset()
    throttle.wait()
    assert clock.sleeps == []


def test_throttle_is_thread_safe():
    clock = _FakeClock()
    lock = threading.Lock()

    def sleep(seconds):
        with lock:
            clock.sleep(seconds)

    throttle = Throttle(1.0, clock=clock, sleep=sleep)
    threads = [threading.Thread(target=throttle.wait) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(clock.sleeps) == 7  # every call but the first waited a full interval


def test_throttled_fetch_waits_before_each_call():
    calls = []

    class _Recorder:
        def wait(self):
            calls.append("wait")

    def fetch(url):
        calls.append(url)
        return b"{}"

    wrapped = throttled(fetch, _Recorder())
    wrapped("a")
    wrapped("b")
    assert calls == ["wait", "a", "wait", "b"]


# -- 429 retry ---------------------------------------------------------------

def _flaky(failures, code=429, body=b'{"ok": true}'):
    state = {"calls": 0}

    def fetch(url):
        state["calls"] += 1
        if state["calls"] <= failures:
            raise _http_error(code)
        return body

    return fetch, state


def test_retry_429_succeeds_after_backoff():
    fetch, state = _flaky(2)
    slept = []
    data = fetch_json("https://x/", fetch, _SourceError, retry_429=(1.0, 2.0), sleep=slept.append)
    assert data == {"ok": True}
    assert slept == [1.0, 2.0]
    assert state["calls"] == 3


def test_retry_429_gives_up_with_the_429_message():
    fetch, state = _flaky(5)
    slept = []
    with pytest.raises(_SourceError, match="rate-limiting"):
        fetch_json(
            "https://x/", fetch, _SourceError, source_name="Google Books", retry_429=(1.0, 2.0),
            sleep=slept.append, status_messages={429: "Google Books is rate-limiting requests right now (HTTP 429)"},
        )
    assert slept == [1.0, 2.0]
    assert state["calls"] == 3


def test_no_retry_by_default():
    fetch, state = _flaky(1)
    with pytest.raises(_SourceError, match="HTTP 429"):
        fetch_json("https://x/", fetch, _SourceError, sleep=lambda s: pytest.fail("slept"))
    assert state["calls"] == 1


def test_retry_only_for_429():
    fetch, state = _flaky(1, code=503)
    with pytest.raises(_SourceError, match="HTTP 503"):
        fetch_json("https://x/", fetch, _SourceError, retry_429=(1.0,), sleep=lambda s: pytest.fail("slept"))
    assert state["calls"] == 1


def test_fetch_bytes_retry_429():
    fetch, _ = _flaky(1, body=b"\xff\xd8")
    slept = []
    assert fetch_bytes("https://x/", fetch, _SourceError, retry_429=(0.5,), sleep=slept.append) == b"\xff\xd8"
    assert slept == [0.5]


# -- status_errors -----------------------------------------------------------

def test_status_errors_raise_the_mapped_class_with_the_mapped_message():
    def fetch(url):
        raise _http_error(429)

    with pytest.raises(_RateLimited, match="too many requests") as info:
        fetch_json(
            "https://x/", fetch, _SourceError, status_errors={429: _RateLimited},
            status_messages={429: "Discogs says too many requests were made (HTTP 429)"},
        )
    assert isinstance(info.value.__cause__, urllib.error.HTTPError)


def test_status_errors_use_the_default_message_without_a_status_message():
    def fetch(url):
        raise _http_error(401, "Unauthorized")

    with pytest.raises(_AuthError, match="GCD returned an error .HTTP 401.: Unauthorized"):
        fetch_json("https://x/", fetch, _SourceError, source_name="GCD", status_errors={401: _AuthError, 403: _AuthError})


def test_status_errors_leave_other_codes_to_error_cls():
    def fetch(url):
        raise _http_error(500)

    with pytest.raises(_SourceError) as info:
        fetch_json("https://x/", fetch, _SourceError, status_errors={429: _RateLimited})
    assert type(info.value) is _SourceError


def test_status_errors_apply_after_retries():
    fetch, state = _flaky(9)
    with pytest.raises(_RateLimited):
        fetch_json("https://x/", fetch, _SourceError, status_errors={429: _RateLimited}, retry_429=(0.1,), sleep=lambda s: None)
    assert state["calls"] == 2


def test_ignore_404_wins_over_status_errors():
    def fetch(url):
        raise _http_error(404)

    assert fetch_json("https://x/", fetch, _SourceError, ignore_404=True, status_errors={404: _AuthError}) is None


def test_fetch_bytes_status_errors():
    def fetch(url):
        raise _http_error(429)

    with pytest.raises(_RateLimited, match="cover image .HTTP 429"):
        fetch_bytes("https://x/", fetch, _SourceError, what="the cover image", status_errors={429: _RateLimited})


def test_fetch_bytes_require_data_still_works():
    with pytest.raises(_SourceError, match="returned no data"):
        fetch_bytes("https://x/", lambda url: b"", _SourceError, require_data=True)


def test_status_messages_alone_still_raise_error_cls():
    def fetch(url):
        raise _http_error(401)

    with pytest.raises(_SourceError, match="rejected the API key"):
        fetch_json("https://x/", fetch, _SourceError, status_messages={401: "TMDB rejected the API key"})


# -- redaction ---------------------------------------------------------------

def test_redact_url_drops_the_query_string():
    assert redact_url("https://api.example.com/search?q=x&api_key=SECRET") == "https://api.example.com/search?***"
    assert redact_url("https://api.example.com/plain") == "https://api.example.com/plain"
    assert redact_url("") == ""


def test_redact_text_handles_urls_inside_text_and_named_secrets():
    text = "unknown url type: 'ftp://h/x?token=SECRET' and token TOK123 again"
    result = redact_text(text, secrets=["TOK123", ""])
    assert "SECRET" not in result
    assert "TOK123" not in result
    assert "ftp://h/x?***" in result


def test_network_error_message_never_carries_the_query_string():
    def fetch(url):
        raise ValueError("unknown url type: 'https://api.example.com/x?api_key=SECRET'")

    with pytest.raises(_SourceError) as info:
        fetch_json("https://x/", fetch, _SourceError)
    assert "SECRET" not in str(info.value)


def test_secrets_are_scrubbed_from_http_reasons():
    def fetch(url):
        raise _http_error(400, reason="bad token TOK123")

    with pytest.raises(_SourceError) as info:
        fetch_json("https://x/", fetch, _SourceError, secrets=["TOK123"])
    assert "TOK123" not in str(info.value)
    assert "***" in str(info.value)


def test_fetch_bytes_network_error_is_redacted():
    def fetch(url):
        raise OSError("failed for https://i.example.com/c.jpg?sig=SECRET")

    with pytest.raises(_SourceError) as info:
        fetch_bytes("https://x/", fetch, _SourceError, what="the cover image", secrets=["nothing"])
    assert "SECRET" not in str(info.value)


# -- make_default_fetch: scheme check, opener, max_bytes ---------------------

class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        return self._data if size < 0 else self._data[:size]


class _FakeOpener:
    def __init__(self, data=b"ok"):
        self.data = data
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        return _FakeResponse(self.data)


def test_non_http_scheme_refused_without_echoing_the_query():
    fetch = make_default_fetch("UA/1", opener=_FakeOpener())
    with pytest.raises(ValueError) as info:
        fetch("file:///etc/passwd?key=SECRET")
    assert "SECRET" not in str(info.value)
    assert "Only http and https" in str(info.value)


def test_custom_opener_is_used_with_user_agent_and_timeout(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("urlopen used"))
    opener = _FakeOpener(b"data")
    fetch = make_default_fetch("UA/2", timeout=7.0, opener=opener)
    assert fetch("https://api.example.com/x") == b"data"
    request, timeout = opener.requests[0]
    assert request.get_header("User-agent") == "UA/2"
    assert timeout == 7.0


def test_request_object_keeps_its_own_user_agent():
    opener = _FakeOpener()
    fetch = make_default_fetch("UA/default", opener=opener)
    fetch(urllib.request.Request("https://x/", headers={"User-Agent": "Mine/1"}))
    assert opener.requests[0][0].get_header("User-agent") == "Mine/1"


def test_max_bytes_caps_the_response():
    fetch = make_default_fetch("UA", opener=_FakeOpener(b"x" * 11), max_bytes=10)
    with pytest.raises(OSError, match="too large"):
        fetch("https://x/")
    assert make_default_fetch("UA", opener=_FakeOpener(b"x" * 10), max_bytes=10)("https://x/") == b"x" * 10


def test_too_large_response_becomes_error_cls():
    fetch = make_default_fetch("UA", opener=_FakeOpener(b"x" * 11), max_bytes=10)
    with pytest.raises(_SourceError, match="Could not reach Discogs"):
        fetch_json("https://x/", fetch, _SourceError, source_name="Discogs")


def test_unreachable_message_is_the_apps_wording_with_the_detail_and_without_secrets():
    import urllib.error

    from redactor_common.core import lookup_client as lc

    def down(request):
        raise urllib.error.URLError("getaddrinfo failed for ?key=SECRET123")

    with pytest.raises(lc.LookupError) as info:
        lc.fetch_json("https://example.org/x?key=SECRET123", down, source_name="Google Books",
                      unreachable_message="Could not reach Google Books: {detail} (check your internet connection)",
                      secrets=["SECRET123"])
    text = str(info.value)
    assert text.startswith("Could not reach Google Books:") and text.endswith("(check your internet connection)")
    assert "SECRET123" not in text
    with pytest.raises(lc.LookupError, match="Could not reach the lookup service:"):
        lc.fetch_json("https://example.org/x", down)  # the default wording is unchanged
    with pytest.raises(lc.LookupError, match="Could not download the cover: .*offline"):
        lc.fetch_bytes("https://example.org/c", lambda r: (_ for _ in ()).throw(urllib.error.URLError("offline")), what="the cover")

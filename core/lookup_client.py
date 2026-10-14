"""
redactor_common/core/lookup_client.py

Shared plumbing for "look up metadata from an online source" features --
promoted after the same shape turned up independently five times across
two projects: epubredactor's Google Books/Calibre/Open Library lookups,
and cbzredactor's Comic Vine/GCD lookups. Each one had its own copy of
an injectable `fetch` callable (for testing without real network
access), a descriptive User-Agent (some APIs, e.g. Comic Vine, reject
a generic default one outright), and the same HTTPError/URLError/JSON-
decode-error -> friendly-message translation.

Deliberately does NOT know anything about any specific source's
response shape, application-level status codes, or field names -- each
consuming project keeps its own <Source>LookupError subclass and its
own parsing. What's shared here is purely mechanical: making one HTTP
GET, and turning whatever can go wrong into the exception type the
caller already wanted to raise anyway.

Wave 1 of the 2026-10 consolidation added the rest of what the apps
still copied:
  - Throttle: one minimum gap between requests, thread-safe, with an
    injectable clock/sleep (MusicBrainz, Discogs, AcoustID, AniList/
    MangaUpdates each had a copy).
  - throttled(): wraps a fetch so every call waits on a Throttle first.
  - fetch_json()/fetch_bytes() `retry_429=(1.0, 2.0)`: retry an HTTP 429
    after each delay before giving up (epubredactor's Google Books and
    Open Library `_fetch_with_retry` twins), and `status_errors`: raise a
    specific exception class for a status code (Discogs' and GCD's
    RateLimited/Auth errors), so a caller can tell "slow down" or "bad
    login" from any other failure without its own wrapper.
  - make_default_fetch(opener=..., max_bytes=...): a custom urllib
    opener (Discogs' redirect handler that never carries the token to
    another host) and a per-source response-size cap.
  - redact_url()/redact_text(): no error message ever carries a URL's
    query string (where API keys travel) or a value the caller names.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Iterable, Mapping, Optional, Sequence, Type, Union

DEFAULT_TIMEOUT = 8.0
# Bigger than any cover image or metadata JSON; stops a misbehaving
# server streaming until memory runs out.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_ALLOWED_SCHEMES = ("http", "https")
# Everything a fetch can raise that fetch_json/fetch_bytes turn into error_cls:
# ValueError covers a malformed URL, HTTPException a truncated/garbled response.
_FETCH_ERRORS = (urllib.error.URLError, socket.timeout, TimeoutError, OSError, http.client.HTTPException, ValueError)
# A fetch callable takes a plain URL, or a urllib Request when the call
# needs headers (an API key, a bearer token) or a POST body -- see
# build_request().
RequestLike = Union[str, urllib.request.Request]
FetchFn = Callable[[RequestLike], bytes]


class LookupError(Exception):
    """Base class for a metadata-lookup failure. Each source defines
    its own subclass (e.g. ComicVineLookupError) rather than raising
    this directly -- callers can then catch their own specific type
    without also catching every other source's errors by accident."""


# -- keeping secrets out of messages -------------------------------------------

# A URL's query string (and fragment), up to the next whitespace or quote
# -- where API keys travel (?api_key=..., &client=..., &token=...). Any
# scheme, since the "only http and https" refusal names the other ones.
_URL_QUERY_RE = re.compile(r"""([a-z][a-z0-9+.-]*://[^\s?#'"<>]*)[?#][^\s'"<>]*""", re.IGNORECASE)
REDACTED = "***"


def redact_url(url: str) -> str:
    """`url` without its query string or fragment ("...?***"), for a
    message or a log line: the query is where most APIs take the key."""
    return _URL_QUERY_RE.sub(lambda m: f"{m.group(1)}?{REDACTED}", url or "")


def redact_text(text: object, secrets: Iterable[str] = ()) -> str:
    """str(text) with every URL query string and every non-empty value in
    `secrets` (a token, a password) replaced by ***. Used for every error
    message this module builds, so an exception text that echoes the
    request URL (urllib's "unknown url type: '...?key=...'") is safe to
    show in a dialog or write to a crash log."""
    result = redact_url(str(text))
    for secret in secrets:
        if secret:
            result = result.replace(secret, REDACTED)
    return result


# -- pacing --------------------------------------------------------------------

class Throttle:
    """One minimum gap between requests, whichever thread makes them.

    wait() sleeps until `interval` seconds have passed since the previous
    wait() returned; the first call never waits. Thread-safe: concurrent
    callers queue on one lock, so a process-wide Throttle shared by every
    request to a service keeps that service's pace even with a worker
    thread and the GUI both asking. `clock` and `sleep` are injectable so
    tests never really wait.

        _throttle = Throttle(1.1)   # MusicBrainz: at most ~1 request/second
        _throttle.wait()
    """

    def __init__(self, interval: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.interval = float(interval)
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last: Optional[float] = None

    def __repr__(self) -> str:
        return f"Throttle({self.interval!r})"

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            if self._last is not None:
                delay = self.interval - (now - self._last)
                if delay > 0:
                    self._sleep(delay)
                    now = self._clock()
            self._last = now

    def reset(self) -> None:
        """Forget the previous request, so the next wait() returns at once."""
        with self._lock:
            self._last = None


def throttled(fetch: FetchFn, throttle: Throttle) -> FetchFn:
    """A fetch that waits on `throttle` before every call to `fetch` (what
    mp3redactor's MusicBrainz `_default_fetch` did by hand)."""

    def _fetch(url: RequestLike) -> bytes:
        throttle.wait()
        return fetch(url)

    return _fetch


# -- the default fetch ---------------------------------------------------------

def make_default_fetch(user_agent: str, timeout: float = DEFAULT_TIMEOUT,
                       opener=None, max_bytes: Optional[int] = None) -> FetchFn:
    """Returns a fetch(url) -> bytes callable using a fixed User-Agent.
    The request-building mechanics are identical across sources; only
    the User-Agent string needs to vary (and, for some APIs, must).

    `opener`: an object with urllib's OpenerDirector.open(request,
    timeout=) API, e.g. urllib.request.build_opener(SomeRedirectHandler)
    (Discogs: never carry the token to another host on a redirect).
    None uses urllib.request.urlopen.
    `max_bytes`: the largest response accepted (default
    MAX_RESPONSE_BYTES); a bigger one raises OSError("the response was
    too large") instead of being read into memory.

    Only http and https URLs are fetched (ValueError otherwise; the
    message never contains the URL's query string)."""
    limit = MAX_RESPONSE_BYTES if max_bytes is None else int(max_bytes)

    def _fetch(url: RequestLike) -> bytes:
        if isinstance(url, urllib.request.Request):
            request = url
            if not request.has_header("User-agent"):
                request.add_header("User-Agent", user_agent)
        else:
            request = urllib.request.Request(url, headers={"User-Agent": user_agent})
        # urlopen would also follow file: and ftp: URLs; a cover URL taken
        # from an API response must never be able to read a local file.
        if urllib.parse.urlsplit(request.full_url).scheme.lower() not in _ALLOWED_SCHEMES:
            raise ValueError(f"Only http and https URLs are allowed: {redact_url(request.full_url)}")
        if opener is not None:
            response_cm = opener.open(request, timeout=timeout)
        else:
            response_cm = urllib.request.urlopen(request, timeout=timeout)
        with response_cm as response:
            data = response.read(limit + 1)
        if len(data) > limit:
            raise OSError("the response was too large")
        return data

    return _fetch


def build_request(
    url: str,
    params: Mapping[str, object] | None = None,
    headers: Mapping[str, str] | None = None,
    json_body: object = None,
) -> urllib.request.Request:
    """A GET (or, with `json_body`, a JSON POST) request with the query
    string encoded from `params` -- for APIs needing an API-key or
    bearer-token header (TMDB, TheTVDB, OpenSubtitles)."""
    if params:
        url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(params)}"
    all_headers = dict(headers or {})
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        all_headers.setdefault("Content-Type", "application/json")
    return urllib.request.Request(
        url, data=data, headers=all_headers, method="POST" if data is not None else "GET"
    )


def _fetch_with_retry(url: RequestLike, fetch: FetchFn, retry_429: Sequence[float],
                      sleep: Callable[[float], None]) -> bytes:
    """fetch(url), retrying an HTTP 429 once after each delay in
    `retry_429`; the last 429 (or any other error) propagates unchanged.
    A 429 is usually a short-lived limit, not a real failure."""
    remaining = list(retry_429 or ())
    while True:
        try:
            return fetch(url)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and remaining:
                sleep(remaining.pop(0))
                continue
            raise


def _status_error(
    exc: urllib.error.HTTPError,
    default_cls: Type[Exception],
    default_message: str,
    status_messages: Mapping[int, str] | None,
    status_errors: Mapping[int, Type[Exception]] | None,
    secrets: Iterable[str],
) -> Exception:
    """The exception to raise for an HTTP error: the class from
    `status_errors` for this code (else `default_cls`), with the message
    from `status_messages` (else `default_message`), redacted."""
    cls = (status_errors or {}).get(exc.code, default_cls)
    message = (status_messages or {}).get(exc.code, default_message)
    return cls(redact_text(message, secrets))


def fetch_json(
    url: RequestLike,
    fetch: FetchFn,
    error_cls: Type[Exception] = LookupError,
    source_name: str = "the lookup service",
    ignore_404: bool = False,
    status_messages: Mapping[int, str] | None = None,
    status_errors: Mapping[int, Type[Exception]] | None = None,
    retry_429: Sequence[float] = (),
    sleep: Callable[[float], None] = time.sleep,
    secrets: Iterable[str] = (),
) -> Optional[dict]:
    """Runs one GET via `fetch`, parses the JSON body, and translates
    every way that can fail into `error_cls` with a friendly message --
    the HTTPError/URLError/socket.timeout/JSONDecodeError boilerplate
    every existing lookup module had its own copy of.

    `ignore_404`: when True, a 404 response returns None instead of
    raising -- several sources (GCD) treat "no such thing" as a normal,
    expected outcome (a typo, or a series/issue that source simply
    doesn't have) rather than a real error; the caller decides what an
    empty result means (e.g. an empty candidate list) rather than
    catching an exception for it.

    Does NOT check for an application-level error embedded in the JSON
    body itself (e.g. Comic Vine's own status_code field) -- that
    convention varies too much per source to generalize; the caller
    checks its own response shape after this returns.

    `status_messages`: a friendlier message for specific HTTP codes,
    e.g. {401: "TMDB rejected the API key (401 Unauthorized)."}.
    `error_cls` may be any Exception subclass taking a message, so a
    project can keep an existing error type that predates this module.

    `status_errors`: a specific exception class for specific HTTP codes,
    e.g. {429: DiscogsRateLimited, 401: GcdAuthError}, raised with the
    `status_messages` text for that code (or the default message), so the
    caller can catch "slow down" or "bad login" on its own. Applied after
    any 429 retries are used up.

    `retry_429`: delays in seconds, e.g. (1.0, 2.0): an HTTP 429 is
    retried after each one in turn before it counts as an error. Default
    () retries nothing. `sleep` is injectable for tests.

    `secrets`: values (a token, a password) to replace with *** in any
    message. URL query strings are always redacted from messages.
    """
    try:
        raw = _fetch_with_retry(url, fetch, retry_429, sleep)
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and ignore_404:
            return None
        raise _status_error(
            exc, error_cls, f"{source_name} returned an error (HTTP {exc.code}): {exc.reason}",
            status_messages, status_errors, secrets,
        ) from exc
    except _FETCH_ERRORS as exc:
        raise error_cls(redact_text(f"Could not reach {source_name}: {exc}", secrets)) from exc

    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise error_cls(f"Received an unreadable response from {source_name}.") from exc


def fetch_bytes(
    url: RequestLike,
    fetch: FetchFn,
    error_cls: Type[Exception] = LookupError,
    what: str = "the file",
    status_messages: Mapping[int, str] | None = None,
    require_data: bool = False,
    status_errors: Mapping[int, Type[Exception]] | None = None,
    retry_429: Sequence[float] = (),
    sleep: Callable[[float], None] = time.sleep,
    secrets: Iterable[str] = (),
) -> bytes:
    """Runs one GET via `fetch` and returns the raw response bytes
    (e.g. a cover image) -- same HTTPError/URLError translation as
    fetch_json(), just without the JSON-parsing step. `what` names the
    thing being downloaded in the error message (e.g. "cover image").
    `status_messages` overrides the message for specific HTTP codes;
    `require_data` treats an empty response body as a failure too.
    `status_errors`, `retry_429`, `sleep` and `secrets` work as in
    fetch_json()."""
    try:
        data = _fetch_with_retry(url, fetch, retry_429, sleep)
    except urllib.error.HTTPError as exc:
        raise _status_error(
            exc, error_cls, f"Could not download {what} (HTTP {exc.code}): {exc.reason}",
            status_messages, status_errors, secrets,
        ) from exc
    except _FETCH_ERRORS as exc:
        raise error_cls(redact_text(f"Could not download {what}: {exc}", secrets)) from exc
    if require_data and not data:
        raise error_cls(f"Downloading {what} returned no data.")
    return data

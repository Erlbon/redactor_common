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
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Mapping, Optional, Type, Union

DEFAULT_TIMEOUT = 8.0
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


def make_default_fetch(user_agent: str, timeout: float = DEFAULT_TIMEOUT) -> FetchFn:
    """Returns a fetch(url) -> bytes callable using a fixed User-Agent.
    The request-building mechanics are identical across sources; only
    the User-Agent string needs to vary (and, for some APIs, must)."""

    def _fetch(url: RequestLike) -> bytes:
        if isinstance(url, urllib.request.Request):
            request = url
            if not request.has_header("User-agent"):
                request.add_header("User-Agent", user_agent)
        else:
            request = urllib.request.Request(url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()

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


def fetch_json(
    url: RequestLike,
    fetch: FetchFn,
    error_cls: Type[Exception] = LookupError,
    source_name: str = "the lookup service",
    ignore_404: bool = False,
    status_messages: Mapping[int, str] | None = None,
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
    """
    try:
        raw = fetch(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and ignore_404:
            return None
        if status_messages and exc.code in status_messages:
            raise error_cls(status_messages[exc.code]) from exc
        raise error_cls(f"{source_name} returned an error (HTTP {exc.code}): {exc.reason}") from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        raise error_cls(f"Could not reach {source_name}: {exc}") from exc

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
) -> bytes:
    """Runs one GET via `fetch` and returns the raw response bytes
    (e.g. a cover image) -- same HTTPError/URLError translation as
    fetch_json(), just without the JSON-parsing step. `what` names the
    thing being downloaded in the error message (e.g. "cover image").
    `status_messages` overrides the message for specific HTTP codes;
    `require_data` treats an empty response body as a failure too."""
    try:
        data = fetch(url)
    except urllib.error.HTTPError as exc:
        if status_messages and exc.code in status_messages:
            raise error_cls(status_messages[exc.code]) from exc
        raise error_cls(f"Could not download {what} (HTTP {exc.code}): {exc.reason}") from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        raise error_cls(f"Could not download {what}: {exc}") from exc
    if require_data and not data:
        raise error_cls(f"Downloading {what} returned no data.")
    return data

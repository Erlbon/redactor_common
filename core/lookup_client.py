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
import urllib.request
from typing import Callable, Optional, Type

DEFAULT_TIMEOUT = 8.0
FetchFn = Callable[[str], bytes]


class LookupError(Exception):
    """Base class for a metadata-lookup failure. Each source defines
    its own subclass (e.g. ComicVineLookupError) rather than raising
    this directly -- callers can then catch their own specific type
    without also catching every other source's errors by accident."""


def make_default_fetch(user_agent: str, timeout: float = DEFAULT_TIMEOUT) -> FetchFn:
    """Returns a fetch(url) -> bytes callable using a fixed User-Agent.
    The request-building mechanics are identical across sources; only
    the User-Agent string needs to vary (and, for some APIs, must)."""

    def _fetch(url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()

    return _fetch


def fetch_json(
    url: str,
    fetch: FetchFn,
    error_cls: Type[LookupError] = LookupError,
    source_name: str = "the lookup service",
    ignore_404: bool = False,
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
    """
    try:
        raw = fetch(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and ignore_404:
            return None
        raise error_cls(f"{source_name} returned an error (HTTP {exc.code}): {exc.reason}") from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        raise error_cls(f"Could not reach {source_name}: {exc}") from exc

    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise error_cls(f"Received an unreadable response from {source_name}.") from exc

"""
redactor_common/core/notes_cache.py -- the request layer of Create Notes (shared by the apps that write folder
notes): a per-run memory memo and a persistent disk cache.

  CachedFetch(inner, source, cache, stats)   wraps a `fetch(url) -> bytes` (MusicBrainz, Wikipedia, TMDB): the same URL
                                              within a run is answered from memory, an answer stored on disk by an earlier
                                              run (or a cancelled one) is used again, only the rest reaches the network
  ResponseCache(path)                        the disk store: SQLite at `path` (each app puts it next to its settings file),
                                              keyed by the full request URL, never a header (tokens travel in headers, not
                                              in the URL), thread-safe, corruption-tolerant, size-capped, with a TTL
  RequestStats                               requests sent / answered from the cache / answered from memory
  default_cache(path) / clear_default_cache(path)   the process-wide cache of an app

What is stored: public service responses and the names asked about (they are in the URL). Nothing else about the
user. Errors, 503s and unreadable answers are NEVER stored; a 404 ('no such page') and an empty result list live one
day, everything else 30 days. A broken cache file is deleted and started again without a word.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TTL_SECONDS = 30 * 24 * 3600
NEGATIVE_TTL_SECONDS = 24 * 3600
MAX_BYTES = 200 * 1024 * 1024
USED_REFRESH_SECONDS = 3600  # a hit refreshes the entry's 'used' stamp at most this often (one write, not one per read)

__all__ = ["CachedFetch", "RequestStats", "ResponseCache", "default_cache", "clear_default_cache", "looks_empty",
           "MAX_BYTES", "NEGATIVE_TTL_SECONDS", "TTL_SECONDS"]


@dataclass
class RequestStats:
    """What one run asked for. `requests` reached the network; `from_cache` came from the disk cache."""

    requests: int = 0
    from_cache: int = 0
    from_memory: int = 0

    def summary(self) -> str:
        text = f"{self.requests} request(s), {self.from_cache} from the cache"
        return text + (f", {self.from_memory} repeated within the run" if self.from_memory else "")


def looks_empty(body: bytes) -> bool:
    """A JSON object whose lists are all empty (a search that found nothing): such an answer may change soon."""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    lists = [v for v in data.values() if isinstance(v, list)]
    return bool(lists) and all(not v for v in lists)


class ResponseCache:
    """The persistent store. All methods are safe to call from several threads and never raise."""

    def __init__(self, path: str | os.PathLike, ttl: float = TTL_SECONDS, negative_ttl: float = NEGATIVE_TTL_SECONDS,
                 max_bytes: int = MAX_BYTES, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.ttl, self.negative_ttl, self.max_bytes, self._clock = ttl, negative_ttl, max_bytes, clock
        self.used_refresh = USED_REFRESH_SECONDS
        self._lock = threading.Lock()
        self._db: Optional[sqlite3.Connection] = None
        self._broken = False
        self.hits = 0
        self.misses = 0
        self.stores = 0

    # -- connection ------------------------------------------------------------------------------------------

    def _connect(self) -> Optional[sqlite3.Connection]:
        if self._broken:
            return None
        if self._db is not None:
            return self._db
        for attempt in (1, 2):
            db = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                db = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
                # WAL + synchronous NORMAL: a write is an append to the log, not an fsync of the database file (the
                # default journal costs about 3 ms per request on Windows); a crash can lose the last few answers,
                # which only means asking the service again.
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=NORMAL")
                db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, status INTEGER NOT NULL, "
                           "body BLOB, expires REAL NOT NULL, used REAL NOT NULL, size INTEGER NOT NULL)")
                db.execute("SELECT COUNT(*) FROM cache").fetchone()  # a damaged file fails here
                self._db = db
                return db
            except (sqlite3.Error, OSError):
                if db is not None:
                    try:
                        db.close()  # an open handle would keep the damaged file from being deleted (Windows)
                    except sqlite3.Error:
                        pass
                self._drop_file()
        self._broken = True  # cannot even create one: run without a cache
        return None

    def _drop_file(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except sqlite3.Error:
                pass
            self._db = None
        for suffix in ("", "-journal", "-wal", "-shm"):
            try:
                os.remove(str(self.path) + suffix)
            except OSError:
                pass

    def _guard(self, work, default=None):
        """Run `work(db)` under the lock; any database trouble drops the file once and yields `default`."""
        with self._lock:
            db = self._connect()
            if db is None:
                return default
            try:
                return work(db)
            except sqlite3.DatabaseError:
                self._drop_file()
                return default
            except sqlite3.Error:
                return default

    # -- the API ---------------------------------------------------------------------------------------------

    def get(self, key: str) -> Optional[tuple[int, bytes]]:
        """(status, body) of a fresh entry (status 404 has no body), else None."""
        now = self._clock()

        def work(db):
            row = db.execute("SELECT status, body, expires, used FROM cache WHERE key=?", (key,)).fetchone()
            if row is None:
                return None
            if row[2] <= now:
                db.execute("DELETE FROM cache WHERE key=?", (key,))
                db.commit()
                return None
            if now - row[3] > self.used_refresh:  # least-recently-used bookkeeping, not on every single read
                db.execute("UPDATE cache SET used=? WHERE key=?", (now, key))
                db.commit()
            return int(row[0]), bytes(row[1] or b"")

        found = self._guard(work)
        if found is None:
            self.misses += 1
        else:
            self.hits += 1
        return found

    def put(self, key: str, body: bytes, status: int = 200) -> None:
        """Stores a good answer (status 200) or a 'not found' (404). Nothing else is ever stored."""
        if status not in (200, 404):
            return
        short = status == 404 or looks_empty(body)
        now = self._clock()
        expires = now + (self.negative_ttl if short else self.ttl)
        size = len(body) + len(key)

        def work(db):
            db.execute("INSERT OR REPLACE INTO cache (key, status, body, expires, used, size) VALUES (?,?,?,?,?,?)",
                       (key, status, body if status == 200 else b"", expires, now, size))
            total = db.execute("SELECT COALESCE(SUM(size),0) FROM cache").fetchone()[0]
            if total > self.max_bytes:  # evict the least recently used until 90% of the cap
                goal = int(self.max_bytes * 0.9)
                for rowkey, rowsize in db.execute("SELECT key, size FROM cache ORDER BY used ASC").fetchall():
                    if total <= goal:
                        break
                    db.execute("DELETE FROM cache WHERE key=?", (rowkey,))
                    total -= rowsize
            db.commit()

        self._guard(work)
        self.stores += 1

    def clear(self) -> int:
        """Empties the cache; returns how many entries went."""
        def work(db):
            n = db.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
            db.execute("DELETE FROM cache")
            db.commit()
            try:
                db.execute("VACUUM")
            except sqlite3.Error:
                pass
            return n

        return self._guard(work, 0) or 0

    def count(self) -> int:
        return self._guard(lambda db: db.execute("SELECT COUNT(*) FROM cache").fetchone()[0], 0) or 0

    def total_bytes(self) -> int:
        return self._guard(lambda db: db.execute("SELECT COALESCE(SUM(size),0) FROM cache").fetchone()[0], 0) or 0

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                try:
                    self._db.close()
                except sqlite3.Error:
                    pass
                self._db = None


_default: Optional[ResponseCache] = None
_default_lock = threading.Lock()


def default_cache(path: str | os.PathLike, enabled: bool = True) -> Optional[ResponseCache]:
    """The process-wide cache at `path` (the app's own cache_path(): next to its settings file, never in a library
    folder), or None when switched off."""
    global _default
    if not enabled:
        return None
    path = Path(path)
    with _default_lock:
        if _default is None or _default.path != path:
            _default = ResponseCache(path)
        return _default


def clear_default_cache(path: str | os.PathLike) -> int:
    """Preferences > Clear the notes lookup cache: the entries removed (0 when there is none)."""
    path = Path(path)
    if not path.exists():
        return 0
    return (default_cache(path) or ResponseCache(path)).clear()


class CachedFetch:
    """`fetch(url) -> bytes` with a memory memo and the disk cache in front of `inner`."""

    def __init__(self, inner: Callable, source: str, cache: Optional[ResponseCache] = None,
                 stats: Optional[RequestStats] = None, phases=None, phase: str = "", secret_params: tuple = ()):
        self.inner, self.source, self.cache = inner, source, cache
        self.secret_params = tuple(secret_params)  # query parameters (an API key) that are NOT part of the key
        self.stats = stats if stats is not None else RequestStats()
        self.phases, self.phase = phases, phase  # core/notes_phases.py: seconds of the real requests, throttle waits included
        self.busy_hook: Optional[Callable[[float], None]] = None  # told when the service said 'busy' and we are about to wait
        self._memo: dict[str, tuple[int, bytes]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def url_of(request) -> str:
        return request if isinstance(request, str) else request.full_url

    def key_of(self, url: str) -> str:
        """The cache key of `url`: the source and the URL with the secret query parameters taken out, so an API key
        sent as ?api_key=... is never written to the cache file (and a changed key still hits the cache)."""
        if self.secret_params:
            parts = urlsplit(url)
            query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in self.secret_params]
            url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
        return f"{self.source}|{url}"

    def __call__(self, request):
        url = self.url_of(request)
        key = self.key_of(url)  # the URL only (minus secret parameters): never a header
        with self._lock:
            found = self._memo.get(key)
        if found is not None:
            self.stats.from_memory += 1
            return self._answer(url, found)
        if self.cache is not None:
            found = self.cache.get(key)
            if found is not None:
                self.stats.from_cache += 1
                with self._lock:
                    self._memo[key] = found
                return self._answer(url, found)
        self.stats.requests += 1
        started = time.perf_counter()
        try:
            body = self.inner(request)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                self._remember(key, 404, b"")
            raise
        finally:
            if self.phases is not None:
                self.phases.add(self.phase, time.perf_counter() - started)
        self._remember(key, 200, body)
        return body

    def on_busy(self, delay: float) -> None:
        """Called by the retry loop of notes_musicbrainz before it waits `delay` seconds for a busy service: the wait is
        part of the phase's time and the person is told (never wait silently)."""
        if self.phases is not None:
            self.phases.add(self.phase, delay, count=0)
            self.phases.add("busy", delay)
        if self.busy_hook is not None:
            self.busy_hook(delay)

    def _remember(self, key: str, status: int, body: bytes) -> None:
        with self._lock:
            self._memo[key] = (status, body)
        if self.cache is not None:
            self.cache.put(key, body, status)

    @staticmethod
    def _answer(url: str, found: tuple[int, bytes]):
        status, body = found
        if status == 404:
            raise urllib.error.HTTPError(url, 404, "Not Found (remembered)", {}, None)
        return body

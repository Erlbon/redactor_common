"""
redactor_common/core/scan_stamp.py

The record of "this file was scanned/redacted at <time>, result <status>"
that every Redact app keeps INSIDE the file (an ID3 TXXX frame, an OPF
<meta>, a ComicInfo/zip-comment field, an MKV/MP4 tag) so it follows the
file when it is copied around. Each app stores the string made here in
its own format-specific place, loads it back on open, and writes it on
Save like any other metadata field -- a scan never writes by itself.

Wire format, one short line:

    STATUS;2026-09-30T14:05:11Z            (scan result + when)
    STATUS;2026-09-30T14:05:11Z;3f9a...    (... + content fingerprint)

STATUS is a short token without ";" (OK, WARNING, ERROR, ...). The
timestamp is always UTC, second precision. The fingerprint is optional:
it lets a later run tell whether the audio/pages/body changed since the
stamp was written, without relying on the modified time (which a copy
often resets and any tag write changes). Parsing never raises -- a
garbled value is simply "no stamp".

content_fingerprint() is deliberately cheap: file size plus a hash of a
few sampled chunks, not the whole file, so checking a stamp costs far
less than the scan it saves. `skip` excludes byte ranges that legitimately
change when only metadata is edited (an ID3 tag at the start of an MP3,
a trailing ID3v1/APE block), so a tag edit doesn't invalidate the stamp
but a change to the payload does.
"""

from __future__ import annotations

import datetime
import hashlib
import os
from dataclasses import dataclass
from typing import Iterable, Optional

SEPARATOR = ";"
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_SAMPLE_BYTES = 64 * 1024
_SAMPLE_POINTS = (0.0, 0.5, 1.0)  # start, middle, end of the hashed region


@dataclass(frozen=True)
class ScanStamp:
    status: str
    timestamp: str  # ISO-8601 UTC, "%Y-%m-%dT%H:%M:%SZ"
    fingerprint: str = ""

    def to_text(self) -> str:
        parts = [self.status, self.timestamp]
        if self.fingerprint:
            parts.append(self.fingerprint)
        return SEPARATOR.join(parts)

    def when(self) -> Optional[datetime.datetime]:
        """The timestamp as an aware UTC datetime, or None if unparseable."""
        try:
            return datetime.datetime.strptime(self.timestamp, _TIME_FORMAT).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            return None

    def display(self, local: bool = True, with_time: bool = True) -> str:
        """"OK · 2026-09-30 14:05" -- the status and the time in the user's
        local zone (or "OK · 2026-09-30" without the time). Falls back to
        the bare status if the timestamp can't be read."""
        moment = self.when()
        if moment is None:
            return self.status
        if local:
            moment = moment.astimezone()
        return f"{self.status} · {moment.strftime('%Y-%m-%d %H:%M' if with_time else '%Y-%m-%d')}"

    def tooltip(self, message: str = "") -> str:
        moment = self.when()
        when = moment.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z").strip() if moment else self.timestamp
        text = f"Scanned {when}: {self.status}"
        return f"{text}\n{message}" if message else text


def now_timestamp(now: Optional[datetime.datetime] = None) -> str:
    """The current UTC time in the stamp format (`now` for tests)."""
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    return moment.astimezone(datetime.timezone.utc).strftime(_TIME_FORMAT)


def make_stamp(status: str, fingerprint: str = "", now: Optional[datetime.datetime] = None) -> ScanStamp:
    """A stamp for a scan that just ran. `status` must be a short token."""
    token = (status or "").strip().replace(SEPARATOR, "_")
    return ScanStamp(token, now_timestamp(now), fingerprint)


def parse_stamp(text: object) -> Optional[ScanStamp]:
    """The ScanStamp in `text`, or None for anything that isn't one
    (empty, wrong shape, unreadable timestamp). Never raises."""
    if not isinstance(text, str):
        return None
    parts = [p.strip() for p in text.strip().split(SEPARATOR)]
    if len(parts) not in (2, 3) or not parts[0]:
        return None
    stamp = ScanStamp(parts[0], parts[1], parts[2] if len(parts) == 3 else "")
    return stamp if stamp.when() is not None else None


def content_fingerprint(path: str, skip: Iterable[tuple[int, int]] = ()) -> str:
    """"<size>-<hash>" for the file's payload: its length (excluding the
    `skip` byte ranges, each (start, end) with end exclusive) and a
    SHA-256 over up to three 64 KB chunks taken from the start, middle and
    end of what is left. Returns "" if the file can't be read.

    Cheap by design (a fraction of a full read on large files), so it can
    gate an expensive scan. It is a change detector, not a checksum: an
    edit confined to the unsampled parts of a big file would go unnoticed,
    which is an acceptable trade for deciding "rescan or trust the stamp"."""
    try:
        size = os.path.getsize(path)
        ranges = sorted((max(0, s), min(size, e)) for s, e in skip if e > s)
        # the byte spans that remain once the skipped ranges are cut out
        spans: list[tuple[int, int]] = []
        cursor = 0
        for start, end in ranges:
            if start > cursor:
                spans.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < size:
            spans.append((cursor, size))
        payload = sum(end - start for start, end in spans)

        digest = hashlib.sha256()
        digest.update(str(payload).encode("ascii"))
        with open(path, "rb") as handle:
            for point in _SAMPLE_POINTS:
                offset = int(max(0, payload - _SAMPLE_BYTES) * point)
                digest.update(_read_payload(handle, spans, offset, _SAMPLE_BYTES))
        return f"{payload}-{digest.hexdigest()[:16]}"
    except OSError:
        return ""


def _read_payload(handle, spans: list[tuple[int, int]], offset: int, length: int) -> bytes:
    """`length` bytes of the payload (the spans joined end to end)
    starting `offset` bytes into it."""
    out = bytearray()
    skipped = 0
    for start, end in spans:
        span_len = end - start
        if skipped + span_len <= offset:
            skipped += span_len
            continue
        begin = start + max(0, offset - skipped)
        handle.seek(begin)
        out += handle.read(min(length - len(out), end - begin))
        skipped += span_len
        if len(out) >= length:
            break
    return bytes(out)


def stamp_is_current(stamp: Optional[ScanStamp], path: str, skip: Iterable[tuple[int, int]] = ()) -> bool:
    """True when `stamp` carries a fingerprint and the file's payload still
    matches it -- i.e. the recorded scan still describes this file. A stamp
    with no fingerprint can't be checked, so it is never "current"."""
    if stamp is None or not stamp.fingerprint:
        return False
    return content_fingerprint(path, skip) == stamp.fingerprint

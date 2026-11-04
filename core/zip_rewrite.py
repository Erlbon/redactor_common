"""
redactor_common/core/zip_rewrite.py

The ONE way the Redactor apps and Dossier rewrite a ZIP archive (a cbz, an
epub, any zip) entry by entry, and write a new one. Promoted from
cbzredactor's core/zip_rewrite.py (2026-11-04), where Save, Remove Pages,
Clean Up and Resize had carried four near-identical loops that drifted apart,
and merged with what Dossier's own writer did better (the CRC check of the
result, entries streamed in chunks instead of read whole, byte progress).

What holds for every rewrite:

- the source is never written to: the new archive goes to a temp file beside
  the destination, is checked (entry count, every entry's CRC, and whatever
  the plan adds), and only then replaces the destination (os.replace,
  retried briefly when another reader holds the file open); when the
  original goes to the Recycle Bin (`dispose_original`, the trasher hook) it
  is first renamed aside, so it exists under some name until the new file is
  in place;
- every entry's ZIP metadata is carried over (timestamp, compression,
  attributes, internal attributes, creating system, entry comment, and the
  extra fields that stay valid), and so is the archive comment unless the
  caller replaces it; a legacy (unflagged, non-UTF-8) entry name keeps its
  bytes (core/zip_names.py);
- an entry whose bytes nobody asked for is streamed in chunks, so a
  multi-gigabyte member never has to fit in memory;
- the temp file is removed on EVERY failure (a bad archive, an encrypted or
  unsupported-compression entry, a full disk, a caller's own exception, a
  cancel, a refused Recycle Bin);
- the zip library's many failure types all surface as RewriteError.

The caller describes the rewrite with a RewritePlan: a per-entry decision
(keep / drop / rename / replace the bytes), entries to add before or after
the copied ones, an optional reordering and optional checks on the result.
Nothing here knows about comics or epubs.

write_new_archive() writes a brand-new zip from NewEntry items (bytes or a
file on disk); it is what "pack these files" and "convert to cbz" use.
"""

from __future__ import annotations

import os
import shutil
import struct
import time
import zipfile
import zlib
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Union

from redactor_common.core.os_utils import replace_with_retry, retry_on_lock
from redactor_common.core.save_errors import describe_save_error
from redactor_common.core.zip_names import RawNameInfo, legacy_raw_name, open_zip, raw_names_supported

try:  # a Python built without lzma has no LZMAError to catch
    from lzma import LZMAError as _LZMAError
except ImportError:  # pragma: no cover
    _LZMAError = OSError

# What zipfile/zlib/lzma/bz2 raise for a bad archive or entry. RuntimeError is
# an encrypted entry, NotImplementedError a compression method zipfile has no
# codec for, ValueError a malformed header/name, EOFError/zlib.error/LZMAError
# a truncated or damaged compressed stream, KeyError a missing entry (bz2
# raises OSError).
ZIP_ERRORS = (
    zipfile.BadZipFile, KeyError, OSError, zlib.error, RuntimeError, NotImplementedError, ValueError, EOFError,
    _LZMAError,
)

CHUNK = 1 << 20
ZIP64_LIMIT = (1 << 31) - 1


class ZipRewriteError(Exception):
    """Raised for a problem reading or writing an archive (cbzredactor calls it CbzError)."""


class RewriteError(ZipRewriteError):
    """A rewrite failed (the source is untouched, the temp file is gone).
    `detail` is the reason without the "Could not ..." prefix."""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail or message


class RewriteCancelled(ZipRewriteError):
    """`should_cancel` fired; the temp file was removed and the source left
    untouched."""


# --- putting a new file in place ---------------------------------------------

# Keyword arguments for retry_on_lock (tests set the delay to 0).
_LOCK_RETRY: dict = {}


def _replace(tmp_path: str, path: str) -> None:
    """os.replace, retried through a transient Windows lock (PermissionError
    or winerror 5/32/33: a scan of the app's own, a virus scanner, the
    indexer, a sync client)."""
    replace_with_retry(tmp_path, path, **_LOCK_RETRY)


def _rename(src: str, dst: str) -> None:
    retry_on_lock(os.rename, src, dst, **_LOCK_RETRY)


def backup_name(path: str) -> str:
    """`<stem>.redact-orig<ext>` beside `path` (numbered if taken): where the
    original waits while the rewritten file takes its name -- the same
    sibling commit_in_place uses. Dossier passes its own namer
    (`<name>.dossier-old`)."""
    stem, ext = os.path.splitext(path)
    candidate = f"{stem}.redact-orig{ext}"
    n = 1
    while os.path.lexists(candidate):
        n += 1
        candidate = f"{stem}.redact-orig{n}{ext}"
    return candidate


class _OriginalKeptAside(OSError):
    """Putting the original back failed after a later step failed; it is
    safe under its set-aside name, which the message gives."""


def swap_in(
    tmp_path: str,
    path: str,
    dispose_original: Optional[Callable[[str], None]] = None,
    *,
    namer: Callable[[str], str] = backup_name,
    keep_new_if_dispose_fails: bool = False,
) -> None:
    """Puts `tmp_path` in place of `path` and hands the original to
    `dispose_original` (the Recycle Bin), never leaving the original
    without a name on disk until the new file is in place:

      1. rename the original aside to `namer(path)`;
      2. os.replace(tmp, path) -- on failure the original is renamed back;
      3. dispose_original(<the set-aside file>) -- on failure the original
         is put back over the new file, so a refused Recycle Bin still means
         "nothing was changed"; with `keep_new_if_dispose_fails` (Dossier's
         choice) the new file stays and the original waits beside it under
         its set-aside name.

    A dispose that returns but leaves the sibling behind (a bin that did not
    take it) keeps it: a doubtful trash never becomes a delete. Without
    `dispose_original` the original is simply replaced."""
    if dispose_original is None or not os.path.exists(path):
        _replace(tmp_path, path)
        return
    backup = namer(path)
    _rename(path, backup)
    try:
        _replace(tmp_path, path)
    except BaseException as exc:
        _restore(backup, path, exc)
        raise
    try:
        dispose_original(backup)
    except BaseException as exc:
        if keep_new_if_dispose_fails and isinstance(exc, OSError):
            return
        _restore(backup, path, exc, replace=True)
        raise


def _restore(backup: str, path: str, cause: BaseException, replace: bool = False) -> None:
    try:
        (_replace if replace else _rename)(backup, path)
    except OSError as rb_exc:
        raise _OriginalKeptAside(
            f"{describe_save_error(cause)}, and putting the original back failed "
            f"({describe_save_error(rb_exc)}): your original is safe at {backup}"
        ) from cause


def _discard_temp(tmp_path: str) -> None:
    """Removes a writer's temp file after a failure (it is never the only
    copy: the original is still on disk under its own or its backup name)."""
    try:
        os.remove(tmp_path)
    except OSError:
        pass


# --- the plan ----------------------------------------------------------------


@dataclass
class Entry:
    """A source entry as a decision sees it."""

    name: str
    info: zipfile.ZipInfo

    @property
    def is_dir(self) -> bool:
        return self.name.endswith("/")


@dataclass
class Action:
    """What to do with one source entry. The default keeps it as it is.
    `drop` leaves it out; `rename` writes it under another name; `data`
    writes these bytes instead of its own (its other metadata is kept;
    `date_time` stamps a replaced entry that is really new content;
    `compress_type` overrides how it is stored -- Resize stores re-encoded
    pages instead of deflating a JPEG a second time)."""

    drop: bool = False
    rename: Optional[str] = None
    data: Optional[bytes] = None
    date_time: Optional[tuple] = None
    compress_type: Optional[int] = None


@dataclass
class NewEntry:
    """An entry that isn't in the source: `data`, or the file at
    `source_path` (streamed). `date_time` None means now; a name ending in
    "/" is a folder. `external_attr`/`create_system` None give what
    ZipFile.writestr(name, data) gives a new file."""

    name: str
    data: bytes = b""
    compress_type: int = zipfile.ZIP_STORED
    date_time: Optional[tuple] = None
    source_path: Optional[str] = None
    compresslevel: Optional[int] = None
    external_attr: Optional[int] = None
    create_system: Optional[int] = None

    def size(self) -> int:
        if self.source_path is None:
            return len(self.data)
        try:
            return os.path.getsize(self.source_path)
        except OSError:
            return 0


# decide(entry, read) -> Action | None (keep) | a zero-argument callable that
# returns the Action later (see RewritePlan.window). read() gives the entry's
# uncompressed bytes (read once, then remembered).
Decision = Union[Action, None, Callable[[], Optional[Action]]]


@dataclass
class RewritePlan:
    decide: Optional[Callable[[Entry, Callable[[], bytes]], Decision]] = None  # None: keep everything
    before: list = field(default_factory=list)  # NewEntry written ahead of the copied entries
    after: list = field(default_factory=list)  # NewEntry written after them
    comment: Optional[bytes] = None  # None keeps the source's archive comment; b"" clears it
    order: Optional[Callable[[list], list]] = None  # entries (source order) -> the order to write them
    begin: Optional[Callable[[list], None]] = None  # called once with every Entry, before any decision
    # Up to `window` entries are decided ahead of the one being written (a sliding
    # window), and they are written in order. A decision that returns a callable
    # lets the caller start slow work for those entries (Resize runs its page
    # encoders on a thread pool) and finish it when the entry's turn comes.
    window: int = 1
    verify_crc: bool = True  # re-read the written archive and check every entry's CRC
    verify: Optional[Callable[[zipfile.ZipFile], Optional[str]]] = None  # a problem message, or None if fine


@dataclass
class RewriteResult:
    kept: int = 0  # copied unchanged
    dropped: list = field(default_factory=list)  # names left out
    renamed: dict = field(default_factory=dict)  # old name -> new name
    replaced: list = field(default_factory=list)  # names whose bytes were replaced
    added: list = field(default_factory=list)  # names of NewEntry items
    bytes_in: int = 0  # uncompressed bytes read from the source
    bytes_out: int = 0  # uncompressed bytes written
    entries_written: int = 0
    files: int = 0  # entries written that are not folders
    folders: int = 0  # folder entries written
    skipped: list = field(default_factory=list)  # (source_path, reason): NewEntry files that could not be read


# --- copying entry metadata --------------------------------------------------

# Extra-field ids that must not be copied: zip64 (zipfile writes its own, and
# a copy would be duplicated) and the Info-ZIP unicode path/comment fields
# (they carry a checksum of the old name, wrong once an entry is renamed).
_DROP_EXTRA_IDS = {0x0001, 0x7075, 0x7063}


def safe_extra(extra: bytes) -> bytes:
    """`extra` without the fields that go stale when an entry is copied. A
    malformed field list is dropped whole, never half-copied."""
    kept = []
    pos = 0
    while pos < len(extra):
        if pos + 4 > len(extra):
            return b""
        header_id, size = struct.unpack("<HH", extra[pos:pos + 4])
        if pos + 4 + size > len(extra):
            return b""
        if header_id not in _DROP_EXTRA_IDS:
            kept.append(extra[pos:pos + 4 + size])
        pos += 4 + size
    return b"".join(kept)


def _set_level(info: zipfile.ZipInfo, level: Optional[int]) -> None:
    if level is None:
        return
    if hasattr(info, "compress_level"):  # Python 3.13+
        info.compress_level = level
    else:  # pragma: no cover
        info._compresslevel = level


def _make_info(name: str, source: Optional[zipfile.ZipInfo], date_time: Optional[tuple] = None) -> zipfile.ZipInfo:
    """A fresh ZipInfo for the output, with `source`'s metadata."""
    when = date_time or (source.date_time if source else _now())
    raw = legacy_raw_name(source) if source is not None and name == source.filename else None
    if raw is not None:
        # A legacy-encoded name kept under its own name: its bytes go back as they were.
        if not raw_names_supported():
            raise RewriteError(
                "Cannot rewrite the archive: its entry names use an old encoding that this Python "
                "version can't keep exactly"
            )
        info = RawNameInfo(name, when, raw)
    else:
        info = zipfile.ZipInfo(name, date_time=when)
    if source is None:
        info.external_attr = 0o600 << 16  # what ZipFile.writestr(name, data) gives a new file
        if name.endswith("/"):
            info.external_attr = (0o40755 << 16) | 0x10
    else:
        info.compress_type = source.compress_type
        info.external_attr = source.external_attr
        info.internal_attr = source.internal_attr
        info.create_system = source.create_system
        info.comment = source.comment
        info.extra = safe_extra(source.extra)
    return info


def _new_info(new: NewEntry) -> zipfile.ZipInfo:
    info = _make_info(new.name, None, new.date_time)
    info.compress_type = zipfile.ZIP_STORED if new.name.endswith("/") else new.compress_type
    _set_level(info, new.compresslevel)
    if new.external_attr is not None:
        info.external_attr = new.external_attr
    if new.create_system is not None:
        info.create_system = new.create_system
    return info


def _now() -> tuple:
    return time.localtime()[:6]


def now_date_time() -> tuple:
    """A ZIP timestamp for 'this moment' (an entry whose content is new)."""
    return _now()


def zip_date_time(mtime: float) -> tuple:
    """A ZIP timestamp for a file's mtime; a zip cannot hold a date before 1980."""
    try:
        return time.localtime(max(mtime, 315532800))[:6]
    except (OverflowError, OSError, ValueError):
        return (1980, 1, 1, 0, 0, 0)


# --- writing entries ---------------------------------------------------------


class _Counter:
    """Bytes done for `progress(done, total)`, and the cancel poll between chunks."""

    def __init__(self, total: int, progress, should_cancel) -> None:
        self.total, self.done = max(total, 1), 0
        self.progress, self.should_cancel = progress, should_cancel

    def check(self) -> None:
        if self.should_cancel is not None and self.should_cancel():
            raise RewriteCancelled("Cancelled")

    def add(self, n: int) -> None:
        self.done += n
        if self.progress is not None:
            self.progress(min(self.done, self.total), self.total)


def _stream(src, dst: zipfile.ZipFile, info: zipfile.ZipInfo, size: int, counter: _Counter) -> int:
    """Copies the open binary file `src` into `dst` as `info`, chunk by chunk."""
    info.file_size = size
    written = 0
    with dst.open(info, "w", force_zip64=size > ZIP64_LIMIT) as out:
        while True:
            counter.check()
            block = src.read(CHUNK)
            if not block:
                break
            out.write(block)
            written += len(block)
            counter.add(len(block))
    return written


class _Writer:
    """Writes entries into `dst`, refusing two entries of the same name."""

    def __init__(self, dst: zipfile.ZipFile, result: RewriteResult, counter: _Counter) -> None:
        self.dst, self.result, self.counter = dst, result, counter
        self.written: dict[str, bool] = {}  # output name -> came straight from a source entry

    def _claim(self, name: str, from_source: bool) -> None:
        previous = self.written.get(name)
        if previous is not None and not (previous and from_source):
            raise RewriteError(f"Cannot rewrite the archive: two entries would be named {name!r}")
        self.written[name] = from_source

    def _count(self, info: zipfile.ZipInfo, size: int) -> None:
        self.result.entries_written += 1
        self.result.bytes_out += size
        if info.filename.endswith("/"):
            self.result.folders += 1
        else:
            self.result.files += 1

    def put(self, info: zipfile.ZipInfo, data: bytes, from_source: bool) -> None:
        self._claim(info.filename, from_source)
        self.dst.writestr(info, data)
        self._count(info, len(data))
        self.counter.add(len(data))

    def put_stream(self, info: zipfile.ZipInfo, src, size: int, from_source: bool) -> None:
        self._claim(info.filename, from_source)
        written = _stream(src, self.dst, info, size, self.counter)
        self._count(info, written)

    def put_new(self, new: NewEntry, on_unreadable=None) -> bool:
        """Writes a NewEntry; False when its file could not be opened and `on_unreadable` took it."""
        info = _new_info(new)
        if new.source_path is None or new.name.endswith("/"):
            self.put(info, b"" if new.name.endswith("/") else new.data, False)
            return True
        try:
            src = open(new.source_path, "rb")
            size = os.fstat(src.fileno()).st_size
        except OSError as exc:
            if on_unreadable is None:
                raise
            on_unreadable(new, exc)
            return False
        with src:
            self.put_stream(info, src, size, False)
        return True


# --- the rewrite -------------------------------------------------------------


def rewrite_archive(
    src_path: str,
    dst_path: str,
    plan: RewritePlan,
    *,
    dispose_original: Optional[Callable[[str], None]] = None,
    temp_suffix: str = ".tmp_write",
    error_prefix: str = "Could not rewrite the archive",
    progress: Optional[Callable[[int, int], None]] = None,
    byte_progress: Optional[Callable[[int, int], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    namer: Callable[[str], str] = backup_name,
    keep_new_if_dispose_fails: bool = False,
) -> RewriteResult:
    """Writes `src_path` rewritten as `plan` says to `dst_path` (the same
    path for an in-place rewrite) and returns what was done.

    The output is built at `dst_path + temp_suffix`, checked (entry count,
    CRCs unless the plan turns that off, plus whatever the plan asks for),
    and only then put in place by swap_in() (see there for
    `dispose_original`, `namer` and `keep_new_if_dispose_fails`).
    `progress(done, total)` follows each source entry, `byte_progress(done,
    total)` the bytes; `should_cancel()` is polled before each entry and
    between chunks (it may also raise its own exception).

    Raises RewriteError (message starting with `error_prefix`) for any
    zip-level failure, RewriteCancelled on cancel; any other exception
    propagates -- the temp file is removed in every case."""
    tmp_path = dst_path + temp_suffix
    try:
        result = _write_temp(src_path, tmp_path, plan, progress, byte_progress, should_cancel)
        _check_written(tmp_path, plan, result)
        swap_in(tmp_path, dst_path, dispose_original, namer=namer, keep_new_if_dispose_fails=keep_new_if_dispose_fails)
    except BaseException as exc:
        _discard_temp(tmp_path)
        if isinstance(exc, ZIP_ERRORS) and not isinstance(exc, _OriginalKeptAside):
            detail = describe_save_error(exc)
            raise RewriteError(f"{error_prefix}: {detail}", detail) from exc
        raise
    return result


def _write_temp(src_path, tmp_path, plan: RewritePlan, progress, byte_progress, should_cancel) -> RewriteResult:
    result = RewriteResult()
    with open_zip(src_path) as src, zipfile.ZipFile(tmp_path, "w", allowZip64=True) as dst:
        dst.comment = src.comment if plan.comment is None else plan.comment
        entries = [Entry(info.filename, info) for info in src.infolist()]
        if plan.begin is not None:
            plan.begin(entries)
        if plan.order is not None:
            entries = plan.order(entries)
        total = sum(e.info.file_size for e in entries) + sum(n.size() for n in [*plan.before, *plan.after])
        counter = _Counter(total, byte_progress, should_cancel)
        writer = _Writer(dst, result, counter)
        for new in plan.before:
            writer.put_new(new)
            result.added.append(new.name)

        window = max(1, plan.window)
        done = 0
        # A sliding window: up to `window` entries are decided ahead of the one being
        # written, and a new one is decided as soon as one is written, so the caller's
        # workers never sit idle waiting for a whole window to drain.
        pending: deque = deque()
        upcoming = iter(entries)
        while True:
            counter.check()
            while len(pending) < window:
                entry = next(upcoming, None)
                if entry is None:
                    break
                reader = _Reader(src, entry.info)
                pending.append((entry, reader, plan.decide(entry, reader) if plan.decide else None))
            if not pending:
                break
            entry, reader, decision = pending.popleft()
            action = decision() if callable(decision) else decision
            action = action or Action()
            done += 1
            if action.drop:
                result.dropped.append(entry.name)
            else:
                new_name = action.rename or entry.name
                info = _make_info(new_name, entry.info, action.date_time)
                if action.compress_type is not None:
                    info.compress_type = action.compress_type
                same_name = new_name == entry.name
                if action.data is not None:
                    writer.put(info, action.data, same_name)
                elif reader.peek() is not None or entry.is_dir:
                    writer.put(info, reader(), same_name)
                else:  # nobody looked at the bytes: stream them
                    with src.open(entry.info) as stream:
                        writer.put_stream(info, stream, entry.info.file_size, same_name)
                    reader.size_streamed = entry.info.file_size
                renamed = not same_name
                replaced = action.data is not None and action.data != reader.peek()
                if renamed:
                    result.renamed[entry.name] = new_name
                if replaced:
                    result.replaced.append(entry.name)
                if not renamed and not replaced:
                    result.kept += 1
            result.bytes_in += reader.size_read
            if progress is not None:
                progress(done, len(entries))

        for new in plan.after:
            writer.put_new(new)
            result.added.append(new.name)
    return result


class _Reader:
    """Reads one source entry's bytes at most once."""

    def __init__(self, src: zipfile.ZipFile, info: zipfile.ZipInfo):
        self._src, self._info = src, info
        self._data: Optional[bytes] = None
        self.size_streamed = 0

    def __call__(self) -> bytes:
        if self._data is None:
            self._data = self._src.read(self._info)
        return self._data

    def peek(self) -> Optional[bytes]:
        return self._data

    @property
    def size_read(self) -> int:
        return len(self._data) if self._data is not None else self.size_streamed


def _check_written(tmp_path: str, plan: RewritePlan, result: RewriteResult) -> None:
    """The temp archive reads back with the entries that were written (and,
    when the plan asks, intact CRCs and its own checks)."""
    with zipfile.ZipFile(tmp_path, "r") as zf:
        if len(zf.infolist()) != result.entries_written:
            raise RewriteError(
                f"The rewritten archive has {len(zf.infolist())} entries, expected {result.entries_written}"
            )
        if plan.verify_crc:
            bad = zf.testzip()
            if bad is not None:
                raise RewriteError(f"The rewritten archive failed its checksum at {bad!r}")
        if plan.verify is not None:
            problem = plan.verify(zf)
            if problem:
                raise RewriteError(f"The rewritten archive failed verification: {problem}")


# --- a new archive -----------------------------------------------------------


def write_new_archive(
    path: str,
    entries: Iterable[NewEntry],
    *,
    comment: bytes = b"",
    total_bytes: int = 0,
    byte_progress: Optional[Callable[[int, int], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    on_unreadable: Optional[Callable[[NewEntry, OSError], None]] = None,
    verify_crc: bool = False,
) -> RewriteResult:
    """Writes a new zip at `path` from `entries` (consumed lazily, so a caller
    may produce them as slow work finishes). The caller picks the path --
    normally a temp name it moves into place afterwards; a partly written
    file is removed on any failure. `on_unreadable(entry, error)` takes a
    file entry whose source cannot be opened (it is left out and listed in
    `skipped`); without it the error propagates. `total_bytes` is the
    progress total (0: unknown, the bytes written so far)."""
    result = RewriteResult()
    counter = _Counter(total_bytes, byte_progress, should_cancel)

    def skip(new: NewEntry, exc: OSError) -> None:
        result.skipped.append((new.source_path, exc.strerror or str(exc)))
        on_unreadable(new, exc)

    try:
        with zipfile.ZipFile(path, "w", allowZip64=True) as dst:
            dst.comment = comment
            writer = _Writer(dst, result, counter)
            for new in entries:
                counter.check()
                if writer.put_new(new, skip if on_unreadable is not None else None):
                    result.added.append(new.name)
        if verify_crc:
            with zipfile.ZipFile(path) as zf:
                bad = zf.testzip()
            if bad is not None:
                raise RewriteError(f"The new archive failed its checksum at {bad!r}")
    except BaseException:
        _discard_temp(path)
        raise
    return result


def copy_file(src: str, dst: str, should_cancel: Optional[Callable[[], bool]] = None) -> None:
    """Copies `src` to `dst` in chunks with its timestamps and mode, polling
    `should_cancel` between chunks (a big audio file is a long copy)."""
    counter = _Counter(0, None, should_cancel)
    with retry_on_lock(open, src, "rb", **_LOCK_RETRY) as fin, open(dst, "wb") as fout:
        while True:
            counter.check()
            block = fin.read(CHUNK)
            if not block:
                break
            fout.write(block)
    shutil.copystat(src, dst)

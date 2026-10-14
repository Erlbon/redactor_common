"""
redactor_common/core/journal.py

The operations journal: an append-only record of every change an app makes to a file's TAGS, so an unwanted
Save or Redact can be undone after the app was closed (the in-memory Undo only lives for one session, and a
plain Save keeps no copy of the old file).

What it records, per file and change: the time, the path, what kind of change it was (save, redact, restore),
and for each tag field that changed its OLD and NEW value as text. It deliberately does NOT record covers or
other binary data (a changed cover is noted as such, not stored: whole images would make the journal huge) and
it is not a backup of the file: replaced or converted files are covered by the Recycle Bin / quarantine.

File format: one JSON object per line, UTF-8, each line flushed and fsynced as it is written:
    {"t": "op", "id": ..., "ts": ..., "path": ..., "kind": ..., "changes": {field: [old, new]}, ...}
    {"t": "done", "id": ..., "ok": true|false, "err": ""}        written after the file operation finished
    {"t": "restored", "id": <the op restored>, "by": <the restore op>}
The "op" line is written BEFORE the file is touched and the "done" line after, so a crash in between leaves a
"pending" entry: the History dialog shows it, and the user knows that save may or may not have happened.

Retention: the newest `max_lines` operations, none older than `max_days` (defaults 50,000 / 90 days), pruned
when the journal is opened and every PRUNE_EVERY appends; a prune rewrites the file through a temp file.

Thread-safe (Redact runs files on a worker thread). Qt-free.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping

from redactor_common.core.os_utils import replace_with_retry

KIND_SAVE = "save"
KIND_REDACT = "redact"
KIND_RESTORE = "restore"
RESTORABLE_KINDS = frozenset({KIND_SAVE, KIND_REDACT, KIND_RESTORE})

STATUS_PENDING = "pending"  # the operation was announced but never reported finished (a crash, a kill)
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_RESTORED = "restored"

DEFAULT_MAX_LINES = 50_000
DEFAULT_MAX_DAYS = 90
PRUNE_EVERY = 2_000
MAX_VALUE_CHARS = 20_000  # a longer value is cut and its field marked truncated: such a field is never restored
SECONDS_PER_DAY = 86_400


@dataclass
class Entry:
    id: str
    ts: float
    path: str
    kind: str
    changes: dict[str, tuple[str, str]] = field(default_factory=dict)  # field -> (old, new)
    cover_changed: bool = False
    truncated: list[str] = field(default_factory=list)
    note: str = ""
    status: str = STATUS_PENDING
    error: str = ""
    restored_by: str = ""

    @property
    def restorable(self) -> bool:
        """Has at least one old value that can be written back, and was not already restored."""
        return (
            self.kind in RESTORABLE_KINDS and self.status in (STATUS_OK, STATUS_PENDING)
            and any(name not in self.truncated for name in self.changes)
        )


def diff_fields(old: Mapping[str, object], new: Mapping[str, object]) -> dict[str, tuple[str, str]]:
    """{field: (old text, new text)} for every field whose text differs (None counts as empty, surrounding
    blanks are ignored). Only fields present in `new` are compared."""
    changes: dict[str, tuple[str, str]] = {}
    for name, value in new.items():
        before = _text(old.get(name))
        after = _text(value)
        if before != after:
            changes[name] = (before, after)
    return changes


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


class Journal:
    def __init__(
        self,
        path: str,
        *,
        max_lines: int = DEFAULT_MAX_LINES,
        max_days: float = DEFAULT_MAX_DAYS,
        clock: Callable[[], float] = time.time,
    ):
        self.path = path
        self.max_lines = max_lines
        self.max_days = max_days
        self._clock = clock
        self._lock = threading.RLock()
        self._appends = 0
        self._pruned_once = False

    # -- writing ----------------------------------------------------------------------------------

    def begin(
        self,
        path: str,
        kind: str,
        changes: Mapping[str, tuple[object, object]],
        *,
        cover_changed: bool = False,
        note: str = "",
    ) -> str | None:
        """Announces a change to `path` (call BEFORE writing the file). `changes` is {field: (old, new)}. Returns
        the entry id to hand to finish(), or None when there is nothing worth recording."""
        if not changes and not cover_changed:
            return None
        stored: dict[str, list[str]] = {}
        truncated: list[str] = []
        for name, (old, new) in changes.items():
            old_text, new_text = _text(old), _text(new)
            if len(old_text) > MAX_VALUE_CHARS or len(new_text) > MAX_VALUE_CHARS:
                truncated.append(name)
                old_text, new_text = old_text[:MAX_VALUE_CHARS], new_text[:MAX_VALUE_CHARS]
            stored[name] = [old_text, new_text]
        entry_id = uuid.uuid4().hex[:12]
        line = {
            "t": "op", "id": entry_id, "ts": round(self._clock(), 3), "path": os.fspath(path), "kind": kind,
            "changes": stored,
        }
        if cover_changed:
            line["cover"] = True
        if truncated:
            line["truncated"] = truncated
        if note:
            line["note"] = note
        with self._lock:
            self._maybe_prune()
            self._append(line)
        return entry_id

    def finish(self, entry_id: str | None, ok: bool = True, error: str = "") -> None:
        """Reports the operation announced by begin() as finished (ok) or failed. A None id is ignored."""
        if entry_id is None:
            return
        with self._lock:
            self._append({"t": "done", "id": entry_id, "ok": bool(ok), "err": error})

    def record(self, path: str, kind: str, changes: Mapping[str, tuple[object, object]], **kwargs) -> str | None:
        """begin() + finish() for a change already made."""
        entry_id = self.begin(path, kind, changes, **kwargs)
        self.finish(entry_id)
        return entry_id

    def mark_restored(self, entry_id: str, restore_id: str | None) -> None:
        with self._lock:
            self._append({"t": "restored", "id": entry_id, "by": restore_id or ""})

    def _append(self, line: dict) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._appends += 1

    # -- reading ----------------------------------------------------------------------------------

    def entries(self, *, limit: int | None = None, path: str | None = None, text: str = "") -> list[Entry]:
        """Operations newest first, each with its status merged in from the done / restored lines. `path` keeps
        one file's entries; `text` keeps entries whose path or a field name contains it (case-insensitive)."""
        with self._lock:
            entries = self._read()
        wanted = (text or "").casefold()
        result = []
        for entry in reversed(entries):
            if path is not None and os.path.normcase(entry.path) != os.path.normcase(os.fspath(path)):
                continue
            if wanted and wanted not in entry.path.casefold() and not any(wanted in n.casefold() for n in entry.changes):
                continue
            result.append(entry)
            if limit is not None and len(result) >= limit:
                break
        return result

    def _read(self) -> list[Entry]:
        if not os.path.isfile(self.path):
            return []
        by_id: dict[str, Entry] = {}
        order: list[Entry] = []
        with open(self.path, encoding="utf-8", newline="") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    line = json.loads(raw)
                except ValueError:
                    continue  # a half-written last line after a crash: skip it, keep the rest
                if not isinstance(line, dict):
                    continue
                kind = line.get("t")
                if kind == "op":
                    entry = Entry(
                        id=str(line.get("id", "")), ts=float(line.get("ts", 0)), path=str(line.get("path", "")),
                        kind=str(line.get("kind", "")),
                        changes={k: (str(v[0]), str(v[1])) for k, v in (line.get("changes") or {}).items()},
                        cover_changed=bool(line.get("cover")), truncated=list(line.get("truncated") or []),
                        note=str(line.get("note", "")),
                    )
                    by_id[entry.id] = entry
                    order.append(entry)
                elif kind == "done" and line.get("id") in by_id:
                    target = by_id[line["id"]]
                    target.status = STATUS_OK if line.get("ok") else STATUS_FAILED
                    target.error = str(line.get("err", ""))
                elif kind == "restored" and line.get("id") in by_id:
                    target = by_id[line["id"]]
                    target.status = STATUS_RESTORED
                    target.restored_by = str(line.get("by", ""))
        return order

    # -- restoring --------------------------------------------------------------------------------

    @staticmethod
    def restore_values(entry: Entry) -> tuple[dict[str, str], list[str]]:
        """({field: old value} that can be written back, [fields that cannot: their values were too long to be
        stored whole])."""
        values = {name: old for name, (old, _new) in entry.changes.items() if name not in entry.truncated}
        skipped = [name for name in entry.changes if name in entry.truncated]
        return values, skipped

    @staticmethod
    def reversed_changes(entry: Entry, names: Iterable[str]) -> dict[str, tuple[str, str]]:
        """The changes a restore of `names` makes: what the journal knew as new becomes old, and the other way."""
        return {name: (entry.changes[name][1], entry.changes[name][0]) for name in names if name in entry.changes}

    # -- retention --------------------------------------------------------------------------------

    def _maybe_prune(self) -> None:
        if not self._pruned_once or self._appends >= PRUNE_EVERY:
            self._pruned_once = True
            self._appends = 0
            self.prune()

    def prune(self) -> int:
        """Drops operations older than max_days and all but the newest max_lines (with their done / restored
        lines). Returns how many operations were dropped. The file is rewritten through a temp file."""
        with self._lock:
            entries = self._read()
            if not entries:
                return 0
            cutoff = self._clock() - self.max_days * SECONDS_PER_DAY
            keep = [e for e in entries if e.ts >= cutoff][-self.max_lines:]
            dropped = len(entries) - len(keep)
            if dropped <= 0:
                return 0
            keep_ids = {e.id for e in keep}
            tmp = self.path + ".tmp"
            with open(self.path, encoding="utf-8", newline="") as src, open(tmp, "w", encoding="utf-8", newline="\n") as out:
                for raw in src:
                    try:
                        line = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(line, dict) and line.get("id") in keep_ids:
                        out.write(raw if raw.endswith("\n") else raw + "\n")
                out.flush()
                os.fsync(out.fileno())
            replace_with_retry(tmp, self.path)
            return dropped


def default_journal_path(app_slug: str, folder: str) -> str:
    """`<folder>/<app_slug>_journal.jsonl`: next to the app's settings."""
    return os.path.join(folder, f"{app_slug}_journal.jsonl")

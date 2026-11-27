"""
redactor_common/core/rename_log.py

A persistent log of file renames, so the last one can be undone --
renames were the one change no Redactor app could take back (the in-app
Undo covers metadata edits only).

Every rename action (Rename/Export by Pattern in rename mode, a
filename Search/Replace, a single-file rename) records ONE batch: a
label, the time, and each (old path, new path). The log is a small JSON
file next to the app's settings, keeping the newest MAX_BATCHES.

undo_last() renames the newest batch back, newest rename first, and
never overwrites anything: a file that isn't at its new path any more,
or whose old name is taken again, is skipped and reported. The batch
leaves the log once undone (the skipped files with it -- they can't be
undone later either, and would only block the next undo).

"Move into folders" batches (core/move_plan.py) also remember the folders
they created (`created_dirs`, so undo can offer to tidy them away), the
library `root`, and which moves were cross-volume copies whose original
went to the Recycle Bin (`trashed`): those can't be moved back by a
rename, so undo leaves the file at its new path and says how to get the
original back. Undo also re-creates a source folder that has been
removed since (the tidy-up after a move), so the file has a place to
return to.

Pure logic, no Qt: see gui/rename_undo.py for the menu action's dialog.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from redactor_common.core.os_utils import rename_no_clobber, replace_with_retry, retry_on_lock

MAX_BATCHES = 50


class RenameLogUnreadable(OSError):
    """The log file exists but could not be read: it must not be written over."""


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


@dataclass
class RenameBatch:
    label: str
    when: float  # time.time()
    renames: list[tuple[str, str]]  # (old path, new path), in the order they happened
    created_dirs: list[str] = field(default_factory=list)  # folders a move batch made, outermost first
    trashed: list[tuple[str, str]] = field(default_factory=list)  # (old, new) moves whose original is in the Recycle Bin
    root: str = ""  # library root of a move batch
    batch_id: str = ""  # set on a batch saved while it grows (RenameLog.open_batch), so each save replaces it

    def describe(self) -> str:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.when))
        return f"{self.label} ({len(self.renames)} file(s), {stamp})"


@dataclass
class UndoResult:
    restored: list[tuple[str, str]] = field(default_factory=list)  # (new path it had, old path it's back at)
    problems: list[str] = field(default_factory=list)
    created_dirs: list[str] = field(default_factory=list)  # the undone batch's folders that still exist, deepest first (offer to prune)
    root: str = ""


class RenameLog:
    def __init__(
        self, path: str, max_batches: int = MAX_BATCHES,
        on_move: Callable[[list[tuple[str, str]]], None] | None = None,
    ):
        self.path = path
        self.max_batches = max_batches
        # Called with the (old, new) pairs after every recorded rename/move batch and after an undo (new, old):
        # the apps hand it the operations journal's record_moves so its entries follow the renamed file.
        # A failing hook never breaks a rename.
        self.on_move = on_move

    def _notify(self, pairs: list[tuple[str, str]]) -> None:
        if self.on_move is not None and pairs:
            try:
                self.on_move(list(pairs))
            except Exception:  # noqa: BLE001 -- bookkeeping must not break the rename that already happened
                pass

    def _read(self) -> list[RenameBatch]:
        """The batches in the log; [] when there is no log yet. Raises RenameLogUnreadable when the file exists but
        cannot be read (still locked after retries): the caller must then not write over it (2026-11-27, review
        card 8: a brief lock used to read as "empty" and the next record() wiped up to 49 older undo batches). A
        file that is not valid JSON is set aside as "<log>.damaged" and counts as empty."""
        try:
            text = retry_on_lock(_read_text, self.path, attempts=6, delay=0.1)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise RenameLogUnreadable(f"the undo log {self.path} could not be read ({exc})") from exc
        except ValueError:  # not UTF-8 text: damaged, set aside below
            text = ""
        try:
            raw = json.loads(text)
        except ValueError:
            try:
                os.replace(self.path, self.path + ".damaged")
            except OSError:
                raise RenameLogUnreadable(f"the undo log {self.path} is damaged and could not be set aside") from None
            return []
        batches = []
        for entry in raw if isinstance(raw, list) else []:
            try:
                batches.append(RenameBatch(
                    label=str(entry["label"]), when=float(entry["when"]),
                    renames=[(str(old), str(new)) for old, new in entry["renames"]],
                    created_dirs=[str(d) for d in entry.get("created_dirs", [])],
                    trashed=[(str(old), str(new)) for old, new in entry.get("trashed", [])],
                    root=str(entry.get("root", "")),
                    batch_id=str(entry.get("id", "")),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return batches

    def _write(self, batches: list[RenameBatch]) -> bool:
        """Writes the log; False when it could not be written (the rename itself already happened)."""
        data = []
        for b in batches[-self.max_batches:]:
            entry: dict = {"label": b.label, "when": b.when, "renames": [list(pair) for pair in b.renames]}
            if b.batch_id:
                entry["id"] = b.batch_id
            if b.created_dirs:
                entry["created_dirs"] = b.created_dirs
            if b.trashed:
                entry["trashed"] = [list(pair) for pair in b.trashed]
            if b.root:
                entry["root"] = b.root
            data.append(entry)
        folder = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(folder, exist_ok=True)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=1)
            # Windows: a virus scanner or indexer can briefly hold the file
            replace_with_retry(tmp, self.path, attempts=5, delay=0.05)
            return True
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False

    def record(
        self, label: str, renames: list[tuple[str, str]],
        created_dirs: list[str] | None = None, trashed: list[tuple[str, str]] | None = None, root: str = "",
    ) -> bool:
        """One rename action. Pairs whose old and new path are identical
        (nothing renamed; a case-only change still counts) are left out; an empty batch isn't recorded.
        `created_dirs`, `trashed`, `root`: see the module docstring (move batches).

        Returns False when the batch could not be saved (the log is locked or the disk refused the write), so the
        caller can tell the user the renames cannot be undone; True otherwise (also when there was nothing to
        record). The existing log is never overwritten when it could not be read."""
        pairs = [(str(old), str(new)) for old, new in renames if str(old) != str(new)]
        if not pairs:
            return True
        try:
            batches = self._read()
        except RenameLogUnreadable:
            return False
        batches.append(RenameBatch(
            label=label, when=time.time(), renames=pairs,
            created_dirs=[str(d) for d in created_dirs or []],
            trashed=[(str(old), str(new)) for old, new in trashed or []], root=root,
        ))
        saved = self._write(batches)
        self._notify(pairs)
        return saved

    def open_batch(self, label: str, root: str = "", flush_every: float = 1.0) -> "BatchRecorder":
        """A batch that is saved while it grows, for a long move run (2026-11-27, review card 7: the moves used to
        be logged once at the end, so a crash or a kill mid-run left every file already moved with no undo
        record). add() each move as it happens; the batch is written at most every `flush_every` seconds and on
        close(). It is Qt-free and may be fed from a worker thread; `on_move` is called only by close(), on the
        caller's thread."""
        return BatchRecorder(self, label, root, flush_every)

    def _save_batch(self, batch: RenameBatch) -> bool:
        """Adds `batch` to the log, or replaces the entry with the same batch_id (a growing batch)."""
        try:
            batches = self._read()
        except RenameLogUnreadable:
            return False
        for index, existing in enumerate(batches):
            if existing.batch_id and existing.batch_id == batch.batch_id:
                batches[index] = batch
                break
        else:
            batches.append(batch)
        return self._write(batches)

    def last_batch(self) -> Optional[RenameBatch]:
        batches = self.batches()
        return batches[-1] if batches else None

    def batches(self) -> list[RenameBatch]:
        try:
            return self._read()
        except RenameLogUnreadable:
            return []

    def undo_last(self) -> Optional[UndoResult]:
        """Renames the newest batch back (see the module docstring);
        None when there's nothing to undo. A log that cannot be read gives a result whose problem says so."""
        try:
            batches = self._read()
        except RenameLogUnreadable as exc:
            return UndoResult(problems=[f"{exc}; try again in a moment"])
        if not batches:
            return None
        batch = batches.pop()
        result = UndoResult(root=batch.root)
        trashed = {(_key(old), _key(new)) for old, new in batch.trashed}
        for old, new in reversed(batch.renames):
            name = os.path.basename(new)
            if (_key(old), _key(new)) in trashed:
                result.problems.append(
                    f"{name}: was copied across drives and its original is in the Recycle Bin, so it "
                    f"can't be moved back -- it stays at {new}; restore the original from the Recycle "
                    "Bin and delete the copy to undo this one"
                )
                continue
            if not os.path.exists(new):
                result.problems.append(f"{name}: no longer there (moved or deleted since)")
                continue
            if os.path.exists(old) and os.path.normcase(os.path.abspath(old)) != os.path.normcase(os.path.abspath(new)):
                result.problems.append(f"{name}: its old name {os.path.basename(old)} is taken again")
                continue
            try:
                old_dir = os.path.dirname(old)
                if old_dir and not os.path.isdir(old_dir):
                    os.makedirs(old_dir, exist_ok=True)  # the source folder was tidied away after the move
                if os.path.normcase(os.path.abspath(old)) == os.path.normcase(os.path.abspath(new)):
                    os.rename(new, old)  # case-only rename: the same file, not a collision
                else:
                    rename_no_clobber(new, old)
            except OSError as exc:
                result.problems.append(f"{name}: {exc}")
                continue
            result.restored.append((new, old))
        if not self._write(batches):
            result.problems.append(f"the undo log {self.path} could not be updated, so this batch is still listed")
        self._notify([(new, old) for new, old in result.restored])
        result.created_dirs = [d for d in reversed(batch.created_dirs) if os.path.isdir(d)]
        return result


class BatchRecorder:
    """One growing batch (RenameLog.open_batch). add() is cheap; the log file is rewritten at most every
    `flush_every` seconds, and by close(). `saved` is False once a save failed (the log locked or the disk full)."""

    def __init__(self, log: RenameLog, label: str, root: str, flush_every: float):
        import threading
        import uuid

        self._log = log
        self._lock = threading.Lock()
        self._batch = RenameBatch(label=label, when=time.time(), renames=[], root=root, batch_id=uuid.uuid4().hex)
        self._flush_every = flush_every
        self._last_flush = 0.0
        self._dirty = False
        self.saved = True

    def add(self, old: str, new: str, trashed: bool = False, created_dirs: list[str] | None = None) -> None:
        if str(old) == str(new):
            return
        with self._lock:
            self._batch.renames.append((str(old), str(new)))
            if trashed:
                self._batch.trashed.append((str(old), str(new)))
            for folder in created_dirs or []:
                if str(folder) not in self._batch.created_dirs:
                    self._batch.created_dirs.append(str(folder))
            self._dirty = True
            if time.monotonic() - self._last_flush >= self._flush_every:
                self._flush()

    def _flush(self) -> None:
        if not self._dirty or not self._batch.renames:
            return
        self._last_flush = time.monotonic()
        self._dirty = False
        # each save writes the whole batch, so the last save decides whether the log holds all of it
        self.saved = self._log._save_batch(self._batch)
        if not self.saved:
            self._dirty = True  # try again at the next add() or at close()

    def close(self) -> bool:
        """Saves what is left and calls the log's on_move hook with every pair; returns `saved`."""
        with self._lock:
            self._flush()
            pairs = list(self._batch.renames)
        self._log._notify(pairs)
        return self.saved

    @property
    def renames(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._batch.renames)


def _key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def rename_log_path(app_slug: str, dev_root) -> str:
    """"<app>_rename_log.json" next to the app's settings file."""
    from redactor_common.core.app_paths import base_dir

    return os.path.join(str(base_dir(dev_root)), f"{app_slug}_rename_log.json")

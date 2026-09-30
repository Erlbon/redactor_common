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
from typing import Optional

from redactor_common.core.os_utils import rename_no_clobber, replace_with_retry

MAX_BATCHES = 50


@dataclass
class RenameBatch:
    label: str
    when: float  # time.time()
    renames: list[tuple[str, str]]  # (old path, new path), in the order they happened
    created_dirs: list[str] = field(default_factory=list)  # folders a move batch made, outermost first
    trashed: list[tuple[str, str]] = field(default_factory=list)  # (old, new) moves whose original is in the Recycle Bin
    root: str = ""  # library root of a move batch

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
    def __init__(self, path: str, max_batches: int = MAX_BATCHES):
        self.path = path
        self.max_batches = max_batches

    def _read(self) -> list[RenameBatch]:
        try:
            with open(self.path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
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
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return batches

    def _write(self, batches: list[RenameBatch]) -> None:
        data = []
        for b in batches[-self.max_batches:]:
            entry: dict = {"label": b.label, "when": b.when, "renames": [list(pair) for pair in b.renames]}
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
        except OSError:
            # best effort, like the apps' other settings: the rename itself already happened
            try:
                os.remove(tmp)
            except OSError:
                pass

    def record(
        self, label: str, renames: list[tuple[str, str]],
        created_dirs: list[str] | None = None, trashed: list[tuple[str, str]] | None = None, root: str = "",
    ) -> None:
        """One rename action. Pairs whose old and new path are identical
        (nothing renamed; a case-only change still counts) are left out; an empty batch isn't recorded.
        `created_dirs`, `trashed`, `root`: see the module docstring (move batches)."""
        pairs = [(str(old), str(new)) for old, new in renames if str(old) != str(new)]
        if not pairs:
            return
        batches = self._read()
        batches.append(RenameBatch(
            label=label, when=time.time(), renames=pairs,
            created_dirs=[str(d) for d in created_dirs or []],
            trashed=[(str(old), str(new)) for old, new in trashed or []], root=root,
        ))
        self._write(batches)

    def last_batch(self) -> Optional[RenameBatch]:
        batches = self._read()
        return batches[-1] if batches else None

    def batches(self) -> list[RenameBatch]:
        return self._read()

    def undo_last(self) -> Optional[UndoResult]:
        """Renames the newest batch back (see the module docstring);
        None when there's nothing to undo."""
        batches = self._read()
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
        self._write(batches)
        result.created_dirs = [d for d in reversed(batch.created_dirs) if os.path.isdir(d)]
        return result


def _key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def rename_log_path(app_slug: str, dev_root) -> str:
    """"<app>_rename_log.json" next to the app's settings file."""
    from redactor_common.core.app_paths import base_dir

    return os.path.join(str(base_dir(dev_root)), f"{app_slug}_rename_log.json")

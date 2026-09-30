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

Pure logic, no Qt: see gui/rename_undo.py for the menu action's dialog.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from redactor_common.core.os_utils import rename_no_clobber

MAX_BATCHES = 50


@dataclass
class RenameBatch:
    label: str
    when: float  # time.time()
    renames: list[tuple[str, str]]  # (old path, new path), in the order they happened

    def describe(self) -> str:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.when))
        return f"{self.label} ({len(self.renames)} file(s), {stamp})"


@dataclass
class UndoResult:
    restored: list[tuple[str, str]] = field(default_factory=list)  # (new path it had, old path it's back at)
    problems: list[str] = field(default_factory=list)


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
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return batches

    def _write(self, batches: list[RenameBatch]) -> None:
        data = [{"label": b.label, "when": b.when, "renames": [list(pair) for pair in b.renames]}
                for b in batches[-self.max_batches:]]
        folder = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(folder, exist_ok=True)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass  # best effort, like the apps' other settings: the rename itself already happened

    def record(self, label: str, renames: list[tuple[str, str]]) -> None:
        """One rename action. Pairs whose old and new path are identical
        (nothing renamed; a case-only change still counts) are left out; an empty batch isn't recorded."""
        pairs = [(str(old), str(new)) for old, new in renames if str(old) != str(new)]
        if not pairs:
            return
        batches = self._read()
        batches.append(RenameBatch(label=label, when=time.time(), renames=pairs))
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
        result = UndoResult()
        for old, new in reversed(batch.renames):
            name = os.path.basename(new)
            if not os.path.exists(new):
                result.problems.append(f"{name}: no longer there (moved or deleted since)")
                continue
            if os.path.exists(old) and os.path.normcase(os.path.abspath(old)) != os.path.normcase(os.path.abspath(new)):
                result.problems.append(f"{name}: its old name {os.path.basename(old)} is taken again")
                continue
            try:
                if os.path.normcase(os.path.abspath(old)) == os.path.normcase(os.path.abspath(new)):
                    os.rename(new, old)  # case-only rename: the same file, not a collision
                else:
                    rename_no_clobber(new, old)
            except OSError as exc:
                result.problems.append(f"{name}: {exc}")
                continue
            result.restored.append((new, old))
        self._write(batches)
        return result


def rename_log_path(app_slug: str, dev_root) -> str:
    """"<app>_rename_log.json" next to the app's settings file."""
    from redactor_common.core.app_paths import base_dir

    return os.path.join(str(base_dir(dev_root)), f"{app_slug}_rename_log.json")

"""
redactor_common/core/notes_scope.py

The choices of the first question of Tools > Create Notes ("which folder?"), the same in every app that writes notes. Pure
logic, no Qt: gui/notes_scope_dialog.py shows what merge_choices() returns. The app builds the ordered candidates (the
folder you opened, the folder of the selected files, one level up, the folder used last time, the library root) because
what a folder IS (an artist, a series) is its own business; this module only merges them.

  - a folder that does not exist is left out
  - the same folder (compared case-insensitively, resolved) appears once, its captions joined with '; '
  - at most MAX_CHOICES, the ticked-by-default ones kept first
  - the first choice is ticked when none was asked to be
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

MAX_CHOICES = 6


@dataclass
class ScopeChoice:
    path: str
    caption: str
    default: bool = False


def folder_key(path: str) -> str:
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except OSError:
        return os.path.normcase(os.path.abspath(path))


def is_root(path: str) -> bool:
    p = Path(path)
    return p.parent == p


def merge_choices(candidates: Iterable[tuple[str, str, bool]], max_choices: int = MAX_CHOICES) -> list[ScopeChoice]:
    """The merged choices of ordered (path, caption, default) candidates."""
    found: list[ScopeChoice] = []
    index: dict[str, ScopeChoice] = {}
    for path, caption, default in candidates:
        if not path or not os.path.isdir(path):
            continue
        key = folder_key(path)
        if key in index:
            existing = index[key]
            if caption not in existing.caption:
                existing.caption += "; " + caption
            existing.default = existing.default or default
            continue
        choice = ScopeChoice(path, caption, default)
        index[key] = choice
        found.append(choice)
    if len(found) > max_choices:
        keep = [c for c in found if c.default]
        rest = [c for c in found if not c.default]
        allowed = {id(c) for c in rest[: max_choices - len(keep)]} | {id(c) for c in keep}
        found = [c for c in found if id(c) in allowed]
    if found and not any(c.default for c in found):
        found[0].default = True
    return found


def opened_folder(loaded: list[str], last_directory: str) -> tuple[str, int]:
    """(the folder the person opened, how many loaded files are in it; 0 when no list is loaded)."""
    parents = [str(Path(f).parent) for f in loaded]
    if last_directory and os.path.isdir(last_directory):
        if not parents:
            return last_directory, 0
        base = folder_key(last_directory)
        if all(folder_key(p) == base or folder_key(p).startswith(base.rstrip("\\/") + os.sep) for p in parents):
            return last_directory, len(loaded)
    if not parents:
        return "", 0
    try:
        common = os.path.commonpath(parents)
    except ValueError:  # several drives
        return "", 0
    return common, len(loaded)


def opened_caption(count: int) -> str:
    return "the folder you opened" if not count else (
        f"the folder you opened: all {count} loaded files are in it" if count > 1
        else "the folder you opened: the loaded file is in it")

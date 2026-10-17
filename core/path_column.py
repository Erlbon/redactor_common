"""
redactor_common/core/path_column.py

The table's "Path" column, ONE definition for all four Redactor apps (owner report 2026-10-17: the apps each did
something different: mp3 the full path as 2nd column, epub the folder as 1st column, video far right in the
technical group, cbz nothing).

The shared rules:

- key `PATH_KEY` ("path"), header `PATH_LABEL` ("Path");
- the cell shows the FOLDER the file is in (`folder_of`); the file name is already in the Filename column;
  the tooltip and Ctrl+C give the FULL file path (`path_tooltip`);
- default position: directly after the Filename column, visible by default, sortable (case-insensitive natural
  order, `natural_sort_key`), filterable (a text "contains" filter where the app has header filters), listed in the
  Columns chooser and in the saved column order like any other column;
- `migrate_saved_columns` is the one-time move for users whose saved order/hidden set predates the column;
- `lint_path_column` is the machine check an app calls from its own tests so a refactor cannot lose the column.

Qt-free on purpose; the item class and the Ctrl+C support are in gui/path_column.py.
"""

from __future__ import annotations

import os
import re
from typing import Iterable, Sequence

PATH_KEY = "path"
PATH_LABEL = "Path"
FILENAME_KEY = "filename"
# Name of the one-time migration flag every app stores (QSettings key) once it has moved the column.
SEEDED_SETTING = "path_column_seeded"


def folder_of(path) -> str:
    """The folder containing `path`, as text (empty for an empty path)."""
    text = os.fspath(path) if path is not None else ""
    if not text:
        return ""
    return os.path.dirname(text)


def path_tooltip(path) -> str:
    """The tooltip (and copy text) of a Path cell: the full file path."""
    return os.fspath(path) if path is not None else ""


_NUM = re.compile(r"(\d+)")


def natural_sort_key(text: str) -> tuple:
    """Case-insensitive natural order: 'Vol 2' sorts before 'Vol 10'."""
    parts = _NUM.split((text or "").casefold())
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in parts)


def insert_path_key(keys: Iterable[str], after: str = FILENAME_KEY) -> list[str]:
    """`keys` with PATH_KEY placed directly after `after` (moved there if present elsewhere; appended when `after`
    is not in the list)."""
    out = [k for k in keys if k != PATH_KEY]
    if after in out:
        out.insert(out.index(after) + 1, PATH_KEY)
    else:
        out.append(PATH_KEY)
    return out


def migrate_saved_columns(
    saved_order: Sequence[str], hidden: Iterable[str], after: str = FILENAME_KEY
) -> tuple[list[str], set[str]]:
    """The one-time migration: move PATH_KEY to just after `after` in the saved order (when there is one) and
    un-hide it. Returns (order, hidden). An empty saved order stays empty (the app's default order already has the
    column in place)."""
    order = list(saved_order)
    if order:
        order = insert_path_key(order, after)
    return order, {h for h in hidden if h != PATH_KEY}


def lint_path_column(keys: Sequence[str], after: str = FILENAME_KEY) -> list[str]:
    """Problems with an app's column key list (empty list = fine): 'path' must exist and sit directly after
    'filename'. Call from each app's tests with the real, default column order."""
    keys = list(keys)
    if PATH_KEY not in keys:
        return [f"column list has no '{PATH_KEY}' column (shared Path column, redactor_common.core.path_column)"]
    if after not in keys:
        return [f"column list has no '{after}' column to put '{PATH_KEY}' after"]
    problems: list[str] = []
    if keys.index(PATH_KEY) != keys.index(after) + 1:
        problems.append(f"'{PATH_KEY}' must come directly after '{after}', it is at position {keys.index(PATH_KEY)}")
    if keys.count(PATH_KEY) > 1:
        problems.append(f"'{PATH_KEY}' appears more than once")
    return problems

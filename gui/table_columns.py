"""
redactor_common/gui/table_columns.py

House rule (owner, 2026-10-15): EVERY table's column headers can be resized by dragging. Qt's Stretch,
ResizeToContents and Fixed header modes all take that away (a stretched or auto-sized column cannot be dragged),
and a dialog that opens with one huge column and the rest squeezed is the usual result. So:

    make_columns_resizable(table, wide_column=COL_NAME)      # after the table is filled (and again after a refill)

- every column becomes draggable (QHeaderView.Interactive); the LAST column also fills what is left of the width;
- the starting widths come from the contents, no column starting wider than a share of the viewport, and
  `wide_column` (the one that carries the long text) gets the room that is left over;
- it works for QTableWidget/QTableView (horizontalHeader()) and QTreeWidget/QTreeView (header()).

`lint_resizable_headers(folder)` is the machine check behind the rule: it scans a folder of .py sources for
`setSectionResizeMode(...)` with Stretch, ResizeToContents or Fixed, and for `resizeSections`/`setSectionsMovable`
free spots. Each app calls it from its own tests against its gui folder:

    def test_all_headers_are_resizable():
        assert lint_resizable_headers(Path(__file__).parent.parent / "gui") == []

A line that truly needs another mode (a one-column list, a row header) carries `# resizable-ok: <reason>`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import QHeaderView

MIN_SECTION_WIDTH = 36
MAX_INITIAL_SHARE = 0.55  # no column starts wider than this share of the viewport (except the wide one)
MIN_WIDE_COLUMN = 180


def _header(view) -> QHeaderView:
    return view.horizontalHeader() if hasattr(view, "horizontalHeader") else view.header()


def make_columns_resizable(view, wide_column: Optional[int] = None) -> None:
    """Make every column of `view` draggable and give it sensible starting widths. Call it after the view is filled
    (the starting widths come from the contents) and again after a refill."""
    header = _header(view)
    count = header.count()
    if count == 0:
        return
    header.setMinimumSectionSize(MIN_SECTION_WIDTH)
    for section in range(count):
        header.setSectionResizeMode(section, QHeaderView.ResizeMode.Interactive)
    header.setStretchLastSection(True)

    # starting widths from the contents, capped, then the wide column takes the room that is left
    for section in range(count):
        view.resizeColumnToContents(section)
    viewport = max(view.viewport().width(), 300)
    cap = int(viewport * MAX_INITIAL_SHARE)
    for section in range(count):
        if section != wide_column and header.sectionSize(section) > cap:
            header.resizeSection(section, cap)
    if wide_column is not None and 0 <= wide_column < count:
        others = sum(header.sectionSize(s) for s in range(count) if s != wide_column and not header.isSectionHidden(s))
        header.resizeSection(wide_column, max(MIN_WIDE_COLUMN, viewport - others))


_FIXED_MODE = re.compile(r"setSectionResizeMode\([^)]*\b(Stretch|ResizeToContents|Fixed)\b")
_SETMODE_ALL = re.compile(r"\bsetSectionResizeMode\(\s*QHeaderView\.ResizeMode\.(Stretch|ResizeToContents|Fixed)\s*\)")


def lint_resizable_headers(folder: Path | str) -> list[str]:
    """Problems found in the .py files under `folder` (one readable string per place): a header put into a Stretch,
    ResizeToContents or Fixed mode, which makes the column impossible to drag. Empty list = the rule holds."""
    problems: list[str] = []
    root = Path(folder)
    for path in sorted(root.rglob("*.py")):
        if any(part in {"tests", "__pycache__", "build", "dist"} for part in path.parts):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(lines, start=1):
            if "resizable-ok" in line:
                continue
            if _FIXED_MODE.search(line) or _SETMODE_ALL.search(line):
                problems.append(
                    f"{path.name}:{number}: header mode {_FIXED_MODE.search(line).group(1) if _FIXED_MODE.search(line) else 'Stretch/ResizeToContents/Fixed'} "
                    "makes the column undraggable: use make_columns_resizable() (or add '# resizable-ok: <reason>')"
                )
    return problems


# --- the sorting rule (owner, 2026-10-17: ALL column headers are sortable) ---------------------------------------------

_TABLE_CREATION = re.compile(r"\b(?:QTableWidget|QTableView)\(")
_SORT_ENABLED = re.compile(r"\benable_sorting\(|\benable_header_sort\(|\bsetSortingEnabled\(\s*True\s*\)")
_SORTABLE_NO = re.compile(r"#\s*sortable:\s*no\s*\(\s*\S")


def lint_sortable_headers(folder: Path | str) -> list[str]:
    """Problems found in the .py files under `folder` (one readable string per place): a module that creates a
    QTableWidget/QTableView but never turns sorting on (`enable_sorting(table)` from sortable_table, or
    `setSortingEnabled(True)`, or `enable_header_sort(table, callback)` for a table with cell widgets). A table that truly cannot sort (a log, say) carries `# sortable: no (<reason>)` on its
    creation line or the line above. Empty list = the rule holds."""
    problems: list[str] = []
    root = Path(folder)
    for path in sorted(root.rglob("*.py")):
        if any(part in {"tests", "__pycache__", "build", "dist"} for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines = text.splitlines()
        sorted_somewhere = any(_SORT_ENABLED.search(line) and not line.lstrip().startswith("#") for line in lines)
        for number, line in enumerate(lines, start=1):
            if line.lstrip().startswith(("#", "from ", "import ")) or not _TABLE_CREATION.search(line):
                continue
            if _SORTABLE_NO.search(line) or (number > 1 and _SORTABLE_NO.search(lines[number - 2])):
                continue
            if not sorted_somewhere:
                problems.append(
                    f"{path.name}:{number}: a table that never turns sorting on: call enable_sorting(table) from "
                    "redactor_common.gui.sortable_table (or add '# sortable: no (<reason>)')"
                )
    return problems

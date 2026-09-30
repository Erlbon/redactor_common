"""
redactor_common/gui/move_runner.py

Executes the planned moves of the Rename dialog's "Move into folders"
mode (core/move_plan.py) under a progress dialog, then reports what
happened and offers the tidy-up. The app hands over
`dialog.planned_moves()` and its RenameLog and gets back a summary it can
use to update its own items (old path -> new path).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable

from PyQt6.QtWidgets import QMessageBox, QWidget

from redactor_common.core.error_summary import summarize_errors
from redactor_common.core.move_plan import PlannedMove, execute_move, prune_empty_dirs
from redactor_common.core.rename_log import RenameLog
from redactor_common.core.trash import move_to_trash
from redactor_common.gui.progress import run_with_progress

TITLE = "Move into Folders"


@dataclass
class MoveRunSummary:
    done: list[tuple[Any, str, str]] = field(default_factory=list)  # (item, old_path, new_path) moved or copied
    failed: list[str] = field(default_factory=list)  # "name: reason"
    warnings: list[str] = field(default_factory=list)  # e.g. an original kept because trashing failed
    cancelled: bool = False
    created_dirs: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)


def run_planned_moves(
    parent: QWidget,
    planned: list[PlannedMove],
    copy: bool,
    rename_log: RenameLog | None,
    label: str,
    trash: Callable[[str], None] = move_to_trash,
    ask_prune: bool = True,
) -> MoveRunSummary:
    """Runs every non-blocking, non-no-op move, isolating errors per file,
    cancellable. Moves (not copies) are recorded in `rename_log` as one
    batch so Undo Last Rename restores them; a cross-volume move whose
    original went to the Recycle Bin is flagged as such in the batch, and
    one whose original couldn't be trashed is reported as a copy (both
    files exist, nothing to undo). Afterwards, once, asks whether to
    remove the now-empty source folders."""
    summary = MoveRunSummary()
    todo = [m for m in planned if not m.is_noop]
    for move in todo:
        if move.blocking:
            summary.failed.append(f"{os.path.basename(move.old_path)}: {move.warning}")
    todo = [m for m in todo if not m.blocking]
    pairs: list[tuple[str, str]] = []
    trashed: list[tuple[str, str]] = []

    def step(move: PlannedMove, _index: int) -> None:
        name = os.path.basename(move.old_path)
        try:
            result = execute_move(move.old_path, move.new_path, copy=copy, trash=trash)
        except Exception as exc:  # noqa: BLE001 -- one bad file must not stop the batch
            summary.failed.append(f"{name}: {exc}")
            return
        summary.created_dirs.extend(result.created_dirs)
        summary.done.append((move.item, move.old_path, result.new_path))
        if result.warning:
            summary.warnings.append(f"{name}: {result.warning}")
        if not copy and not result.original_kept:
            pairs.append((move.old_path, result.new_path))
            if result.original_trashed:
                trashed.append((move.old_path, result.new_path))

    finished = run_with_progress(
        parent, todo, step, label, cancellable=True,
        label_for=lambda m: f"{label}: {os.path.basename(m.old_path)}",
    )
    summary.cancelled = not finished
    root = planned[0].root if planned else ""
    if rename_log is not None and pairs:
        rename_log.record(label, pairs, created_dirs=summary.created_dirs, trashed=trashed, root=root)

    problems = list(summary.failed) + list(summary.warnings)
    if summary.cancelled:
        problems.insert(0, "Cancelled -- the remaining files were not processed.")
    if problems:
        QMessageBox.warning(
            parent, TITLE,
            f"{len(summary.done)} of {len(todo)} file(s) done.\n\n" + summarize_errors(problems, max_shown=6),
        )

    if ask_prune and not copy and pairs:
        sources = {os.path.dirname(old) for old, _new in pairs}
        empty = [d for d in sources if os.path.isdir(d) and not os.listdir(d)]
        if empty:
            answer = QMessageBox.question(
                parent, TITLE,
                f"{len(empty)} source folder(s) are now empty. Remove them (and any empty parent "
                "folders inside the library root)?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                summary.pruned = prune_empty_dirs(empty, stop_at_root=root or None)
    return summary

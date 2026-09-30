"""
redactor_common/gui/rename_undo.py

The "Undo Last Rename..." menu action every Redactor app offers
(core/rename_log.py): shows what the newest logged rename was, asks,
renames it back, and reports anything that couldn't be. The app passes
`on_restored(new_path, old_path)` to point its own in-memory items at
their old paths again (and refreshes its table afterwards).
"""

from __future__ import annotations

import os
from typing import Callable

from PyQt6.QtWidgets import QMessageBox, QWidget

from redactor_common.core.move_plan import prune_empty_dirs
from redactor_common.core.rename_log import RenameLog

TITLE = "Undo Last Rename"


def undo_last_rename(parent: QWidget, log: RenameLog, on_restored: Callable[[str, str], None]) -> int:
    """The whole action; returns how many files were renamed back."""
    batch = log.last_batch()
    if batch is None:
        QMessageBox.information(parent, TITLE, "There's no rename to undo.")
        return 0
    sample = "\n".join(
        f"{os.path.basename(new)}  →  {os.path.basename(old)}" for old, new in batch.renames[:5]
    )
    if len(batch.renames) > 5:
        sample += f"\n... and {len(batch.renames) - 5} more"
    answer = QMessageBox.question(
        parent, TITLE,
        f"Undo the last rename -- {batch.describe()}?\n\n{sample}",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes,
    )
    if answer != QMessageBox.StandardButton.Yes:
        return 0
    result = log.undo_last()
    for new, old in result.restored:
        on_restored(new, old)
    if result.problems:
        details = "\n".join(result.problems[:15])
        if len(result.problems) > 15:
            details += f"\n... and {len(result.problems) - 15} more"
        QMessageBox.warning(
            parent, TITLE,
            f"Renamed {len(result.restored)} of {len(batch.renames)} file(s) back. Not undone:\n\n{details}",
        )
    if result.created_dirs:
        tidy = QMessageBox.question(
            parent, TITLE,
            f"The move created {len(result.created_dirs)} folder(s). Remove those that are now empty?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes,
        )
        if tidy == QMessageBox.StandardButton.Yes:
            prune_empty_dirs(result.created_dirs, stop_at_root=result.root or None, climb=False)
    return len(result.restored)

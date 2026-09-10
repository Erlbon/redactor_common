"""
redactor_common/gui/rename_single_file.py

Quick, direct rename of a single file on disk -- for fixing a typo or
small mistake in a filename without going through the pattern-based
Rename/Export tool (rename_pattern_dialog.py). Wraps core/rename_pattern.py's
rename_file_on_disk() (already generic: a plain path string in, no
project-specific item type needed) with the QInputDialog prompt and
QMessageBox error reporting every consuming project was independently
re-writing -- epubredactor had its own local copy (still does, not yet
migrated -- see this repo's README "Still open" section), then
mp3redactor wrote a near-identical one from scratch. This repo's own
context_menu.py docstring already anticipated exactly this call
(`lambda: self.rename_single_file(books[0])`) without a shared
implementation actually existing yet -- this fills that in.

Deliberately a plain function, not a dialog class -- there's no state
to hold between calls, just "ask, validate, rename, report", the same
shape context_menu.py's show_table_context_menu() uses.

Usage (see mp3redactor's gui/main_window.py for a full call site):

    from redactor_common.gui.rename_single_file import rename_single_file

    def _on_cell_double_clicked(self, row, col):
        if col != self._col_index["filename"]:
            return
        mp3 = ...  # this project's own row->item lookup
        if mp3 is not None and not mp3.load_error:
            if rename_single_file(self, str(mp3.path), lambda p: setattr(mp3, "path", Path(p))):
                self._rebuild_table()
"""

from __future__ import annotations

import os
from typing import Callable

from PyQt6.QtWidgets import QInputDialog, QMessageBox, QWidget

from redactor_common.core.rename_pattern import rename_file_on_disk


def rename_single_file(
    parent: QWidget,
    current_path: str,
    set_path: Callable[[str], None],
    title: str = "Rename File",
) -> bool:
    """Prompts for a new filename (current stem pre-filled, extension
    kept automatically), renames the file at `current_path` on disk
    immediately via core.rename_pattern.rename_file_on_disk(), and
    calls `set_path(new_path)` on success -- the caller's own closure
    over whichever item object holds that path (see the module
    docstring's example), since that object's shape differs per
    project (a dataclass field, a plain attribute, ...).

    Returns True if a rename actually happened on disk, False otherwise
    (cancelled, typed the same name back, or failed). A failure already
    shows its own QMessageBox.warning before returning False, so
    callers don't need to handle that case themselves -- just act on a
    True return (e.g. refresh the row/table).

    A physical file operation -- callers should NOT push this onto
    their own undo stack, same "in-memory edits only" line every
    Redactor project already draws for Save/Fix Integrity/Rename-
    Export's own "rename in place" mode.
    """
    current_stem = os.path.splitext(os.path.basename(current_path))[0]
    new_stem, ok = QInputDialog.getText(
        parent, title,
        f'New filename for "{os.path.basename(current_path)}" '
        "(the file extension is kept automatically):",
        text=current_stem,
    )
    if not ok:
        return False
    new_stem = new_stem.strip()
    if new_stem == current_stem:
        return False

    try:
        new_path = rename_file_on_disk(current_path, new_stem)
    except (ValueError, FileExistsError, OSError) as exc:
        QMessageBox.warning(parent, "Could Not Rename", str(exc))
        return False

    set_path(new_path)
    return True

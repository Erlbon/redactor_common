"""
redactor_common/gui/owned_dialog.py

The base class of every dialog of the family.

A dialog is made with the main window as its parent, and a Qt widget with a parent is kept alive by it: letting go of the
Python name does not free it. Without more, every dialog that was opened stayed in memory, with all its fields, models and
connected lambdas, until the program closed (found 2026-10-24 by soak-testing mp3redactor, epubredactor and Dossier: a
Preferences dialog about 150 widgets and half a megabyte for every time it was opened). `OwnedDialog` deletes the dialog
once it has been closed, so an app left open for weeks does not grow with every dialog it opens.

How, and why NOT `WA_DeleteOnClose`: `QDialog.exec()` deletes a dialog that has that flag the moment it returns, before the
caller can read what the user typed (`dialog.values()` after `exec()` fails with "wrapped C/C++ object has been deleted";
the first version of this class did exactly that). So `exec()` here runs the dialog and then calls `deleteLater()`, which
waits until control is back in the event loop the caller itself runs in: what the caller reads right after `exec()`
returns is still there, and a nested loop the caller starts (a follow-up dialog) does not delete it early. A dialog that is
shown without `exec()` is deleted when it is closed. A dialog that is to be shown again must be made again, not kept in an
attribute and reshown. Calling `deleteLater()` a second time (a caller that already does) is harmless.
"""
from __future__ import annotations

from PyQt6.QtWidgets import QDialog


class OwnedDialog(QDialog):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._running = False
        self.finished.connect(self._closed)

    def exec(self) -> int:
        self._running = True
        try:
            return super().exec()
        finally:
            self._running = False
            self.deleteLater()      # after the caller has read the answer: at its own loop level, never in a nested one

    def _closed(self, _result: int) -> None:
        if not self._running:       # shown without exec(): nobody will call deleteLater for it
            self.deleteLater()

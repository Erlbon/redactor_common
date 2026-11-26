"""
redactor_common/gui/remove_or_delete.py

The Delete key in an app that can delete files. Pressing Delete on a list of
files most people expect the files to go (Explorer does that), while the
family's Delete key used to mean Remove from List only, which leaves the file
on disk (Erlend, 2026-11-26: "This is probably not the instinctive behaviour.
At the least, we should ask the user"). So in an app that has Delete Files,
the Delete key asks which of the two is meant:

    Remove from List   (the default button: harmless, the file stays)
    Delete Files       (to the Recycle Bin)
    Cancel

The menu items keep acting at once without the question, because their names
already say what they do: File > Remove from List (no shortcut in such an
app), File > Delete Files (Recycle Bin) (Shift+Delete). An app without Delete
Files keeps Delete = Remove from List; there is nothing to choose between.

Wiring, in the app's menu spec:

    file=standard_file_items(..., remove_from_list=self.remove_selected, delete_files=self.delete_files),
    hidden=[remove_or_delete_action(self.on_delete_key)],

    def on_delete_key(self):
        rows = self._selected_rows()
        if not rows:
            return
        choice = ask_remove_or_delete(self, len(rows), names=[...])
        if choice == REMOVE:
            self.remove_selected()
        elif choice == DELETE:
            self.delete_files(confirm=False)   # the question already asked
"""

from __future__ import annotations

from typing import Callable, Sequence

from PyQt6.QtWidgets import QMessageBox, QWidget

REMOVE = "remove"
DELETE = "delete"

PREVIEW_LIMIT = 10


def remove_or_delete_text(count: int, names: Sequence[str] = (), noun: str = "file") -> str:
    """The question's body text: what is selected (up to PREVIEW_LIMIT names) and what each choice does."""
    plural = noun if count == 1 else noun + "s"
    lines = [f"What should happen to the {count} selected {plural}?"]
    if names:
        shown = [f"- {n}" for n in list(names)[:PREVIEW_LIMIT]]
        if len(names) > PREVIEW_LIMIT:
            shown.append(f"...and {len(names) - PREVIEW_LIMIT} more")
        lines += ["", *shown]
    lines += [
        "",
        "Remove from List: takes them off this list. The files stay on disk.",
        "Delete Files: sends the files to the Recycle Bin (you can get them back from there).",
    ]
    return "\n".join(lines)


def ask_remove_or_delete(
    parent: QWidget | None,
    count: int,
    names: Sequence[str] = (),
    noun: str = "file",
    exec_box: Callable[[QMessageBox], object] | None = None,
) -> str | None:
    """Asks whether the selection is to be removed from the list or deleted (Recycle Bin). Returns REMOVE,
    DELETE, or None for Cancel / Esc. Remove from List is the default button, so Delete then Enter never
    deletes a file. `exec_box` replaces box.exec() in tests (it may click a button)."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle("Remove or Delete?")
    box.setText(remove_or_delete_text(count, names, noun))
    remove_btn = box.addButton("Remove from &List", QMessageBox.ButtonRole.AcceptRole)
    delete_btn = box.addButton("&Delete Files (Recycle Bin)", QMessageBox.ButtonRole.DestructiveRole)
    cancel_btn = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(remove_btn)
    box.setEscapeButton(cancel_btn)
    (exec_box or (lambda b: b.exec()))(box)
    clicked = box.clickedButton()
    if clicked is remove_btn:
        return REMOVE
    if clicked is delete_btn:
        return DELETE
    return None

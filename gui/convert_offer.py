"""
redactor_common/gui/convert_offer.py

The question Open Folder asks when the folder holds files the app does
not load but can convert: "No MP3 files found, but 12 audio file(s)
(FLAC, WAV) that can be converted to MP3. Convert them?" (or "Also
found ..." when some files did load). Added to all four apps on
2026-10-13 and promoted here 2026-10-14 from mp3redactor's _load_paths,
epubredactor's _offer_import_of_other_formats, videoredactor's
_offer_import_of_convertible and cbzredactor's
_prompt_convert_foreign_archives (whose extra buttons and checkboxes
the `extra_buttons` / `options` parameters cover).

The app keeps its own conversion; this module only finds and asks:

    found = find_convertible(self, [folder], IMPORTABLE_EXTENSIONS, recursive, skip=is_app_temp_name)
    answer = offer_conversion(self, found, loaded_count=len(loaded), kind_label="MP3", target_label="MP3",
                              noun="audio file", remember_key=folder, remember=self._declined)
    if answer == YES:
        self._convert_paths_and_load(found)

`remember` (optional) is a Remember pair of callables over wherever the
app keeps settings (remembered_folders() builds one over a stored
list): with it the box has "Don't ask again for this folder", and a
folder the user ticked that for is not asked about again (the call
returns NEVER without showing anything).
"""

from __future__ import annotations

import os
from typing import Callable, Iterable, NamedTuple, Sequence

from PyQt6.QtWidgets import QCheckBox, QMessageBox, QVBoxLayout, QWidget

from redactor_common.core.folder_discovery import extension_labels, find_by_extension
from redactor_common.gui.progress import run_busy

YES = "yes"
NO = "no"
NEVER = "never"

DETAILS_LIMIT = 200  # paths listed under Show Details; the rest is "... and N more"
DONT_ASK_TEXT = "Don't ask again for this folder"
DONT_ASK_MANY_TEXT = "Don't ask again for these folders"


class Remember(NamedTuple):
    """Where "Don't ask again for this folder" is kept: `is_declined(key)` and `decline(key)`, `key` being
    folder_key() of the folder."""

    is_declined: Callable[[str], bool]
    decline: Callable[[str], None]


def folder_key(folder: str | os.PathLike) -> str:
    """The form a folder is remembered under: absolute, normalised, case-folded where the platform is."""
    return os.path.normcase(os.path.abspath(os.fspath(folder)))


def remembered_folders(load: Callable[[], Iterable[str]], save: Callable[[list[str]], None], limit: int = 500) -> Remember:
    """A Remember over a list of folder keys the app stores itself (QSettings, an ini, JSON...). `load()`
    returns the stored list (anything iterable; None-safe), `save(keys)` stores the new one. The newest
    `limit` folders are kept, so the list cannot grow without end."""

    def stored() -> list[str]:
        return [str(k) for k in (load() or []) if str(k)]

    def is_declined(key: str) -> bool:
        return folder_key(key) in {folder_key(k) for k in stored()}

    def decline(key: str) -> None:
        key = folder_key(key)
        keys = [k for k in stored() if folder_key(k) != key] + [key]
        save(keys[-limit:] if limit > 0 else keys)

    return Remember(is_declined, decline)


def find_convertible(
    parent: QWidget,
    paths: Iterable[str | os.PathLike],
    extensions: Iterable[str],
    recursive: bool = True,
    skip: Callable[[str], bool] | None = None,
    label: str = "Looking for files that can be converted...",
) -> list[str]:
    """core/folder_discovery.find_by_extension() under a busy dialog (run_busy: the walk runs off the GUI
    thread and the dialog appears only if it takes a moment)."""
    paths = [os.fspath(p) for p in paths]
    exts = list(extensions)
    return run_busy(parent, label, find_by_extension, paths, exts, recursive, skip)


def offer_text(
    found: Sequence[str | os.PathLike],
    loaded_count: int,
    kind_label: str,
    target_label: str,
    noun: str = "file",
    converter: str = "",
    question: str | None = None,
) -> str:
    """The box's text: "No <kind_label> files found, but N <noun>(s) in other formats (FLAC, WAV) that can be
    converted to <target_label>." ("Also found N ..." when `loaded_count` files did load), then the
    question. `converter` names the tool ("Calibre": "... that Calibre can convert to EPUB")."""
    intro = f"No {kind_label} files found, but" if not loaded_count else "Also found"
    kinds = ", ".join(extension_labels(found))
    kinds_part = f" in other formats ({kinds})" if kinds else ""
    how = f"that {converter} can convert to {target_label}" if converter else f"that can be converted to {target_label}"
    if question is None:
        question = f"Convert them to {target_label}?"
    return f"{intro} {len(found)} {noun}(s){kinds_part} {how}.\n\n{question}"


def details_text(found: Sequence[str | os.PathLike], limit: int = DETAILS_LIMIT) -> str:
    """The Show Details list: one path per line, at most `limit`, then "... and N more"."""
    shown = [os.fspath(p) for p in found[:limit]]
    more = f"\n... and {len(found) - len(shown)} more" if len(found) > len(shown) else ""
    return "\n".join(shown) + more


def offer_conversion(
    parent: QWidget | None,
    found: Sequence[str | os.PathLike],
    loaded_count: int,
    kind_label: str,
    target_label: str,
    remember_key: str | os.PathLike | Sequence[str | os.PathLike] | None = None,
    remember: Remember | tuple[Callable[[str], bool], Callable[[str], None]] | None = None,
    *,
    text: str | None = None,
    noun: str = "file",
    converter: str = "",
    question: str | None = None,
    title: str | None = None,
    yes_text: str = "Convert",
    no_text: str = "Not Now",
    extra_buttons: Sequence[tuple[str, str]] = (),
    default: str = YES,
    options: Sequence[QWidget] = (),
) -> str:
    """Asks whether to convert `found` (the files Open Folder did not load) and returns the answer: YES,
    NO, NEVER, or the result string of one of `extra_buttons`. Nothing is shown (NO) when `found` is empty.

    `loaded_count` picks the wording ("No <kind_label> files found, but ..." when 0, "Also found ..."
    otherwise; see offer_text() for `noun`, `converter` and `question`). The full list is under Show Details.

    With `remember` and `remember_key` (the folder that was opened): when that folder was declined before,
    NEVER is returned at once; otherwise the box has a "Don't ask again for this folder" checkbox. Ticked
    with No (or Escape) it records the folder and returns NEVER; ticked with Convert it records the folder
    too (the originals are usually kept, and the same offer would come back on every open) and returns YES.

    Richer boxes (cbzredactor's Convert Now / Add Unconverted / Skip): `extra_buttons` are (label, result)
    pairs added between the yes and no buttons, `default` is the result whose button is the default, and
    `options` are the caller's own widgets (checkboxes such as "Dry run first") shown under the text; the
    caller reads them after this returns.

    `text` replaces the whole message (an app whose wording does not fit offer_text(): it can list files
    inline itself). `remember_key` may be one folder or a sequence of folders (several opened at once): the
    offer is skipped (NEVER) only when ALL of them were declined before, and the checkbox then reads "Don't
    ask again for these folders" and records every one."""
    if not found:
        return NO
    store = Remember(*remember) if remember is not None else None
    keys: list[str] = []
    if remember_key is not None and store is not None:
        many = remember_key if isinstance(remember_key, (list, tuple, set, frozenset)) else [remember_key]
        keys = [folder_key(k) for k in many]
    if keys and all(store.is_declined(k) for k in keys):
        return NEVER

    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(title or f"Convert to {target_label}?")
    box.setText(text if text is not None else offer_text(found, loaded_count, kind_label, target_label, noun, converter, question))
    box.setDetailedText(details_text(found))

    results: dict[int, str] = {}
    yes_button = box.addButton(yes_text, QMessageBox.ButtonRole.AcceptRole)
    results[id(yes_button)] = YES
    buttons = {YES: yes_button}
    for label, result in extra_buttons:
        button = box.addButton(label, QMessageBox.ButtonRole.ActionRole)
        results[id(button)] = result
        buttons[result] = button
    no_button = box.addButton(no_text, QMessageBox.ButtonRole.RejectRole)
    results[id(no_button)] = NO
    buttons[NO] = no_button
    box.setEscapeButton(no_button)
    box.setDefaultButton(buttons.get(default, yes_button))

    dont_ask = None
    widgets = list(options)
    if keys:
        dont_ask = QCheckBox(DONT_ASK_MANY_TEXT if len(keys) > 1 else DONT_ASK_TEXT)
        dont_ask.setObjectName("convert_offer_dont_ask")
        widgets.append(dont_ask)
    if widgets:
        # More than one widget, so not QMessageBox.setCheckBox() (which takes one): they go into the box's
        # own grid, under the text.
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        for widget in widgets:
            layout.addWidget(widget)
        grid = box.layout()
        grid.addWidget(holder, grid.rowCount(), 0, 1, grid.columnCount())

    box.exec()
    clicked = box.clickedButton()
    answer = results.get(id(clicked), NO) if clicked is not None else NO
    if dont_ask is not None and dont_ask.isChecked() and answer in (YES, NO):
        for key in keys:
            store.decline(key)
        if answer == NO:
            answer = NEVER
    return answer

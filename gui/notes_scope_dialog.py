"""
redactor_common/gui/notes_scope_dialog.py

The first step of Tools > Create Notes: which folder, and what to create. A short list of radio buttons (the choices the
app built with core/notes_scope.merge_choices) and "Another folder...", the kinds of note to make as check boxes, a time
hint for a big scope, "also review notes made by an older version", and Include subfolders. The first useful choice is
ticked, so Enter just works. Nothing is used without Continue.

What a folder would get ('would get a series note: Dark') and how many folders a scope covers are the app's own knowledge:
`describe(path)` and `count(folder, include_subfolders)` run on worker threads (a walk of a big library takes a moment),
`caption_of(info, folder_name, kinds)` and `hint(counts, kinds)` turn what they found into words.

Include subfolders mirrors the File menu's checkbox: `on_include_changed(bool)` is called at every tick, the caller passes
the shared action's setChecked, so there is still exactly one saved value.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup, QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton, QRadioButton, QSizePolicy, QVBoxLayout,
    QWidget,
)

from redactor_common.core.notes_scope import ScopeChoice
from redactor_common.gui.owned_dialog import OwnedDialog

OUTDATED_CHECK_TEXT = "Also review notes made by an older version"
MAX_PATH_CHARS = 70

_running: set = set()  # worker threads still running when their dialog is gone: kept alive until they end


class _Worker(QThread):
    """Runs `fn(*args)` off the GUI thread and emits (key, result); an exception gives None (it is only a hint)."""

    done = pyqtSignal(object, object)

    def __init__(self, key, fn: Callable, *args):
        super().__init__()
        self.key, self._fn, self._args = key, fn, args

    def run(self) -> None:
        try:
            result = self._fn(*self._args)
        except Exception:  # noqa: BLE001 -- only a caption or a hint
            result = None
        self.done.emit(self.key, result)


def _start(worker: _Worker, slot: Callable) -> None:
    worker.done.connect(slot)
    worker.finished.connect(lambda t=worker: (_running.discard(t), t.deleteLater()))
    _running.add(worker)
    worker.start()


def _shrinkable(*labels: QLabel) -> None:
    for label in labels:
        label.setWordWrap(True)
        label.setMinimumWidth(0)
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)


class NotesScopeDialog(OwnedDialog):
    def __init__(
        self, parent: QWidget, choices: list[ScopeChoice], include_subfolders: bool, *,
        kind_checks: tuple[tuple[str, str], ...], kinds: str, parse_kinds: Callable, kinds_text: Callable,
        describe: Callable[[str], object], caption_of: Callable[[object, str, str], str],
        count: Callable[[str, bool], object], hint: Callable[[object, str], str],
        on_include_changed: Optional[Callable[[bool], None]] = None, start: str = "",
        title: str = "Create Notes: which folder?", heading: str = "Create notes: which folder?",
        browse_title: str = "Create Notes: choose a folder",
    ):
        super().__init__(parent)
        self.outdated = False
        self.setWindowTitle(title)
        self.start, self.folder, self.kinds, self.picked = start, "", kinds, ""
        self._on_include_changed = on_include_changed
        self._parse_kinds, self._kinds_text = parse_kinds, kinds_text
        self._describe_fn, self._caption_of, self._count_fn, self._hint_fn = describe, caption_of, count, hint
        self._browse_title = browse_title
        self._previous: Optional[QRadioButton] = None
        self._browsing = False
        self.radios: list[tuple[QRadioButton, str]] = []
        self.captions: list[QLabel] = []

        head = QLabel(heading)
        font = head.font()
        font.setBold(True)
        head.setFont(font)
        layout = QVBoxLayout(self)
        layout.addWidget(head)
        self.group = QButtonGroup(self)
        for choice in choices:
            radio = QRadioButton(self._elide(choice.path))
            radio.setToolTip(choice.path)
            caption = QLabel(choice.caption[:1].upper() + choice.caption[1:])
            caption.setToolTip(choice.path)
            caption.setContentsMargins(22, 0, 0, 6)
            _shrinkable(caption)
            self.group.addButton(radio)
            self.radios.append((radio, choice.path))
            self.captions.append(caption)
            layout.addWidget(radio)
            layout.addWidget(caption)
            if choice.default:
                radio.setChecked(True)
        self.other_radio = QRadioButton("Another folder…")
        self.group.addButton(self.other_radio)
        self.browse_button = QPushButton("Browse…")
        self.browse_button.setAutoDefault(False)
        row = QHBoxLayout()
        row.addWidget(self.other_radio)
        row.addWidget(self.browse_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.other_label = QLabel("")
        self.other_label.setContentsMargins(22, 0, 0, 6)
        _shrinkable(self.other_label)
        layout.addWidget(self.other_label)
        if not choices:
            self.other_radio.setChecked(True)
        self._previous = self.group.checkedButton()

        layout.addSpacing(6)
        what = QLabel("What to create")
        what_font = what.font()
        what_font.setBold(True)
        what.setFont(what_font)
        layout.addWidget(what)
        self.kind_boxes: dict[str, QCheckBox] = {}
        wanted = parse_kinds(kinds)
        for key, text in kind_checks:
            box = QCheckBox(text)
            box.setChecked(key in wanted)
            box.toggled.connect(lambda *_: (self._update_hint(), self._update_continue(), self._update_captions()))
            self.kind_boxes[key] = box
            layout.addWidget(box)

        layout.addSpacing(6)
        self.hint_label = QLabel()  # a one-line time warning for a big scope: only a hint
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet("background:#fff6d6; color:#4d3600; border:1px solid #e6cf85; padding:3px 8px;")
        self.hint_label.setVisible(False)
        layout.addWidget(self.hint_label)
        self.outdated_box = QCheckBox(OUTDATED_CHECK_TEXT)  # never on by itself, never remembered
        self.outdated_box.setToolTip("Lists the notes this app wrote in an older shape that nobody edited, and the notes "
                                     "of the first versions (no stamp: it cannot be known whether you edited them), "
                                     "as updates you can compare and replace one by one. A note you edited, or one "
                                     "this app did not write, is never listed.")
        layout.addWidget(self.outdated_box)
        self.include_box = QCheckBox("Include subfolders")
        self.include_box.setChecked(include_subfolders)
        self.include_note = QLabel()
        self.include_note.setContentsMargins(22, 0, 0, 0)
        _shrinkable(self.include_note)
        layout.addSpacing(6)
        layout.addWidget(self.include_box)
        layout.addWidget(self.include_note)
        self._update_include_note()

        self.continue_button = QPushButton("Continue")
        self.continue_button.setDefault(True)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setAutoDefault(False)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.continue_button)
        layout.addSpacing(6)
        layout.addLayout(buttons)

        self._base_captions = [label.text() for label in self.captions]
        self.infos: dict[str, object] = {}  # path -> what each folder would get
        self._describe([path for _radio, path in self.radios])
        self.counts: Optional[object] = None  # the counts of the chosen folder, once known
        self._count_key: Optional[tuple] = None
        self._count_busy = False
        self._count_timer = QTimer(self)
        self._count_timer.setSingleShot(True)
        self._count_timer.timeout.connect(self._start_count)
        self.group.buttonToggled.connect(lambda *_: self._count_timer.start(150))
        self.include_box.toggled.connect(lambda *_: self._count_timer.start(150))
        self._count_timer.start(0)
        self.group.buttonToggled.connect(self._radio_toggled)
        self.browse_button.clicked.connect(self._browse)
        self.include_box.toggled.connect(self._include_toggled)
        self.continue_button.clicked.connect(self._continue)
        self.cancel_button.clicked.connect(self.reject)
        self.setMinimumWidth(560)
        self._update_continue()
        if not choices:
            QTimer.singleShot(0, self._browse)

    @staticmethod
    def _elide(path: str) -> str:
        if len(path) <= MAX_PATH_CHARS:
            return path
        head = MAX_PATH_CHARS // 3
        return path[:head] + "…" + path[-(MAX_PATH_CHARS - head - 1):]

    def chosen_folder(self) -> str:
        """The folder the ticked radio stands for ("" when 'Another folder...' has none yet)."""
        button = self.group.checkedButton()
        if button is self.other_radio:
            return self.picked
        for radio, path in self.radios:
            if radio is button:
                return path
        return ""

    def chosen_kinds(self) -> str:
        """The ticked kinds as a comma list; '' when nothing is ticked."""
        ticked = [key for key, box in self.kind_boxes.items() if box.isChecked()]
        return self._kinds_text(ticked) if ticked else ""

    # -- what each folder would get, and the counts -----------------------------------------------------------

    def _describe(self, paths: list[str]) -> None:
        for path in (p for p in paths if p):
            _start(_Worker(path, self._describe_fn, path), self._described)

    def _described(self, path, info) -> None:
        if info is not None:
            self.infos[path] = info
        self._update_captions()

    def _update_captions(self) -> None:
        """Under each folder what it would get, following the ticked kinds."""
        kinds = self.chosen_kinds() or self._kinds_text([])
        for (_radio, path), label, base in zip(self.radios, self.captions, self._base_captions):
            info = self.infos.get(path)
            if info is not None:
                text = self._caption_of(info, Path(path).name, kinds)
                label.setText(f"{base[:1].upper() + base[1:]}  -  {text}" if base else text)
        if self.picked and self.picked in self.infos:
            self.other_label.setText(f"{self.picked}  -  {self._caption_of(self.infos[self.picked], Path(self.picked).name, kinds)}")

    def _start_count(self) -> None:
        """Counts the chosen scope off the GUI thread. ONE walk at a time: a toggle while a walk runs only notes the
        newest wish, which starts when the running one ends (a walk of a big library cannot be interrupted, so
        starting one per toggle used to pile up threads, some still running when the dialog closed)."""
        folder = self.chosen_folder()
        if not folder:
            return
        key = (folder, self.include_box.isChecked())
        if key == self._count_key and self.counts is not None:
            return
        self._count_key = key
        self.counts = None
        self._update_hint()
        if not self._count_busy:
            self._launch_count(key)

    def _launch_count(self, key: tuple) -> None:
        self._count_busy = True
        _start(_Worker(key, self._count_fn, *key), self._counted)

    def _counted(self, key, counts) -> None:
        self._count_busy = False
        if key != self._count_key:  # the choice changed while this ran: count the newest one now
            if self._count_key is not None:
                self._launch_count(self._count_key)
            return
        self.counts = counts
        self._update_hint()

    def _update_hint(self) -> None:
        text = self._hint_fn(self.counts, self.chosen_kinds()) if self.counts and self.chosen_kinds() else ""
        self.hint_label.setText(text)
        self.hint_label.setVisible(bool(text))

    def _update_continue(self) -> None:
        self.continue_button.setEnabled(bool(self.chosen_folder()) and bool(self.chosen_kinds()))

    def _update_include_note(self) -> None:
        self.include_note.setText("Everything below the folder is looked at."
                                  if self.include_box.isChecked() else "Only this folder is looked at.")

    def _include_toggled(self, on: bool) -> None:
        self._update_include_note()
        if self._on_include_changed is not None:
            self._on_include_changed(bool(on))

    def _radio_toggled(self, button, checked: bool) -> None:
        if not checked or self._browsing:
            return
        if button is self.other_radio:
            if not self._browse():  # cancelled: back to where it was (or stay empty when nothing came before)
                if self._previous is not None and self._previous is not self.other_radio:
                    self._previous.setChecked(True)
                    return
        else:
            self._previous = button
        self._update_continue()

    def _browse(self) -> bool:
        self._browsing = True
        try:
            folder = QFileDialog.getExistingDirectory(self, self._browse_title, self.picked or self.start)
        finally:
            self._browsing = False
        if not folder:
            self._update_continue()
            return False
        self.picked = folder
        if folder not in self.infos:
            self._describe([folder])
        self.other_label.setText(folder)
        self.other_label.setToolTip(folder)
        self._browsing = True
        self.other_radio.setChecked(True)
        self._browsing = False
        self._previous = self.other_radio
        self._update_continue()
        return True

    def _continue(self) -> None:
        self.folder = self.chosen_folder()
        self.kinds = self.chosen_kinds()
        self.outdated = self.outdated_box.isChecked()
        if self.folder:
            self.accept()


def ask_scope(parent: QWidget, choices: list[ScopeChoice], include_subfolders: bool, **kw) -> tuple[str, str, bool]:
    """(folder, kinds, include_outdated): the folder to create notes for ("" = cancelled), what to create and whether to
    also review notes made by an older version (always off at first). `kw` are NotesScopeDialog's keyword arguments."""
    dialog = NotesScopeDialog(parent, choices, include_subfolders, **kw)
    try:
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return "", kw.get("kinds", ""), False
        return dialog.folder, dialog.kinds, dialog.outdated
    finally:
        dialog.deleteLater()  # parented to `parent`: one dialog per call would pile up for the life of the window

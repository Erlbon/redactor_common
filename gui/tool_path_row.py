"""
redactor_common/gui/tool_path_row.py

"Locate External Tools": one row per external program (ffmpeg, mp3val,
MKVToolNix...) with a path field, Browse..., Clear (back to auto-detect
via PATH) and a live "✓ found" / "✗ missing" indicator, so the user sees
at once whether the path -- or auto-detection -- resolves. Written twice
before this: mp3redactor gui/external_tools_dialog.py (_ToolRow,
ToolPathsWidget) and videoredactor gui/tool_settings_dialog.py
(ToolPathsWidget), each with its own hard-coded green/red.

The two differed in when a change is stored; that is the `commit`
parameter, not a silent choice:

* commit="immediate" (video): editing finished, Browse and Clear each
  call `on_change(path)` straight away -- the page behaves the same in
  its own dialog and inside Preferences, with no OK button needed.
* commit="deferred" (mp3): nothing is stored by the row; the caller
  reads ToolPathsWidget.paths() when its dialog is accepted.

`probe(path)` says whether the tool resolves, given the path now in the
field ("" = auto-detect). The default asks core/tool_locator.find_tool
with that path as the override. Video's probe reads the stored override
instead (is_executable_available), which works because "immediate"
stores before it probes.

Usage:
    rows = [ToolPathRow("ffmpeg:", "ffmpeg", get_tool_override("ffmpeg"),
                        probe=lambda _path: is_executable_available("ffmpeg"),
                        on_change=lambda path: set_tool_override("ffmpeg", path))]
    page = ToolPathsWidget(rows, "If ffmpeg isn't on your PATH, point at it here. ...")

    # mp3 style:
    rows = [ToolPathRow("mp3val:", MP3VAL_EXE_NAME, settings.mp3val_path,
                        probe=lambda p: find_tool(MP3VAL_EXE_NAME, override=p or None) is not None,
                        commit="deferred", read_only=True, missing_text="✗ not found")]
    ...
    settings.mp3val_path = page.paths()[MP3VAL_EXE_NAME]
"""

from __future__ import annotations

import os
import sys
from typing import Callable, Iterable, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from redactor_common.core.tool_locator import find_tool
from redactor_common.gui.colors import FOUND_TEXT_COLOR, MISSING_TEXT_COLOR

COMMIT_IMMEDIATE = "immediate"
COMMIT_DEFERRED = "deferred"
AUTO_DETECT_PLACEHOLDER = "(auto-detect via PATH)"


def default_executable_filter() -> str:
    """Executables on Windows (with All files as an escape hatch); any file
    elsewhere, where tools have no .exe."""
    return "Executables (*.exe);;All files (*)" if sys.platform == "win32" else "All files (*)"


class ToolPathRow(QWidget):
    """Path field + Browse + Clear + found indicator for one executable. The
    row's label text is `label`; ToolPathsWidget puts it in front."""

    path_changed = pyqtSignal(str)  # the user changed the path (Browse, Clear, or editing finished)

    def __init__(
        self,
        label: str,
        exe_name: str,
        initial: str = "",
        probe: Optional[Callable[[str], bool]] = None,
        on_change: Optional[Callable[[str], None]] = None,
        *,
        commit: str = COMMIT_IMMEDIATE,
        read_only: bool = False,
        placeholder: str = AUTO_DETECT_PLACEHOLDER,
        found_text: str = "✓ found",
        missing_text: str = "✗ missing",
        status_width: Optional[int] = None,
        file_filter: Optional[str] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        if commit not in (COMMIT_IMMEDIATE, COMMIT_DEFERRED):
            raise ValueError(f"commit must be {COMMIT_IMMEDIATE!r} or {COMMIT_DEFERRED!r}, not {commit!r}")
        self.label = label
        self.exe_name = exe_name
        self._probe = probe or (lambda path: find_tool(exe_name, override=path or None) is not None)
        self._on_change = on_change
        self.commit = commit
        self._found_text = found_text
        self._missing_text = missing_text
        self._file_filter = file_filter or default_executable_filter()
        self._committed = (initial or "").strip()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.path_edit = QLineEdit(initial or "")
        self.path_edit.setPlaceholderText(placeholder)
        self.path_edit.setReadOnly(read_only)
        if not read_only:
            self.path_edit.editingFinished.connect(self._on_editing_finished)
        layout.addWidget(self.path_edit, 1)

        self.browse_button = QPushButton("Browse...")
        self.browse_button.clicked.connect(self.browse)
        layout.addWidget(self.browse_button)

        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self.clear)
        layout.addWidget(self.clear_button)

        self.status_label = QLabel()
        if status_width is not None:
            self.status_label.setFixedWidth(status_width)
        layout.addWidget(self.status_label)

        self.refresh_status()

    # -- state ---------------------------------------------------------------------

    def current_path(self) -> str:
        """The path now in the field, stripped ("" = auto-detect)."""
        return self.path_edit.text().strip()

    def is_found(self) -> bool:
        """The probe's answer for the current path; a probe that raises counts as missing."""
        try:
            return bool(self._probe(self.current_path()))
        except Exception:
            return False

    def refresh_status(self) -> bool:
        found = self.is_found()
        self.status_label.setText(self._found_text if found else self._missing_text)
        self.status_label.setStyleSheet(f"color: {FOUND_TEXT_COLOR if found else MISSING_TEXT_COLOR};")
        return found

    # -- user actions --------------------------------------------------------------

    def set_path(self, path: str) -> None:
        """Sets the field as if the user had chosen `path` (commits per policy)."""
        self.path_edit.setText(path or "")
        self._changed()

    def browse(self) -> None:
        start = os.path.dirname(self.current_path()) if self.current_path() else ""
        path, _ = QFileDialog.getOpenFileName(self, f"Locate {self.exe_name}", start, self._file_filter)
        if path:  # cancelled: leave the current path untouched
            self.set_path(path)

    def clear(self) -> None:
        self.set_path("")

    def _on_editing_finished(self) -> None:
        self._changed()

    def _changed(self) -> None:
        path = self.current_path()
        changed = path != self._committed
        if changed:
            self._committed = path
            if self.commit == COMMIT_IMMEDIATE and self._on_change is not None:
                self._on_change(path)
            self.path_changed.emit(path)
        self.refresh_status()


class ToolPathsWidget(QWidget):
    """An intro text, an optional rich-text line (download links), and the
    rows in a form (label: row). Used as a dialog's body and as the
    Tools page of Preferences."""

    def __init__(
        self,
        rows: Iterable[ToolPathRow],
        intro: str = "",
        *,
        links_html: str = "",
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.rows: list[ToolPathRow] = list(rows)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        if intro:
            self.intro_label = QLabel(intro)
            self.intro_label.setWordWrap(True)
            layout.addWidget(self.intro_label)
        if links_html:
            links = QLabel(links_html)
            links.setTextFormat(Qt.TextFormat.RichText)
            links.setOpenExternalLinks(True)
            links.setWordWrap(True)
            layout.addWidget(links)
        form_container = QWidget()
        form = QFormLayout(form_container)
        for row in self.rows:
            form.addRow(row.label, row)
        layout.addWidget(form_container)
        layout.addStretch()

    def row(self, exe_name: str) -> ToolPathRow:
        return next(r for r in self.rows if r.exe_name == exe_name)

    def paths(self) -> dict[str, str]:
        """{exe_name: current path} ("" = auto-detect), in row order."""
        return {r.exe_name: r.current_path() for r in self.rows}

    @property
    def path_edits(self) -> dict[str, QLineEdit]:
        return {r.exe_name: r.path_edit for r in self.rows}

    @property
    def status_labels(self) -> dict[str, QLabel]:
        return {r.exe_name: r.status_label for r in self.rows}

    def refresh_all(self) -> None:
        for r in self.rows:
            r.refresh_status()

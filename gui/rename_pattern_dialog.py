"""
redactor_common/gui/rename_pattern_dialog.py

The "Tag -> Filename" dialog, mp3tag's Convert feature: build a
filename pattern from metadata placeholders, preview the result for
every item being processed, then either rename the files in place or
export copies with the new names into a chosen folder, leaving the
originals untouched -- or move them into a folder tree under a library
root (the pattern may then contain "/", see core/move_plan.py).

Generalized from the epub project's RenameDialog to work on any item
type via accessor callables, so it isn't tied to EpubBook.
"""

from __future__ import annotations

import os
from typing import Callable, TypeVar

from PyQt6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMenu, QPushButton, QRadioButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from redactor_common.core.error_summary import summarize_errors
from redactor_common.core.move_plan import PlannedMove, plan_moves
from redactor_common.core.rename_pattern import render_filename, unique_path, zero_pad_numeric_value
from redactor_common.gui.pattern_field_panel import PatternFieldPanel

T = TypeVar("T")


class RenamePatternDialog(QDialog):
    def __init__(
        self,
        items: list[T],
        placeholders: list[tuple[str, str]],  # (field_key, label)
        get_values: Callable[[T], dict[str, str]],
        get_current_path: Callable[[T], str],
        pattern_history: list[str],
        default_pattern: str,
        title: str = "Rename / Export by Metadata Pattern",
        item_noun: str = "item",
        zero_pad_field: str | None = None,
        zero_pad_label: str = "Zero-pad number to:",
        zero_pad_widths: tuple[int, ...] = (2, 3, 4),
        always_pad_fields: dict[str, int] | None = None,
        ascii_only: bool = False,
        on_ascii_only_changed: Callable[[bool], None] | None = None,
        zero_pad_initial: tuple[bool, int] | None = None,
        on_zero_pad_changed: Callable[[bool, int], None] | None = None,
        library_root: str = "",
        on_library_root_changed: Callable[[str], None] | None = None,
        macro_labels: Callable[[], list[str]] | None = None,
        on_save_macro: Callable[[int, dict], None] | None = None,
        on_clear_macro: Callable[[int], None] | None = None,
        parent=None,
    ):
        """
        `get_values(item)` returns the item's full placeholder value
        dict (already reflecting any unsaved in-memory edits).
        `get_current_path(item)` returns the item's current file path.
        `zero_pad_field`, if given, is the placeholder key that gets
        zero-padding applied -- to whichever of `zero_pad_widths` is
        picked in the dropdown -- when the checkbox is on (e.g.
        "series_index" or "episode").
        `always_pad_fields`, if given, is a field -> width map applied
        unconditionally, no checkbox involved (e.g. {"month": 2} so a
        %month% token always renders "02", never "2" -- unlike
        `zero_pad_field`, this isn't a per-run user choice, it's always
        correct for that field).
        `ascii_only` / `on_ascii_only_changed`: the "ASCII-safe filenames"
        checkbox (rename_pattern.to_ascii) -- its starting state, and a
        callback the app uses to remember the choice in its settings.

        `zero_pad_initial` / `on_zero_pad_changed`: the same
        remember-last-choice pair for the zero-pad checkbox and width
        -- `(enabled, width)` in, `callback(enabled, width)` on every
        change. Ignored without `zero_pad_field`.

        `library_root` / `on_library_root_changed`: the "Move into
        folders" mode's root folder -- its starting value and a callback
        (`callback(path)`) the app uses to remember the choice, like the
        ASCII checkbox. In that mode the pattern may contain "/" or "\\"
        to make sub-folders (`%author%/%series%/%title%`).

        `macro_labels` / `on_save_macro` / `on_clear_macro`: the "Save as
        Macro" button. `macro_labels()` returns one short description per
        slot ("" for an empty one); `on_save_macro(slot, macro_state())` and
        `on_clear_macro(slot)` (slots are 0-based) let the app keep them. The
        app replays a macro later with `apply_macro_state()` on a dialog it
        never shows -- see those two methods.
        """
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(990, 560)
        self.items = items
        self._get_values = get_values
        self._get_current_path = get_current_path
        self._zero_pad_field = zero_pad_field
        self._always_pad_fields = always_pad_fields or {}
        self._initial_ascii_only = ascii_only
        self._on_ascii_only_changed = on_ascii_only_changed
        self._zero_pad_initial = zero_pad_initial
        self._on_zero_pad_changed = on_zero_pad_changed
        self.output_folder: str | None = None
        self._library_root = library_root or ""
        self._on_library_root_changed = on_library_root_changed
        self._moves: list[PlannedMove] = []
        self._macro_labels = macro_labels
        self._on_save_macro = on_save_macro
        self._on_clear_macro = on_clear_macro

        self._build_ui(placeholders, pattern_history, default_pattern, item_noun, zero_pad_label, zero_pad_widths)
        self._refresh_preview()

    def _build_ui(
        self, placeholders, pattern_history, default_pattern, item_noun, zero_pad_label, zero_pad_widths
    ) -> None:
        outer = QHBoxLayout(self)

        layout = QVBoxLayout()
        outer.addLayout(layout, 2)
        layout.addWidget(QLabel(f"Applies to {len(self.items)} {item_noun}(s)."))

        pattern_row = QHBoxLayout()
        pattern_row.addWidget(QLabel("Pattern:"))
        starting_pattern = pattern_history[0] if pattern_history else default_pattern
        self.pattern_edit = QLineEdit(starting_pattern)
        self.pattern_edit.textChanged.connect(self._refresh_preview)
        pattern_row.addWidget(self.pattern_edit, 1)

        self._panel = PatternFieldPanel(self.pattern_edit, placeholders, pattern_history, parent=self)
        pattern_row.addWidget(self._panel.recent_button)
        layout.addLayout(pattern_row)
        layout.addWidget(self._panel.recent_list)

        outer.addWidget(self._panel.placeholder_list, 1)

        if self._zero_pad_field:
            pad_row = QHBoxLayout()
            self.zero_pad_cb = QCheckBox(zero_pad_label)
            pad_row.addWidget(self.zero_pad_cb)

            self.zero_pad_width_combo = QComboBox()
            for width in zero_pad_widths:
                self.zero_pad_width_combo.addItem(f"{width} digits (e.g. {str(1).zfill(width)})", width)
            pad_row.addWidget(self.zero_pad_width_combo)
            if self._zero_pad_initial is not None:
                enabled, width = self._zero_pad_initial
                self.zero_pad_cb.setChecked(bool(enabled))
                index = self.zero_pad_width_combo.findData(width)
                if index >= 0:
                    self.zero_pad_width_combo.setCurrentIndex(index)
            # Connected after the restore above so setting the saved
            # state doesn't write it straight back.
            self.zero_pad_cb.stateChanged.connect(self._on_zero_pad_edited)
            self.zero_pad_width_combo.currentIndexChanged.connect(self._on_zero_pad_edited)
            pad_row.addStretch(1)
            layout.addLayout(pad_row)
        else:
            self.zero_pad_cb = None
            self.zero_pad_width_combo = None

        self.ascii_cb = QCheckBox("ASCII-safe filenames (é → e, æ → ae, ø → o; other symbols dropped)")
        self.ascii_cb.setToolTip(
            "Only plain ASCII letters, digits and punctuation in the new names -- for old file "
            "systems, network shares, e-readers, car stereos and sync tools that mangle anything else."
        )
        self.ascii_cb.setChecked(self._initial_ascii_only)
        self.ascii_cb.toggled.connect(self._on_ascii_toggled)
        layout.addWidget(self.ascii_cb)

        mode_box = QGroupBox("Action")
        mode_layout = QVBoxLayout(mode_box)
        self.rename_radio = QRadioButton("Rename files in place (in their current folder)")
        self.export_radio = QRadioButton("Export renamed copies to a folder (originals untouched)")
        self.rename_radio.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.rename_radio)
        group.addButton(self.export_radio)
        self.move_radio = QRadioButton("Move into folders under a library root (pattern may contain /)")
        group.addButton(self.move_radio)
        mode_layout.addWidget(self.rename_radio)

        export_row = QHBoxLayout()
        export_row.addWidget(self.export_radio)
        self.choose_folder_btn = QPushButton("Choose Folder\u2026")
        self.choose_folder_btn.clicked.connect(self._choose_folder)
        self.choose_folder_btn.setEnabled(False)
        export_row.addWidget(self.choose_folder_btn)
        mode_layout.addLayout(export_row)

        self.folder_label = QLabel("(no folder chosen)")
        self.folder_label.setStyleSheet("color: gray; font-size: 11px;")
        mode_layout.addWidget(self.folder_label)

        move_row = QHBoxLayout()
        move_row.addWidget(self.move_radio)
        self.choose_root_btn = QPushButton("Library Root…")
        self.choose_root_btn.clicked.connect(self._choose_root)
        self.choose_root_btn.setEnabled(False)
        move_row.addWidget(self.choose_root_btn)
        mode_layout.addLayout(move_row)

        self.root_label = QLabel(self._library_root or "(no library root chosen)")
        self.root_label.setStyleSheet("color: gray; font-size: 11px;")
        mode_layout.addWidget(self.root_label)

        self.rename_radio.toggled.connect(self._on_mode_toggled)
        self.export_radio.toggled.connect(self._on_mode_toggled)
        self.move_radio.toggled.connect(self._on_mode_toggled)
        layout.addWidget(mode_box)

        self.preview_table = QTableWidget()
        self.preview_table.setColumnCount(2)
        self.preview_table.setHorizontalHeaderLabels(["Current filename", "New filename"])
        self.preview_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.preview_table, 1)

        self.warning_label = QLabel("")
        self.warning_label.setStyleSheet("color: #b45309; font-size: 11px;")
        self.warning_label.setWordWrap(True)
        layout.addWidget(self.warning_label)

        if self._on_save_macro is not None:
            macro_row = QHBoxLayout()
            self.macro_button = QPushButton("Save as Macro…")
            self.macro_button.setToolTip(
                "Remember the pattern, action and options above in one of the macro slots, "
                "so a hotkey can run them on the selected files without this window."
            )
            self.macro_menu = QMenu(self.macro_button)
            self.macro_menu.aboutToShow.connect(self._fill_macro_menu)
            self.macro_button.setMenu(self.macro_menu)
            macro_row.addWidget(self.macro_button)
            macro_row.addStretch(1)
            layout.addLayout(macro_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Apply")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)

    # --- macros -----------------------------------------------------------

    def _fill_macro_menu(self) -> None:
        self.macro_menu.clear()
        labels = self._macro_labels() if self._macro_labels else []
        for slot, label in enumerate(labels):
            action = self.macro_menu.addAction(f"Save as Macro {slot + 1}" + (f"  (replaces: {label})" if label else ""))
            action.triggered.connect(lambda _checked=False, n=slot: self._on_save_macro(n, self.macro_state()))
        if self._on_clear_macro is not None and any(labels):
            self.macro_menu.addSeparator()
            for slot, label in enumerate(labels):
                if label:
                    action = self.macro_menu.addAction(f"Clear Macro {slot + 1}  ({label})")
                    action.triggered.connect(lambda _checked=False, n=slot: self._on_clear_macro(n))

    def macro_state(self) -> dict:
        """Everything this dialog is set to, as plain JSON-able data: the pattern, the action
        ("rename" / "export" / "move"), the export folder and library root, the zero-pad and
        ASCII options."""
        mode = "move" if self.move_radio.isChecked() else "export" if self.export_radio.isChecked() else "rename"
        return {
            "pattern": self.pattern_edit.text(),
            "mode": mode,
            "export_folder": self.output_folder or "",
            "library_root": self._library_root,
            "zero_pad": bool(self.zero_pad_cb and self.zero_pad_cb.isChecked()),
            "zero_pad_width": int(self.zero_pad_width_combo.currentData()) if self.zero_pad_width_combo else 2,
            "ascii": self.ascii_cb.isChecked(),
        }

    def apply_macro_state(self, state: dict) -> None:
        """Loads a `macro_state()` into the widgets and refreshes the plan, WITHOUT writing the
        values back through the remember-last-choice callbacks (running a macro must not change
        what the dialog starts with). Afterwards `can_apply()`, `apply_problem()`,
        `planned_renames()` and `planned_moves()` answer as if the user had set it all by hand."""
        widgets = [self.pattern_edit, self.ascii_cb, self.rename_radio, self.export_radio, self.move_radio]
        if self.zero_pad_cb is not None:
            widgets += [self.zero_pad_cb, self.zero_pad_width_combo]
        for widget in widgets:
            widget.blockSignals(True)
        try:
            self.pattern_edit.setText(str(state.get("pattern", self.pattern_edit.text())))
            self.ascii_cb.setChecked(bool(state.get("ascii", self.ascii_cb.isChecked())))
            if self.zero_pad_cb is not None:
                self.zero_pad_cb.setChecked(bool(state.get("zero_pad", False)))
                index = self.zero_pad_width_combo.findData(int(state.get("zero_pad_width", 2)))
                if index >= 0:
                    self.zero_pad_width_combo.setCurrentIndex(index)
            folder = state.get("export_folder") or ""
            self.output_folder = folder or None
            self.folder_label.setText(folder or "(no folder chosen)")
            root = state.get("library_root") or ""
            if root:
                self._library_root = root
            self.root_label.setText(self._library_root or "(no library root chosen)")
            mode = state.get("mode", "rename")
            {"move": self.move_radio, "export": self.export_radio}.get(mode, self.rename_radio).setChecked(True)
        finally:
            for widget in widgets:
                widget.blockSignals(False)
        self._on_mode_toggled()  # enables the right buttons and refreshes the preview and plan

    def can_apply(self) -> bool:
        """Whether Apply would be enabled right now (a folder chosen, nothing blocking)."""
        return self._ok_button.isEnabled()

    def apply_problem(self) -> str:
        """The warning the dialog shows under the preview ("" when there is none)."""
        return self.warning_label.text()

    def _on_zero_pad_edited(self, *_args) -> None:
        if self._on_zero_pad_changed is not None:
            self._on_zero_pad_changed(
                self.zero_pad_cb.isChecked(), int(self.zero_pad_width_combo.currentData())
            )
        self._refresh_preview()

    def _on_ascii_toggled(self, checked: bool) -> None:
        if self._on_ascii_only_changed is not None:
            self._on_ascii_only_changed(checked)
        self._refresh_preview()

    def ascii_only(self) -> bool:
        return self.ascii_cb.isChecked()

    def _on_mode_toggled(self) -> None:
        self.choose_folder_btn.setEnabled(self.export_radio.isChecked())
        self.choose_root_btn.setEnabled(self.move_radio.isChecked())
        self.preview_table.setHorizontalHeaderLabels(
            ["Current filename", "New path (relative to library root)" if self.move_radio.isChecked() else "New filename"]
        )
        self._refresh_preview()

    def _choose_root(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose Library Root Folder", self._library_root)
        if folder:
            self._library_root = folder
            self.root_label.setText(folder)
            if self._on_library_root_changed is not None:
                self._on_library_root_changed(folder)
            self._refresh_preview()

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose Export Folder")
        if folder:
            self.output_folder = folder
            self.folder_label.setText(folder)
            self._refresh_preview()

    def _refresh_move_preview(self) -> None:
        zero_pad = bool(self.zero_pad_cb and self.zero_pad_cb.isChecked())
        zero_pad_width = self.zero_pad_width_combo.currentData() if self.zero_pad_width_combo else 2

        def padded_values(item) -> dict[str, str]:
            values = dict(self._get_values(item))
            if zero_pad and self._zero_pad_field and self._zero_pad_field in values:
                values[self._zero_pad_field] = zero_pad_numeric_value(values[self._zero_pad_field], zero_pad_width)
            for field, width in self._always_pad_fields.items():
                if field in values:
                    values[field] = zero_pad_numeric_value(values[field], width)
            return values

        self._moves = plan_moves(
            self.items, self._library_root, self.pattern_edit.text(), padded_values,
            self._get_current_path, ascii_only=self.ascii_cb.isChecked(),
        )
        self._planned = [(m.item, m.old_path, m.new_path) for m in self._moves]
        self.preview_table.setRowCount(len(self._moves))
        for row, move in enumerate(self._moves):
            shown = move.relative_path() if not (move.blocking and not self._library_root) else ""
            self.preview_table.setItem(row, 0, QTableWidgetItem(os.path.basename(move.old_path)))
            self.preview_table.setItem(row, 1, QTableWidgetItem(shown))

        if not self._library_root:
            self.warning_label.setText("Choose a library root folder before applying.")
            self._ok_button.setEnabled(False)
            return
        blocking = [f"{os.path.basename(m.old_path)}: {m.warning}" for m in self._moves if m.blocking]
        notes = [f"{os.path.basename(m.old_path)}: {m.warning}" for m in self._moves if m.warning and not m.blocking]
        self.warning_label.setText(summarize_errors(blocking or notes))
        self._ok_button.setEnabled(bool(self.items) and not blocking)

    def _refresh_preview(self) -> None:
        if self.move_radio.isChecked():
            self._refresh_move_preview()
            return
        self._moves = []
        pattern = self.pattern_edit.text()
        zero_pad = bool(self.zero_pad_cb and self.zero_pad_cb.isChecked())
        zero_pad_width = self.zero_pad_width_combo.currentData() if self.zero_pad_width_combo else 2

        self._planned: list[tuple[T, str, str]] = []  # (item, old_path, new_stem)
        self.preview_table.setRowCount(len(self.items))
        taken: set[str] = set()

        for row, item in enumerate(self.items):
            values = dict(self._get_values(item))
            if zero_pad and self._zero_pad_field and self._zero_pad_field in values:
                values[self._zero_pad_field] = zero_pad_numeric_value(values[self._zero_pad_field], zero_pad_width)
            for field, width in self._always_pad_fields.items():
                if field in values:
                    values[field] = zero_pad_numeric_value(values[field], width)

            old_path = self._get_current_path(item)
            old_name = os.path.basename(old_path)
            new_stem = render_filename(values, pattern, ascii_only=self.ascii_cb.isChecked())
            ext = os.path.splitext(old_path)[1]

            directory = self.output_folder if self.export_radio.isChecked() else os.path.dirname(old_path)
            own = None if self.export_radio.isChecked() else old_path
            new_path = unique_path(directory, new_stem, ext, taken, own) if directory else os.path.join("", new_stem + ext)
            taken.add(os.path.normcase(os.path.abspath(new_path)) if directory else new_path)
            new_name = os.path.basename(new_path)

            self.preview_table.setItem(row, 0, QTableWidgetItem(old_name))
            self.preview_table.setItem(row, 1, QTableWidgetItem(new_name))
            self._planned.append((item, old_path, new_path))

        if self.export_radio.isChecked() and not self.output_folder:
            self.warning_label.setText("Choose an export folder before applying.")
            self._ok_button.setEnabled(False)
        else:
            self.warning_label.setText("")
            self._ok_button.setEnabled(bool(self.items))

    def _on_accept(self) -> None:
        self.accept()

    def planned_renames(self) -> list[tuple[T, str, str]]:
        """(item, old_path, new_path) for every item, reflecting the
        current pattern/mode/folder. The caller performs the actual
        rename/copy -- this dialog only plans it, since how a rename
        vs. an export-copy is executed is project-specific (e.g.
        whether it interacts with an undo stack)."""
        return self._planned

    def is_export_mode(self) -> bool:
        return self.export_radio.isChecked()

    def is_move_mode(self) -> bool:
        return self.move_radio.isChecked()

    def library_root(self) -> str:
        return self._library_root

    def planned_moves(self) -> list[PlannedMove]:
        """The richer plan of "Move into folders" mode (folders to create,
        warnings); empty in the other modes. Hand it to
        gui.move_runner.run_planned_moves()."""
        return self._moves if self.move_radio.isChecked() else []

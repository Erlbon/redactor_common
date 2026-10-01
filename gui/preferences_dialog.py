"""
redactor_common/gui/preferences_dialog.py

The shared Preferences dialog. An app hands it PrefSection lists (core/
preferences.py: the standard sections plus any of its own) and a
PreferencesBackend over its own storage; the dialog builds one page per
section with a control per setting, help text under each control, greying for
settings that depend on a checkbox, and OK / Cancel / Apply plus "Reset to
Defaults" for the current page.

Only keys the user actually changed are written, in ONE backend.set_many()
call per OK/Apply; Cancel writes nothing. `result_values()` returns everything
this dialog wrote, so the app can refresh just what moved.

An app's specialised widgets (mp3's tool paths, a table of API keys...) join
as `extra_pages`: (title, factory) pairs whose pages come AFTER the shared
sections. The dialog doesn't read or write extra pages; the factory's widget
manages itself (the app connects to the dialog's `accepted`/`applied` signals).

Usage:
    dlg = PreferencesDialog(
        [filenames_section(), language_section(include_enabled=True)],
        MyBackend(), parent=self,
        extra_pages=[("Tools", lambda: ToolPathsWidget(self.settings))],
    )
    if dlg.exec():
        self.refresh_after(dlg.result_values())

    # Tools menu:
    MenuAction in standard_tools_items(preferences=...) or preferences_menu_action(slot)
"""

from __future__ import annotations

import re
from typing import Any, Callable, Sequence

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QPalette
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from redactor_common.core import labels, preferences
from redactor_common.core.preferences import PrefSection, PrefSpec, PreferencesBackend
from redactor_common.gui import standard_shortcuts as keys
from redactor_common.gui.menu_builder import MenuAction
from redactor_common.gui.standard_menus import KEY_PREFERENCES, ROLE_BY_KEY

# More pages than this switch from tabs to a list + stack (tabs wrap badly).
MAX_TABS = 6

_MNEMONIC = re.compile(r"&(?!&)(.)")
_INT_LIMIT = 2_147_483_647


def preferences_menu_action(slot: Callable[[], None]) -> MenuAction:
    """The canonical Preferences entry (Tools menu): label, Ctrl+, and the
    platform Preferences role, the same as standard_tools_items(preferences=)."""
    return MenuAction(
        KEY_PREFERENCES, labels.PREFERENCES, slot,
        shortcut=keys.PREFERENCES, role=ROLE_BY_KEY[KEY_PREFERENCES],
    )


class _Mnemonics:
    """Hands out one Alt+letter per piece of text, never reusing a letter."""

    def __init__(self, reserved: str = "") -> None:
        self.used = set(reserved.lower())

    def apply(self, text: str) -> str:
        existing = _MNEMONIC.search(text)
        if existing:
            self.used.add(existing.group(1).lower())
            return text
        for index, char in enumerate(text):
            if char.isalnum() and char.lower() not in self.used and char.isascii():
                self.used.add(char.lower())
                return text[:index].replace("&", "&&") + "&" + text[index:].replace("&", "&&")
        return text.replace("&", "&&")


class _Row:
    """One setting's control plus the pieces greyed together."""

    def __init__(self, spec: PrefSpec, control: QWidget, getter, setter, signal, widgets: list[QWidget]):
        self.spec = spec
        self.control = control
        self.get = getter
        self.set = setter
        self.signal = signal
        self.widgets = widgets  # label (if any) + container: enabled/disabled together


class PreferencesDialog(QDialog):
    applied = pyqtSignal(dict)  # emitted after each write with just the keys written

    def __init__(
        self,
        sections: Sequence[PrefSection],
        backend: PreferencesBackend,
        parent: QWidget | None = None,
        *,
        title: str = "Preferences",
        extra_pages: Sequence[tuple[str, Callable[[], QWidget]]] = (),
    ) -> None:
        super().__init__(parent)
        preferences.validate_sections(sections)
        self.setWindowTitle(title)
        self._sections = list(sections)
        self._backend = backend
        self._rows: dict[str, _Row] = {}
        self._baseline: dict[str, Any] = {}
        self._written: dict[str, Any] = {}
        self._page_keys: list[list[str]] = []  # per page: its setting keys ([] for extra pages)

        # Reserve letters the button box and the Reset button use.
        mnemonics = _Mnemonics(reserved="r")
        page_widgets: list[tuple[str, QWidget]] = []
        for section in self._sections:
            page_widgets.append((section.title, self._build_page(section, mnemonics)))
            self._page_keys.append([spec.key for spec in section.specs])
        for page_title, factory in extra_pages:
            page_widgets.append((page_title, factory()))
            self._page_keys.append([])

        if len(page_widgets) > MAX_TABS:
            self._tabs: QTabWidget | None = None
            self._list: QListWidget | None = QListWidget()
            self._stack: QStackedWidget | None = QStackedWidget()
            self._list.setMaximumWidth(200)
            for page_title, page in page_widgets:
                self._list.addItem(page_title)
                self._stack.addWidget(page)
            self._list.currentRowChanged.connect(self._stack.setCurrentIndex)
            self._list.setCurrentRow(0)  # before the reset button exists, so no slot hears it
            self._list.currentRowChanged.connect(lambda _i: self._update_reset_enabled())
            self._list.setAccessibleName("Preferences pages")
            body = QHBoxLayout()
            body.addWidget(self._list)
            body.addWidget(self._stack, 1)
        else:
            self._list = None
            self._stack = None
            self._tabs = QTabWidget()
            for page_title, page in page_widgets:
                self._tabs.addTab(page, mnemonics.apply(page_title))
            self._tabs.currentChanged.connect(lambda _i: self._update_reset_enabled())
            body = QHBoxLayout()
            body.addWidget(self._tabs)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply
        )
        self.reset_button = QPushButton("&Reset to Defaults")
        self.reset_button.setToolTip("Put every setting on this page back to its default (nothing is saved until OK or Apply).")
        box.addButton(self.reset_button, QDialogButtonBox.ButtonRole.ResetRole)
        self.ok_button = box.button(QDialogButtonBox.StandardButton.Ok)
        self.apply_button = box.button(QDialogButtonBox.StandardButton.Apply)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        self.apply_button.clicked.connect(self.apply)
        self.reset_button.clicked.connect(lambda: self.reset_page(self.current_page()))
        self._button_box = box

        layout = QVBoxLayout(self)
        layout.addLayout(body, 1)
        layout.addWidget(box)

        self._reload_from_backend()
        for row in self._rows.values():
            row.signal.connect(self._on_edited)
        self._refresh_state()
        self.resize(max(self.sizeHint().width(), 560), max(self.sizeHint().height(), 320))

    # -- building ---------------------------------------------------

    def _build_page(self, section: PrefSection, mnemonics: _Mnemonics) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        if section.description:
            intro = QLabel(section.description)
            intro.setWordWrap(True)
            outer.addWidget(intro)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        outer.addLayout(form)
        outer.addStretch(1)

        for spec in section.specs:
            row = self._make_row(spec)
            self._rows[spec.key] = row
            container = QWidget()
            box = QVBoxLayout(container)
            box.setContentsMargins(0, 0, 0, 6)
            box.setSpacing(2)
            if spec.kind == "bool":
                row.control.setText(mnemonics.apply(spec.label))
                box.addWidget(row.control)
                label = None
            else:
                label = QLabel(mnemonics.apply(spec.label) + ":")
                label.setBuddy(row.control if spec.kind != "path" else row.control.findChild(QLineEdit))
                box.addWidget(row.control)
            help_text = spec.help
            if spec.restart_note:
                help_text = (help_text + " " + spec.restart_note).strip()
            if help_text:
                help_label = QLabel(help_text)
                help_label.setWordWrap(True)
                help_label.setIndent(4 if spec.kind != "bool" else 22)
                palette = help_label.palette()
                palette.setColor(QPalette.ColorRole.WindowText, palette.color(QPalette.ColorRole.PlaceholderText))
                help_label.setPalette(palette)
                box.addWidget(help_label)
                row.control.setAccessibleDescription(help_text)
            row.control.setAccessibleName(spec.label)
            if label is None:
                form.addRow(container)
                row.widgets = [container]
            else:
                form.addRow(label, container)
                row.widgets = [label, container]
        return page

    def _make_row(self, spec: PrefSpec) -> _Row:
        kind = spec.kind
        if kind == "bool":
            check = QCheckBox()
            return _Row(spec, check, check.isChecked, check.setChecked, check.toggled, [])
        if kind == "int":
            spin = QSpinBox()
            spin.setRange(
                int(spec.minimum) if spec.minimum is not None else -_INT_LIMIT,
                int(spec.maximum) if spec.maximum is not None else _INT_LIMIT,
            )
            return _Row(spec, spin, spin.value, spin.setValue, spin.valueChanged, [])
        if kind == "float":
            dspin = QDoubleSpinBox()
            dspin.setDecimals(2)
            dspin.setSingleStep(0.1)
            dspin.setRange(
                float(spec.minimum) if spec.minimum is not None else -1e9,
                float(spec.maximum) if spec.maximum is not None else 1e9,
            )
            return _Row(spec, dspin, dspin.value, dspin.setValue, dspin.valueChanged, [])
        if kind == "choice":
            combo = QComboBox()
            for value, label in spec.choice_pairs():
                combo.addItem(label, value)
            return _Row(
                spec, combo,
                lambda c=combo: c.currentData(),
                lambda v, c=combo: c.setCurrentIndex(max(0, c.findData(v))),
                combo.currentIndexChanged, [],
            )
        if kind == "language":
            return self._make_language_row(spec)
        edit = QLineEdit()
        if kind == "str":
            return _Row(spec, edit, edit.text, edit.setText, edit.textChanged, [])
        return self._make_path_row(spec, edit)

    @staticmethod
    def _make_language_row(spec: PrefSpec) -> _Row:
        combo = QComboBox()
        combo.setEditable(True)
        for code, name in spec.choice_pairs():
            combo.addItem(f"{name} ({code})", code)

        def get() -> str:
            text = combo.currentText()
            index = combo.findText(text)
            return str(combo.itemData(index)) if index >= 0 else text.strip()

        def set_(value: str) -> None:
            index = combo.findData(value)
            if index >= 0:
                combo.setCurrentIndex(index)
            else:
                combo.setEditText(value)

        return _Row(spec, combo, get, set_, combo.editTextChanged, [])

    @staticmethod
    def _make_path_row(spec: PrefSpec, edit: QLineEdit) -> _Row:
        holder = QWidget()
        line = QHBoxLayout(holder)
        line.setContentsMargins(0, 0, 0, 0)
        line.addWidget(edit, 1)
        browse = QPushButton("Browse…")
        line.addWidget(browse)

        def pick() -> None:
            start = edit.text().strip()
            if spec.path_mode == "folder":
                chosen = QFileDialog.getExistingDirectory(holder, spec.label, start)
            else:
                chosen, _filter = QFileDialog.getOpenFileName(holder, spec.label, start)
            if chosen:
                edit.setText(chosen)

        browse.clicked.connect(pick)
        row = _Row(spec, holder, edit.text, edit.setText, edit.textChanged, [])
        row.edit = edit  # type: ignore[attr-defined]
        row.browse = browse  # type: ignore[attr-defined]
        holder.setFocusProxy(edit)
        return row

    # -- values -----------------------------------------------------

    def _reload_from_backend(self) -> None:
        for key, row in self._rows.items():
            value = preferences.coerce(row.spec, self._backend.get(key))
            self._baseline[key] = value
            self._set_control(row, value)

    @staticmethod
    def _set_control(row: _Row, value: Any) -> None:
        blocked = row.control.blockSignals(True)
        try:
            row.set(value)
        finally:
            row.control.blockSignals(blocked)
        # Compound controls (a path row) emit from an inner widget; also
        # silence nothing else: callers refresh state themselves.

    def value(self, key: str) -> Any:
        """The control's current value for `key`, coerced to the spec's type."""
        row = self._rows[key]
        return preferences.coerce(row.spec, row.get())

    def set_value(self, key: str, value: Any) -> None:
        """Programmatic edit (as if the user changed the control)."""
        self._rows[key].set(preferences.coerce(self._rows[key].spec, value))

    def changed_values(self) -> dict[str, Any]:
        """key -> value for every setting that differs from what is stored."""
        return {key: self.value(key) for key in self._rows if self.value(key) != self._baseline[key]}

    def result_values(self) -> dict[str, Any]:
        """Everything this dialog has written so far (latest value per key)."""
        return dict(self._written)

    def apply(self) -> None:
        """Writes the changed keys (one set_many call) and rebases on them."""
        changed = self.changed_values()
        if changed:
            self._backend.set_many(dict(changed))
            self._baseline.update(changed)
            self._written.update(changed)
            self.applied.emit(dict(changed))
        self._refresh_state()

    def accept(self) -> None:
        self.apply()
        super().accept()

    # -- pages / reset -----------------------------------------------

    def page_titles(self) -> list[str]:
        if self._tabs is not None:
            return [_MNEMONIC.sub(r"\1", self._tabs.tabText(i)) for i in range(self._tabs.count())]
        assert self._list is not None
        return [self._list.item(i).text() for i in range(self._list.count())]

    def current_page(self) -> int:
        return self._tabs.currentIndex() if self._tabs is not None else self._list.currentRow()

    def set_current_page(self, index: int) -> None:
        if self._tabs is not None:
            self._tabs.setCurrentIndex(index)
        else:
            self._list.setCurrentRow(index)

    def reset_page(self, index: int) -> None:
        """Puts the page's controls on their defaults. Saves nothing: OK or
        Apply does, like any other edit."""
        for key in self._page_keys[index]:
            row = self._rows[key]
            row.set(preferences.coerce(row.spec, row.spec.default))
        self._refresh_state()

    def _update_reset_enabled(self) -> None:
        index = self.current_page()
        self.reset_button.setEnabled(0 <= index < len(self._page_keys) and bool(self._page_keys[index]))

    # -- state --------------------------------------------------------

    def _on_edited(self, *_args) -> None:
        self._refresh_state()

    def _refresh_state(self) -> None:
        # Two passes in spec order settle a chain (c depends on b depends on a)
        # even when a dependent is declared before the setting it needs.
        for _pass in range(2):
            for row in self._rows.values():
                target = row.spec.depends_on
                if target is None:
                    continue
                parent_row = self._rows[target]
                enabled = bool(parent_row.get()) and parent_row.widgets[-1].isEnabled()
                for widget in row.widgets:
                    widget.setEnabled(enabled)
        self.apply_button.setEnabled(bool(self.changed_values()))
        self._update_reset_enabled()

    def is_row_enabled(self, key: str) -> bool:
        row = self._rows[key]
        return row.widgets[-1].isEnabled()

    def control(self, key: str) -> QWidget:
        """The widget behind `key` (for an app test or custom focus)."""
        return self._rows[key].control

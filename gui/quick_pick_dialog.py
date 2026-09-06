"""
redactor_common/gui/quick_pick_dialog.py

A searchable list-picker popup for a field's "+" quick-pick button --
replaces a flat QMenu, which becomes genuinely unusable once its list
grows past a screenful (a real complaint from a cbzredactor session:
"the genre list gets too long to see the apply button", after enough
custom genres had been added via Settings > Add/Remove Genres... that
the plain QMenu -- with no search, no scroll affordance beyond the
OS's own tiny scroll arrows, and no fixed size -- started overflowing
the screen and, depending on where its anchor button sat, could even
render up over other UI).

A QDialog fixes this at the root: a fixed, reasonable size regardless
of how many entries exist, a real internally-scrolling QListWidget
(never overflows the screen), a filter box to narrow a long list
instantly instead of hunting through it, and an OK/Cancel row that's
always visible below the list no matter how it's scrolled.

Parametrized via callables, same pattern as manage_list_dialog.py's
ManageListDialog: `load_entries_fn` is called fresh each time the
dialog opens (and again after Add Custom, if provided), so a change
made elsewhere (e.g. a custom genre added in a *different* dialog)
never needs a restart to show up here.
"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)


class QuickPickDialog(QDialog):
    def __init__(
        self,
        title: str,
        load_entries_fn: Callable[[], list[tuple[str, str]]],
        multi_select: bool,
        add_custom_fn: Optional[Callable[["QuickPickDialog"], None]] = None,
        parent=None,
    ):
        """
        load_entries_fn: () -> [(key, display_text), ...], called fresh
            every time the list needs (re)populating.
        multi_select: True shows a checkbox per row and lets several be
            picked at once (e.g. Genre, a comma-separated field where
            picking several makes sense in one go); False is a plain
            single-selection list where double-clicking a row also
            accepts the dialog immediately (e.g. Language, which
            replaces the field outright -- picking a second one
            wouldn't mean anything).
        add_custom_fn: optional (dialog) -> None; runs whatever
            prompt(s) are needed and saves the new entry itself (via
            app_settings), same shape as ManageListDialog's
            add_dialog_fn. The list is refreshed automatically
            afterward -- a just-added entry shows up right away without
            needing to reopen the dialog.
        """
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(320, 420)
        self._load_entries_fn = load_entries_fn
        self._multi_select = multi_select
        self._add_custom_fn = add_custom_fn
        self._keys_by_row: list[str] = []

        layout = QVBoxLayout(self)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter…")
        self.filter_edit.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter_edit)

        self.list_widget = QListWidget()
        if not multi_select:
            self.list_widget.itemDoubleClicked.connect(lambda _item: self.accept())
        layout.addWidget(self.list_widget, 1)

        btn_row = QHBoxLayout()
        if add_custom_fn is not None:
            add_btn = QPushButton("Add Custom…")
            add_btn.clicked.connect(self._on_add_custom)
            btn_row.addWidget(add_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Add Selected" if multi_select else "Use Selected"
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.filter_edit.setFocus()
        self._refresh()

    def _refresh(self) -> None:
        self.list_widget.clear()
        self._keys_by_row = []
        for key, display_text in self._load_entries_fn():
            item = QListWidgetItem(display_text)
            if self._multi_select:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked)
            self.list_widget.addItem(item)
            self._keys_by_row.append(key)
        self._apply_filter(self.filter_edit.text())

    def _apply_filter(self, text: str) -> None:
        query = text.strip().casefold()
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            item.setHidden(bool(query) and query not in item.text().casefold())

    def _on_add_custom(self) -> None:
        self._add_custom_fn(self)
        self._refresh()

    def selected_keys(self) -> list[str]:
        """The key(s) for whatever's checked (multi-select) or
        currently selected (single-select) -- 0 or 1 items in the
        single-select case."""
        if self._multi_select:
            return [
                self._keys_by_row[row]
                for row in range(self.list_widget.count())
                if self.list_widget.item(row).checkState() == Qt.CheckState.Checked
            ]
        row = self.list_widget.currentRow()
        return [self._keys_by_row[row]] if row >= 0 else []

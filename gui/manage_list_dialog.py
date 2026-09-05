"""
redactor_common/gui/manage_list_dialog.py

A small reusable Add/Remove dialog for a Settings-menu management
screen over a "hideable default list plus user-editable custom list"
(e.g. Add/Remove Genres, Add/Remove Languages). Both built-in defaults
and custom entries can be removed -- removing a default doesn't delete
it, just hides it from the active list (a "(built-in)" tag marks it as
such), and "Restore Hidden Defaults" brings back everything hidden
this way in one step. Parametrized via callables rather than tied to
one list's shape -- entries are just (key, display_text) pairs, so it
works for genres (key == the genre name itself), languages (key ==
the ISO code, display_text includes the human name), or any project's
own equivalent alike.

Promoted from epubredactor's original version (already fully generic,
no epub-specific code) once cbzredactor needed the exact same
Genre/Language management pattern.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

_KIND_DEFAULT = "default"
_KIND_CUSTOM = "custom"


class ManageListDialog(QDialog):
    def __init__(
        self,
        title: str,
        load_defaults_fn,
        load_custom_fn,
        add_dialog_fn,
        remove_custom_fn,
        hide_default_fn,
        restore_defaults_fn,
        parent=None,
    ):
        """
        load_defaults_fn: () -> [(key, display_text), ...] for currently-VISIBLE
            built-in entries (already excluding hidden ones), called fresh each refresh
        load_custom_fn: () -> [(key, display_text), ...] for user-added entries, called fresh each refresh
        add_dialog_fn: (parent_widget) -> None; runs whatever prompt(s) are needed and saves via app_settings itself
        remove_custom_fn: (key) -> None; permanently removes a custom entry
        hide_default_fn: (key) -> None; hides a built-in default from the active list (not a deletion)
        restore_defaults_fn: () -> None; un-hides every previously-hidden default
        """
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(420, 480)
        self._load_defaults_fn = load_defaults_fn
        self._load_custom_fn = load_custom_fn
        self._add_dialog_fn = add_dialog_fn
        self._remove_custom_fn = remove_custom_fn
        self._hide_default_fn = hide_default_fn
        self._restore_defaults_fn = restore_defaults_fn

        layout = QVBoxLayout(self)
        info = QLabel(
            "Built-in defaults are marked \"(built-in)\". Removing one just hides it "
            "from this list -- it's not deleted, and \"Restore Hidden Defaults\" brings "
            "back everything hidden this way. Custom entries you remove are gone for good."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget, 1)

        btn_row = QHBoxLayout()
        self.add_btn = QPushButton("Add…")
        self.add_btn.clicked.connect(self._on_add)
        btn_row.addWidget(self.add_btn)
        self.remove_btn = QPushButton("Remove Selected")
        self.remove_btn.clicked.connect(self._on_remove)
        btn_row.addWidget(self.remove_btn)
        btn_row.addStretch(1)
        self.restore_btn = QPushButton("Restore Hidden Defaults")
        self.restore_btn.clicked.connect(self._on_restore_defaults)
        btn_row.addWidget(self.restore_btn)
        layout.addLayout(btn_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self._refresh()

    def _refresh(self) -> None:
        self.list_widget.clear()
        for key, display_text in self._load_defaults_fn():
            item = QListWidgetItem(f"{display_text}  (built-in)")
            item.setData(Qt.ItemDataRole.UserRole, (_KIND_DEFAULT, key))
            self.list_widget.addItem(item)
        for key, display_text in self._load_custom_fn():
            item = QListWidgetItem(display_text)
            item.setData(Qt.ItemDataRole.UserRole, (_KIND_CUSTOM, key))
            self.list_widget.addItem(item)

    def _on_add(self) -> None:
        self._add_dialog_fn(self)
        self._refresh()

    def _on_remove(self) -> None:
        item = self.list_widget.currentItem()
        if item is None:
            QMessageBox.information(self, "Nothing to remove", "Select an entry to remove.")
            return
        kind, key = item.data(Qt.ItemDataRole.UserRole)
        if kind == _KIND_DEFAULT:
            self._hide_default_fn(key)
        else:
            self._remove_custom_fn(key)
        self._refresh()

    def _on_restore_defaults(self) -> None:
        self._restore_defaults_fn()
        self._refresh()

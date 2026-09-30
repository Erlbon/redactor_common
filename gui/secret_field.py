"""
redactor_common/gui/secret_field.py

A settings-dialog row for one secret (API key, password): a password-mode
line edit that never shows the stored value, a Remove button, and a label
naming where the secret lives (see core/secret_store). Generic: the app
slug and secret name are injected.

Semantics: an empty edit means "keep what is stored"; typing a value
means "replace it"; Remove marks it for deletion. Nothing touches the
store until the dialog calls apply() (on OK), so Cancel loses nothing.

Usage:
    self.key_field = SecretField("cbzredactor", "comicvine_api_key",
                                 env_var="COMICVINE_API_KEY")
    form.addRow("Comic Vine API key:", self.key_field)
    ...on accept:
    try:
        self.key_field.apply()
    except SecretStoreUnavailable:
        ...offer the unencrypted fallback, then apply(True)
"""

from __future__ import annotations

import sys
from typing import Optional

from PyQt6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from redactor_common.core import secret_store


def _keyring_label() -> str:
    if sys.platform == "win32":
        return "Windows Credential Manager"
    if sys.platform == "darwin":
        return "macOS Keychain"
    return "the system keyring"


def source_description(source: str) -> str:
    return {
        secret_store.SOURCE_KEYRING: f"Stored in {_keyring_label()}",
        secret_store.SOURCE_ENV: "Taken from an environment variable",
        secret_store.SOURCE_FILE: "Stored in an UNENCRYPTED file",
    }.get(source, "Not set")


class SecretField(QWidget):
    def __init__(self, app: str, name: str, env_var: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.app = app
        self.name = name
        self.env_var = env_var
        self._remove_pending = False

        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._on_remove)
        self.source_label = QLabel()

        row = QHBoxLayout()
        row.addWidget(self.edit, 1)
        row.addWidget(self.remove_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(row)
        layout.addWidget(self.source_label)
        self.edit.textChanged.connect(self._on_text_changed)
        self.refresh()

    def __repr__(self) -> str:  # never the typed or stored value
        return f"SecretField(app={self.app!r}, name={self.name!r})"

    def refresh(self) -> None:
        """Re-read the store and reset pending edits."""
        self._remove_pending = False
        self.edit.clear()
        source = secret_store.secret_source(self.app, self.name, self.env_var)
        has = source != secret_store.SOURCE_NONE
        self.edit.setPlaceholderText(
            f"•••••• ({source_description(source).lower()}; "
            "type to change)" if has else "Not set"
        )
        self.source_label.setText(source_description(source))
        # An env var can't be removed from here.
        self.remove_button.setEnabled(has and source != secret_store.SOURCE_ENV)

    def new_value(self) -> str:
        """What the user typed ("" = keep the stored secret)."""
        return self.edit.text()

    def removal_pending(self) -> bool:
        return self._remove_pending

    def _on_remove(self) -> None:
        self._remove_pending = True
        self.edit.clear()
        self.edit.setPlaceholderText("Will be removed when you press OK")
        self.source_label.setText("Removal pending")
        self.remove_button.setEnabled(False)

    def _on_text_changed(self, text: str) -> None:
        if text and self._remove_pending:  # typing a replacement cancels removal
            self._remove_pending = False
            self.source_label.setText("Will be replaced when you press OK")

    def apply(self, allow_unencrypted_fallback: Optional[bool] = None) -> bool:
        """Write pending changes; True if the store changed. Raises
        SecretStoreUnavailable (nothing lost, edit kept) so the dialog can
        offer the fallback and call again."""
        changed = False
        if self._remove_pending:
            secret_store.delete_secret(self.app, self.name)
            changed = True
        elif self.edit.text():
            secret_store.set_secret(self.app, self.name, self.edit.text(), allow_unencrypted_fallback)
            changed = True
        if changed:
            self.refresh()
        return changed

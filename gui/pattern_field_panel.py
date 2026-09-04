"""
redactor_common/gui/pattern_field_panel.py

The %field% pattern-editing UX shared by Rename/Export by Pattern and
Parse Filename -> Metadata: a "▼" button showing a menu of recently
used patterns, an always-visible clickable list of the same recent
patterns right below the field (added in epub v51 -- not everyone
thinks to click a small button, so both are kept), and a clickable
side panel of individual %field% placeholder codes that insert at the
cursor on double-click (added in epub v54). The video project never
had any of this; it's promoted here as shared UX rather than rebuilt
per project.

This module provides the pieces as attachable widgets/behavior around
a QLineEdit the caller creates and owns -- it doesn't dictate dialog
layout, since Rename/Export and Parse Filename arrange them slightly
differently (and future projects may too).

Usage:
    self.pattern_edit = QLineEdit(starting_pattern)
    self.pattern_edit.textChanged.connect(self._refresh_preview)

    panel = PatternFieldPanel(
        pattern_edit=self.pattern_edit,
        placeholders=PLACEHOLDERS,  # list[(field_key, label)]
        history=app_settings.load_pattern_history(),
        parent=self,
    )
    pattern_row.addWidget(panel.recent_button)      # next to the QLineEdit
    layout.addWidget(panel.recent_list)              # below the pattern row
    right_column.addWidget(panel.placeholder_list)   # side column
"""

from __future__ import annotations

from PyQt6.QtWidgets import QListWidget, QListWidgetItem, QMenu, QPushButton, QWidget

RECENT_BUTTON_GLYPH = "\u25bc"
RECENT_BUTTON_WIDTH = 26
RECENT_LIST_MAX_HEIGHT = 90


class PatternFieldPanel:
    def __init__(
        self,
        pattern_edit,
        placeholders: list[tuple[str, str]],
        history: list[str],
        parent: QWidget | None = None,
    ):
        self.pattern_edit = pattern_edit
        self.placeholders = placeholders
        self.history = history

        self.recent_button = QPushButton(RECENT_BUTTON_GLYPH, parent)
        self.recent_button.setMaximumWidth(RECENT_BUTTON_WIDTH)
        self.recent_button.setToolTip("Choose from recently used patterns")
        self.recent_button.clicked.connect(self._show_recent_menu)

        self.recent_list = QListWidget(parent)
        self.recent_list.setMaximumHeight(RECENT_LIST_MAX_HEIGHT)
        self.recent_list.itemClicked.connect(self._on_recent_list_clicked)
        self._populate_recent_list()

        self.placeholder_list = QListWidget(parent)
        for key, label in placeholders:
            item = QListWidgetItem(f"%{key}%  \u2014  {label}")
            item.setData(256, f"%{key}%")  # Qt.ItemDataRole.UserRole == 256
            self.placeholder_list.addItem(item)
        self.placeholder_list.itemDoubleClicked.connect(self._on_placeholder_double_clicked)

    def _populate_recent_list(self) -> None:
        self.recent_list.clear()
        for pattern in self.history:
            self.recent_list.addItem(QListWidgetItem(pattern))

    def _show_recent_menu(self) -> None:
        if not self.history:
            return
        menu = QMenu(self.recent_button)
        for pattern in self.history:
            action = menu.addAction(pattern)
            action.triggered.connect(lambda _checked=False, p=pattern: self.pattern_edit.setText(p))
        menu.exec(self.recent_button.mapToGlobal(self.recent_button.rect().bottomLeft()))

    def _on_recent_list_clicked(self, item: QListWidgetItem) -> None:
        self.pattern_edit.setText(item.text())

    def _on_placeholder_double_clicked(self, item: QListWidgetItem) -> None:
        token = item.data(256)
        self.pattern_edit.insert(token)
        self.pattern_edit.setFocus()

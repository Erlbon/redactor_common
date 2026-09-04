"""
redactor_common/gui/column_menu.py

Shared column-header right-click menu: an inline show/hide checklist
for every current column, promoted from video's version (the most
complete of the three -- mp3 has no column-visibility system at all
yet, epub only offered a "Hide <this column>" quick action for the
column under the cursor). Also links to the fuller "Add/Remove
Columns..." dialog (column_settings_dialog.py) at the bottom, since
both read/write the exact same persisted hidden-columns state -- see
that dialog's own docstring.

Usage (field-name-based projects, i.e. anything using core/table_settings.py):
    from redactor_common.gui.column_menu import show_column_header_context_menu

    def _on_column_header_context_menu(self, pos):
        show_column_header_context_menu(
            self, self.table, pos,
            column_order=self._column_order,
            label_lookup=COLUMN_LABEL_LOOKUP,
            protected_columns=PROTECTED_COLUMNS,
            hidden_fields=sanitize_hidden_fields(self._load_hidden_fields()),
            is_visible=is_column_visible,
            on_toggle=self._on_column_visibility_toggled,
            open_column_settings_dialog=self.open_column_settings_dialog,
        )

Column keys are deliberately untyped (`Any`/`Hashable`, not `str`) -- the
logic here is just dict/set lookups, so it works equally well for a
project still on plain column-index keys (epub, as of this writing --
core/table_settings.py's field-name scheme was written to replace that,
but epub hasn't been migrated onto it yet) as for the field-name keys
table_settings.py itself uses.
"""

from __future__ import annotations

from typing import Callable, Hashable

from PyQt6.QtWidgets import QMenu, QTableWidget, QWidget


def show_column_header_context_menu(
    window: QWidget,
    table: QTableWidget,
    pos,
    column_order: list[Hashable],
    label_lookup: dict[Hashable, str],
    protected_columns: frozenset[Hashable],
    hidden_fields: set[Hashable],
    is_visible: Callable[[Hashable, set[Hashable]], bool],
    on_toggle: Callable[[Hashable, bool], None],
    open_column_settings_dialog: Callable[[], None] | None = None,
) -> None:
    """`is_visible` is redactor_common.core.table_settings.is_column_visible
    (or a project's PROTECTED_COLUMNS-bound wrapper around it, same
    shape) -- called as is_visible(key, hidden_fields).
    `on_toggle(key, checked)` is the project's own persist +
    table-rebuild callback. Protected columns (row-identity anchors
    like "filename") are never offered here -- hiding the one column
    that makes rows identifiable is never something a right-click
    should be able to do by accident.
    """
    header = table.horizontalHeader()
    menu = QMenu(window)

    for key in column_order:
        if key in protected_columns:
            continue
        label = label_lookup.get(key, str(key))
        action = menu.addAction(label)
        action.setCheckable(True)
        action.setChecked(is_visible(key, hidden_fields))
        action.toggled.connect(lambda checked, k=key: on_toggle(k, checked))

    if open_column_settings_dialog:
        if column_order:
            menu.addSeparator()
        menu.addAction("Add/Remove Columns…", open_column_settings_dialog)

    menu.exec(header.mapToGlobal(pos))

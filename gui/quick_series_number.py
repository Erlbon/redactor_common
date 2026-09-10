"""
redactor_common/gui/quick_series_number.py

The table right-click's quick version of Number Series: just prompts
for a starting value (no step, no preview) and numbers the selected
items +1 per row from there, in current table order. Promoted from
epub's quick_number_series() -- the whole point of this one, versus
redactor_common.gui.auto_numbering_dialog's full dialog (field picker,
step, zero-padding, preview), is that it's the "start here, count up
by one" one-prompt shortcut for the single field a project's own
right-click menu wires it to. For anything beyond that -- a different
step, non-default padding, or a look at what's changing before it
does -- that full dialog (or, for a fractional/decimal use case like
this one, a bespoke dialog built on
redactor_common.core.series_numbering directly) is the answer, not
this.

Usage (inside a project's own extra_items callable for
redactor_common.gui.context_menu.show_table_context_menu):
    from redactor_common.gui.quick_series_number import prompt_and_generate_series_numbers

    def _quick_number(items):
        values = prompt_and_generate_series_numbers(self, len(items), field_label="Track #")
        if values is None:
            return
        self._push_undo("Number Tracks", items)
        for item, new_value in zip(items, values):
            item.apply_tags({"track": new_value})
        self._rebuild_table()
"""

from __future__ import annotations

from PyQt6.QtWidgets import QInputDialog, QWidget

from redactor_common.core.series_numbering import DEFAULT_START, generate_series_numbers


def prompt_and_generate_series_numbers(
    parent: QWidget,
    count: int,
    field_label: str = "Starting value",
    title: str = "Number Series",
    step: str = "1",
    default_start: str = DEFAULT_START,
) -> list[str] | None:
    """Shows the one-field "starting value" prompt and returns `count`
    sequential values (decimal-capable -- "0.5", "3.5", etc. all work),
    or None if the user cancelled. Caller applies the result to
    whichever field/items it's numbering; this function only ever
    prompts and generates, it never touches app data."""
    start_text, ok = QInputDialog.getText(
        parent, title,
        f"{field_label} for {count} selected item(s) "
        f"(numbered in their current table order, +{step} per row):",
        text=default_start,
    )
    if not ok:
        return None
    return generate_series_numbers(count, start_text, step)

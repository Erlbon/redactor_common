"""
redactor_common/core/table_settings.py

Pure logic for merging a user's persisted column preferences (drag-
reordered order, hidden-via-context-menu set) against whatever the
current field list says should be shown.

Field-name-based (not index-based) deliberately: an index-based scheme
breaks silently the moment a field is added, removed, or reordered in
code, since a persisted "hide index 4" setting.ini entry now points at
a different field than the one the user actually hid. This is the
epub project's original weakness relative to the video project's
version -- this module is video's, promoted to the shared package.

Kept separate from any gui/main_window.py deliberately -- this is real
business logic (not "recreate a widget"), so it belongs somewhere
testable, following the same testable-core-plus-thin-GUI-wiring pattern
used throughout these projects. gui code calls these functions; it
should never reimplement this merging logic inline.
"""

from __future__ import annotations


def merge_column_order(
    persisted_order: list[str], current_fields: list[str]
) -> list[str]:
    """Reorder current_fields to match the user's persisted drag-order
    preference. Fields the user has never seen/ordered (e.g. because a
    filter just changed to show a field that wasn't visible before)
    keep their original relative order, appended after the fields that
    do have a known preferred position -- new fields don't jump to
    arbitrary positions just because they're unfamiliar to the
    persisted order.

    A persisted field no longer present in current_fields (e.g. the
    filter now hides it, or a project removed the field) is silently
    dropped rather than erroring -- the persisted order is a
    preference, not a hard requirement that every named field must
    exist right now.
    """
    ordered = [f for f in persisted_order if f in current_fields]
    remaining = [f for f in current_fields if f not in ordered]
    return ordered + remaining


def is_column_visible(
    field_name: str, hidden_fields: set[str], protected_columns: frozenset[str]
) -> bool:
    """A column is visible unless the user explicitly hid it AND it
    isn't protected. Protected columns (e.g. whatever field anchors
    row identity for a given project -- "filename" for the media
    tools) are always visible regardless of what's in hidden_fields.
    """
    if field_name in protected_columns:
        return True
    return field_name not in hidden_fields


def sanitize_hidden_fields(
    hidden_fields: set[str], protected_columns: frozenset[str]
) -> set[str]:
    """Strip any protected column out of a hidden-fields set before
    using or persisting it -- defends against a hand-edited or
    corrupted settings.ini claiming a protected column is hidden,
    rather than trusting that is_column_visible() is the only code
    path that will ever read this set.
    """
    return hidden_fields - protected_columns

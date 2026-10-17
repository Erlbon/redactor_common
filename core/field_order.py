"""
redactor_common/core/field_order.py

Pure logic for the user's order of the side-panel fields (drag a field by its
grip to move it). Saved BY FIELD KEY, never by index, so a field added or
removed in a later version cannot scramble a saved preference; see
redactor_common.gui.field_order for the widgets that use it.
"""

from __future__ import annotations

from typing import Iterable


def merge_field_order(saved: Iterable[str], defaults: list[str]) -> list[str]:
    """The saved order, applied to `defaults` (the field keys in their default order).

    * a saved key that no longer exists is dropped;
    * a default key the saved order has never seen (a field added in a later
      version) is inserted right after the nearest earlier default key, so it
      appears where the default order puts it relative to its neighbours (at
      the very start when nothing before it is known);
    * duplicates in `saved` count once.
    """
    known = set(defaults)
    result: list[str] = []
    for key in saved:
        if key in known and key not in result:
            result.append(key)
    for index, key in enumerate(defaults):
        if key in result:
            continue
        after = next((defaults[i] for i in range(index - 1, -1, -1) if defaults[i] in result), None)
        result.insert(result.index(after) + 1 if after is not None else 0, key)
    return result


def move_key(order: list[str], key: str, target_index: int) -> list[str]:
    """`order` with `key` moved so that it ends up at position `target_index` of the result
    (clamped to the list)."""
    if key not in order:
        return list(order)
    rest = [k for k in order if k != key]
    target_index = max(0, min(target_index, len(rest)))
    rest.insert(target_index, key)
    return rest


def merge_visible_order(saved: Iterable[str], defaults: list[str], new_visible: list[str]) -> list[str]:
    """The full order after the user rearranged only the VISIBLE fields (a content-type filter or
    hidden columns show a subset): the visible keys take, in their new sequence, the places the
    visible keys had in the full order; hidden keys keep their places."""
    full = merge_field_order(saved, defaults)
    wanted = [k for k in new_visible if k in full]
    slots = [i for i, k in enumerate(full) if k in set(wanted)]
    result = list(full)
    for slot, key in zip(slots, wanted):
        result[slot] = key
    return result


def encode_order(order: Iterable[str]) -> str:
    """Comma-separated keys (settings.ini friendly; field keys never contain a comma)."""
    return ",".join(k for k in order if k)


def decode_order(raw: str | None) -> list[str]:
    return [k.strip() for k in (raw or "").split(",") if k.strip()]

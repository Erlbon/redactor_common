"""
redactor_common/core/clear_fields.py

The Qt-free logic of "Clear Fields": what WOULD be emptied on the selected items, grouped the way the user chooses
(descriptive fields, source ids, the embedded picture, ...). The app supplies the accessors, so the logic knows nothing
about any data model (same accessor style as gui/case_conversion_dialog.py):

    groups = [FieldGroup("descriptive", "Descriptive fields", "title, description, genre, cast..."),
              FieldGroup("sources", "Source IDs and links"),
              FieldGroup("type", "Content type", default_checked=False)]
    plan = plan_clear(files, groups,
                      get_fields=lambda f: {"title": f.title, "tmdb_id": f.tmdb_id, ...},   # field -> current value
                      group_fields={"descriptive": ["title", ...], "sources": ["tmdb_id", ...], "type": ["content_type"]})
    plan.totals()                       # {group key: GroupTotal(values, items)} for the live counts of the check boxes
    for item_plan in plan.selected(["descriptive", "sources"]):   # only what the chosen groups would clear
        item_plan.item, item_plan.fields   # {field: current value}

A field counts only when it has a value (a blank one has nothing to clear). The app does the clearing itself, staged
like any other edit; this module only plans and describes.

A field named in no group is never cleared (scan stamps, technical fields and the like simply are not listed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence


@dataclass(frozen=True)
class FieldGroup:
    key: str
    label: str
    description: str = ""
    default_checked: bool = True


@dataclass(frozen=True)
class GroupTotal:
    values: int  # how many non-empty values the group would clear
    items: int   # in how many items


@dataclass
class ItemPlan:
    """What would be cleared on one item: {group key: {field: current value}} (non-empty fields only)."""

    item: Any
    by_group: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def fields(self) -> dict[str, str]:
        merged: dict[str, str] = {}
        for values in self.by_group.values():
            merged.update(values)
        return merged

    @property
    def count(self) -> int:
        return sum(len(values) for values in self.by_group.values())

    def __bool__(self) -> bool:
        return self.count > 0


def _text(value: Any) -> str:
    if value is None:
        return ""
    value_attr = getattr(value, "value", None)  # an Enum (a content type)
    return str(value_attr if isinstance(value_attr, str) else value).strip()


@dataclass
class ClearPlan:
    groups: list[FieldGroup]
    items: list[ItemPlan]

    def totals(self) -> dict[str, GroupTotal]:
        """Per group: how many values and in how many items (for the live counts next to the check boxes)."""
        result: dict[str, GroupTotal] = {}
        for group in self.groups:
            values = sum(len(p.by_group.get(group.key, {})) for p in self.items)
            items = sum(1 for p in self.items if p.by_group.get(group.key))
            result[group.key] = GroupTotal(values, items)
        return result

    def selected(self, group_keys: Iterable[str]) -> list[ItemPlan]:
        """The plans restricted to `group_keys`, only the items that still have something to clear. Order kept."""
        wanted = set(group_keys)
        out: list[ItemPlan] = []
        for plan in self.items:
            kept = {k: dict(v) for k, v in plan.by_group.items() if k in wanted and v}
            if kept:
                out.append(ItemPlan(plan.item, kept))
        return out

    def total_values(self, group_keys: Iterable[str]) -> int:
        return sum(p.count for p in self.selected(group_keys))


def plan_clear(
    items: Sequence[Any],
    groups: Sequence[FieldGroup],
    get_fields: Callable[[Any], Mapping[str, Any]],
    group_fields: Mapping[str, Sequence[str]],
) -> ClearPlan:
    """What clearing `groups` would remove from each of `items`. `get_fields(item)` returns {field: current value}
    for every field the app can hold (anything that is not a string is turned into text; None and blank are empty);
    `group_fields[group.key]` lists the fields of a group. An item with nothing to clear in any group is still part
    of the plan (with an empty ItemPlan) so the counts can say "in 4 of 5 files"."""
    plans: list[ItemPlan] = []
    for item in items:
        current = get_fields(item)
        plan = ItemPlan(item)
        for group in groups:
            found = {}
            for name in group_fields.get(group.key, ()):
                text = _text(current.get(name))
                if text:
                    found[name] = text
            if found:
                plan.by_group[group.key] = found
        plans.append(plan)
    return ClearPlan(list(groups), plans)


def describe_fields(fields: Mapping[str, str], label: Callable[[str], str] | None = None, limit: int = 6) -> str:
    """'Title, Artist, Cast (+3 more)' for a preview cell: the field labels, the first `limit` of them."""
    names = [label(name) if label else name for name in fields]
    shown = ", ".join(names[:limit])
    return shown + (f" (+{len(names) - limit} more)" if len(names) > limit else "")

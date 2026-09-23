"""
redactor_common/core/managed_list.py

The model behind gui/manage_list_dialog.py: a list of built-in defaults
(individually hideable and restorable) plus user-added custom entries.
Genres are plain names (deduped case-insensitively); languages are
(code, display_name) pairs (deduped by code).

The dialog was already shared, but the storage-side merge/hide/add/
remove logic behind it had been written three times -- cbz and epub
each in gui/app_settings.py, mp3 in core/settings.py -- identical in
behavior, differing only in how the lists get persisted. That part
stays per project (QSettings vs configparser); these are the pure
list operations every backend calls. Pure logic, no Qt dependency.
"""

from __future__ import annotations

import json

Pair = tuple[str, str]


# -- plain names (genres) ---------------------------------------------------

def merge_names(defaults: list[str], custom: list[str]) -> list[str]:
    """Defaults first, then each custom entry not already present
    (case-insensitive); blank entries dropped."""
    seen = {name.lower() for name in defaults}
    result = list(defaults)
    for name in custom:
        name = name.strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        result.append(name)
    return result


def exclude_hidden_names(defaults: list[str], hidden: list[str]) -> list[str]:
    hidden_lower = {name.lower() for name in hidden}
    return [name for name in defaults if name.lower() not in hidden_lower]


def add_name(names: list[str], name: str) -> list[str]:
    """Returns `names` plus `name` unless already present
    (case-insensitive) or blank."""
    name = name.strip()
    if not name or any(n.lower() == name.lower() for n in names):
        return list(names)
    return [*names, name]


def remove_name(names: list[str], name: str) -> list[str]:
    return [n for n in names if n.lower() != name.lower()]


# -- (code, name) pairs (languages) ------------------------------------------

def merge_pairs(defaults: list[Pair], custom: list[Pair]) -> list[Pair]:
    """Defaults first, then each custom pair whose code isn't already
    present; pairs with a blank code or name dropped."""
    seen_codes = {code for code, _name in defaults}
    result = list(defaults)
    for code, name in custom:
        code, name = code.strip(), name.strip()
        if not code or not name or code in seen_codes:
            continue
        seen_codes.add(code)
        result.append((code, name))
    return result


def exclude_hidden_codes(defaults: list[Pair], hidden_codes: list[str]) -> list[Pair]:
    hidden = set(hidden_codes)
    return [(code, name) for code, name in defaults if code not in hidden]


def add_code(codes: list[str], code: str) -> list[str]:
    return list(codes) if code in codes else [*codes, code]


def add_pair(pairs: list[Pair], code: str, name: str) -> list[Pair]:
    """Adds (code, name), replacing any existing entry with that code."""
    code, name = code.strip(), name.strip()
    if not code or not name:
        return list(pairs)
    return [*remove_pair(pairs, code), (code, name)]


def remove_pair(pairs: list[Pair], code: str) -> list[Pair]:
    return [(c, n) for c, n in pairs if c != code]


# -- JSON (de)serialization, for a single-string settings value ---------------

def decode_names(raw: str) -> list[str]:
    """A JSON list of strings, tolerant of a missing/corrupt value."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    return [item for item in data if isinstance(item, str)] if isinstance(data, list) else []


def decode_pairs(raw: str) -> list[Pair]:
    """A JSON list of [code, name] lists, tolerant of a missing/corrupt
    value or malformed entries."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [
        (item[0], item[1])
        for item in data
        if isinstance(item, (list, tuple)) and len(item) >= 2
        and isinstance(item[0], str) and isinstance(item[1], str)
    ]


def encode_names(names: list[str]) -> str:
    return json.dumps(list(names))


def encode_pairs(pairs: list[Pair]) -> str:
    return json.dumps([[code, name] for code, name in pairs])

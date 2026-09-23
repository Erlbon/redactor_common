"""
redactor_common/core/pattern_history.py

Most-recently-used history for the Rename/Export and Parse Filename
pattern fields -- written four times before this (cbz and epub in
gui/app_settings.py, mp3 in core/settings.py, video in
core/filename_pattern.py), identical except for how the list got
serialized. Pure logic, no Qt dependency.

Serialization: a pattern can legitimately contain commas (e.g.
"%title%, %year%"), so a comma-joined list would corrupt on reload.
JSON (cbz/epub) and a \\x1f unit-separator join (video) are both safe;
decode_history() reads either, so a project can switch format without
losing a user's saved history.
"""

from __future__ import annotations

import json

DEFAULT_MAX_HISTORY = 15
UNIT_SEPARATOR = "\x1f"


def dedupe_and_trim(
    history: list[str], new_pattern: str, max_history: int = DEFAULT_MAX_HISTORY
) -> list[str]:
    """Moves `new_pattern` to the front of `history`, deduplicated and
    capped at `max_history`. A blank pattern leaves history unchanged."""
    new_pattern = (new_pattern or "").strip()
    if not new_pattern:
        return list(history)
    result = [p for p in history if p != new_pattern]
    result.insert(0, new_pattern)
    return result[:max_history]


def encode_history(history: list[str]) -> str:
    return json.dumps(list(history))


def decode_history(raw: str) -> list[str]:
    """Reads a JSON list, or falls back to a \\x1f-joined string."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [p for p in data if isinstance(p, str) and p]
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return [p for p in raw.split(UNIT_SEPARATOR) if p]

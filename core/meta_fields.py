"""
redactor_common/core/meta_fields.py

The common shape of what the read-only metadata readers return, so a file
manager (Dossier) or any Redactor app can show the facts inside a file
without knowing which reader produced them.

A reader returns a ``MetaResult``: a list of ``Field`` (key, label, value,
group) in display order, plus an ``error`` text when the file could not be
read at all. A reader never raises for a bad or missing file: a damaged
file is a result with an ``error``, not a crash.
"""
from __future__ import annotations

from dataclasses import dataclass, field

GROUP_TAGS = "Tags"
GROUP_CREDITS = "Credits"
GROUP_TECHNICAL = "Technical"
GROUP_CONTAINER = "Container"
GROUP_PAGES = "Pages"


@dataclass(frozen=True)
class Field:
    key: str                  # a stable name: "title", "artist", "video.1.codec"
    label: str                # what the user reads: "Title", "Artist"
    value: str                # always text; lists are joined with ", "
    group: str = GROUP_TAGS


@dataclass
class MetaResult:
    kind: str                                    # "audio", "comic", "video"
    fields: list = field(default_factory=list)
    error: str = ""

    def get(self, key: str, default: str = "") -> str:
        for f in self.fields:
            if f.key == key:
                return f.value
        return default

    def groups(self) -> list:
        """The group names in the order they first appear."""
        seen: list = []
        for f in self.fields:
            if f.group not in seen:
                seen.append(f.group)
        return seen


def add(fields: list, key: str, label: str, value, group: str = GROUP_TAGS) -> None:
    """Append a field when ``value`` has anything in it (empty text, None and empty lists are left out)."""
    if value is None:
        return
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v) for v in value if str(v).strip())
    text = str(value).strip()
    if text:
        fields.append(Field(key, label, text, group))


def duration_text(seconds) -> str:
    """``3:07`` or ``1:02:09`` for a number of seconds; empty when it is not a usable number."""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return ""
    if total < 0:
        return ""
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"

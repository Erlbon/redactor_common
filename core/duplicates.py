"""
redactor_common/core/duplicates.py

The data model behind the shared "Find Duplicates" review dialog
(gui/duplicates_dialog.py), Qt-free: what a group of duplicate
candidates looks like, how confident each kind of match is, and how a
user's "these are not duplicates" decision is remembered.

Policy (Erlend, 2026-10-01): duplicates are NOT errors. The same book can
legitimately exist twice (two editions, a wrongly assigned ISBN), and the
same recording on several releases is normal. So everything here serves
a REVIEW aid -- candidates with the reason they matched, never a verdict:

- every group carries a `tier` (how strong the evidence is) and a plain
  `reason` sentence ("same ISBN", "same title and artist, lengths within
  2 s"), so the user sees WHY two things were grouped;
- nothing is selected for deletion by default;
- the user can mark a group "not duplicates" and it stops appearing
  (DismissStore).

Apps do the finding (what makes two videos/books/tracks alike is theirs);
they hand over `DuplicateGroup`s. `DuplicateMember.item` is whatever the
app wants back (its own file object), `fields` the display strings per
column key, and `fingerprint` a CONTENT-based identity (a hash of the
file's bytes, or of its audio/frame data) so a dismissal survives a
rename or a move -- a path would not.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from redactor_common.core.os_utils import replace_with_retry

# --- Tiers --------------------------------------------------------------

TIER_IDENTICAL = "identical"   # byte-for-byte / content-hash equal
TIER_STRONG = "strong"         # a unique identifier or near-certain match agrees
TIER_POSSIBLE = "possible"     # several descriptive fields agree
TIER_WEAK = "weak"             # only something loose agrees (a similar title)

# Strongest first. The index is the sort rank.
TIER_ORDER = (TIER_IDENTICAL, TIER_STRONG, TIER_POSSIBLE, TIER_WEAK)

TIER_LABELS = {
    TIER_IDENTICAL: "Identical",
    TIER_STRONG: "Strong match",
    TIER_POSSIBLE: "Possible match",
    TIER_WEAK: "Weak match",
}

TIER_DESCRIPTIONS = {
    TIER_IDENTICAL: "The contents are exactly the same.",
    TIER_STRONG: "A unique identifier or near-certain evidence agrees.",
    TIER_POSSIBLE: "Several details agree, but they may still be different things.",
    TIER_WEAK: "Only something loose agrees; most of these will be false alarms.",
}


def tier_label(tier: str) -> str:
    """The plain-language name of a tier (an unknown tier is shown as-is)."""
    return TIER_LABELS.get(tier, str(tier))


def tier_strength(tier: str) -> int:
    """Sort rank: 0 for the strongest tier. An unknown tier ranks after
    every known one."""
    try:
        return TIER_ORDER.index(tier)
    except ValueError:
        return len(TIER_ORDER)


# --- Groups -------------------------------------------------------------


@dataclass
class DuplicateMember:
    """One file in a group. `item` is handed back to the app unchanged;
    `fields` maps column key -> display text; `fingerprint` is a stable
    content identity (see the module docstring)."""
    item: Any
    path: str
    fields: dict[str, str] = field(default_factory=dict)
    fingerprint: str = ""

    @property
    def identity(self) -> str:
        """What a dismissal is keyed by: the content fingerprint, or --
        for an app that has none -- the path (then a rename makes the
        group reappear, which is the safe direction)."""
        return self.fingerprint or str(self.path)


@dataclass
class DuplicateGroup:
    """Files that may be duplicates of each other. `key` is the app's own
    id for the group (shown nowhere; handy for the app), `reason` the
    sentence explaining the match."""
    key: str
    tier: str
    reason: str
    members: list[DuplicateMember] = field(default_factory=list)

    @property
    def identities(self) -> list[str]:
        return [m.identity for m in self.members]

    @property
    def dismissal_key(self) -> str:
        return dismissal_key(self.identities)


def sort_groups(groups: Iterable[DuplicateGroup]) -> list[DuplicateGroup]:
    """Strongest tier first, then the larger group, then the reason text
    (stable and predictable). Returns a new list."""
    return sorted(groups, key=lambda g: (tier_strength(g.tier), -len(g.members), g.reason.lower()))


# --- Dismissal ----------------------------------------------------------


def dismissal_key(fingerprints: Iterable[str]) -> str:
    """The stable key for "this exact set of members": a hash of the sorted
    fingerprints (order and path independent). Because it covers the
    whole set, a dismissed group REAPPEARS when a member is added (a
    third copy turns up) -- the user reviewed those two, not three. A
    group that merely loses a member is a different set too, and also
    reappears, which is the safe direction."""
    digest = hashlib.sha256()
    for fp in sorted(fingerprints):
        digest.update(fp.encode("utf-8", "surrogatepass"))
        digest.update(b"\n")
    return digest.hexdigest()


class DismissStore(ABC):
    """Remembers which groups the user marked "not duplicates". Every
    method takes the group's member FINGERPRINTS (`group.identities`).
    Apps may implement this on top of their own settings; only the four
    abstract methods are required (`undismiss` is optional -- without it
    the dialog can't offer to un-hide a single group, only
    `undismiss_all()` from the app)."""

    @abstractmethod
    def is_dismissed(self, group_fingerprints: Sequence[str]) -> bool: ...

    @abstractmethod
    def dismiss(self, group_fingerprints: Sequence[str]) -> None: ...

    @abstractmethod
    def undismiss_all(self) -> None: ...

    @abstractmethod
    def count(self) -> int: ...

    def undismiss(self, group_fingerprints: Sequence[str]) -> None:  # optional
        raise NotImplementedError


class InMemoryDismissStore(DismissStore):
    """Dismissals for this run only (also the test double)."""

    def __init__(self) -> None:
        self._keys: dict[str, None] = {}   # ordered set

    def is_dismissed(self, group_fingerprints: Sequence[str]) -> bool:
        return dismissal_key(group_fingerprints) in self._keys

    def dismiss(self, group_fingerprints: Sequence[str]) -> None:
        self._keys[dismissal_key(group_fingerprints)] = None

    def undismiss(self, group_fingerprints: Sequence[str]) -> None:
        self._keys.pop(dismissal_key(group_fingerprints), None)

    def undismiss_all(self) -> None:
        self._keys.clear()

    def count(self) -> int:
        return len(self._keys)


class JsonDismissStore(DismissStore):
    """Dismissals kept in a small JSON file (`{"version": 1, "dismissed":
    [key, ...]}`, oldest first). The file is read lazily and tolerantly
    (missing, unreadable or malformed -> empty, never an exception) and
    rewritten atomically (temp file in the same folder, then
    os.replace via replace_with_retry). At most `max_entries` keys are
    kept -- the oldest are dropped first -- so the file can't grow
    without bound. A failed write leaves the in-memory state intact
    (the dismissal still works this session) and is reported in
    `last_error`."""

    VERSION = 1

    def __init__(self, path: str, max_entries: int = 5000) -> None:
        self.path = str(path)
        self.max_entries = max(1, max_entries)
        self.last_error: Optional[str] = None
        self._keys: Optional[dict[str, None]] = None

    def _load(self) -> dict[str, None]:
        if self._keys is None:
            keys: dict[str, None] = {}
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                raw = data.get("dismissed", []) if isinstance(data, dict) else []
                if isinstance(raw, list):
                    for key in raw:
                        if isinstance(key, str) and key:
                            keys[key] = None
            except (OSError, ValueError):
                keys = {}
            self._keys = keys
            self._trim()
        return self._keys

    def _trim(self) -> None:
        keys = self._keys
        while keys is not None and len(keys) > self.max_entries:
            del keys[next(iter(keys))]

    def _save(self) -> None:
        keys = self._load()
        tmp_path = ""
        try:
            folder = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(folder, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix=".dismissed-", suffix=".tmp", dir=folder)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"version": self.VERSION, "dismissed": list(keys)}, fh)
            replace_with_retry(tmp_path, self.path)
            tmp_path = ""
            self.last_error = None
        except OSError as exc:
            self.last_error = str(exc)
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def is_dismissed(self, group_fingerprints: Sequence[str]) -> bool:
        return dismissal_key(group_fingerprints) in self._load()

    def dismiss(self, group_fingerprints: Sequence[str]) -> None:
        keys = self._load()
        key = dismissal_key(group_fingerprints)
        keys.pop(key, None)   # re-dismissing makes it the newest entry
        keys[key] = None
        self._trim()
        self._save()

    def undismiss(self, group_fingerprints: Sequence[str]) -> None:
        if self._load().pop(dismissal_key(group_fingerprints), "absent") != "absent":
            self._save()

    def undismiss_all(self) -> None:
        self._load().clear()
        self._save()

    def count(self) -> int:
        return len(self._load())

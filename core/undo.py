"""
redactor_common/core/undo.py

A small bounded undo stack for in-memory edits (bulk edits, single-
field edits, search & replace, case conversion, lookup-apply, ...).

Deliberately OUT of scope: physical file operations (Rename/Export,
Save, Delete). Those are already deliberate, explicitly-confirmed
actions with their own safety dialogs (or, for Delete, the Recycle
Bin's own undo), and reverting one would mean re-touching the
filesystem in ways that could surprise the user or collide with
changes made outside the app. Undo here only ever restores in-memory
state.

Generalized from epubredactor's original version (which snapshotted
EpubBook/EpubMetadata fields directly, so the whole module was
EpubBook-specific) to work on any item type via caller-supplied
snapshot/restore callables -- `push()` takes a `snapshot_fn(item) ->
opaque snapshot`, `undo()` takes the matching `restore_fn(item,
snapshot) -> None`. Neither UndoManager nor UndoEntry knows or cares
what's actually inside a snapshot.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Callable, Generic, Optional, TypeVar

T = TypeVar("T")


@dataclass
class UndoEntry(Generic[T]):
    label: str
    snapshots: list[tuple[T, object]]


class UndoManager(Generic[T]):
    def __init__(self, max_entries: int = 5):
        self._stack: deque[UndoEntry[T]] = deque(maxlen=max_entries)

    def push(self, label: str, items: list[T], snapshot_fn: Callable[[T], object]) -> None:
        """Call BEFORE mutating `items`, to capture their pre-change
        state via snapshot_fn(item). Pushing a 6th entry (beyond
        max_entries) silently drops the oldest one -- that's the
        "last N changes" behavior."""
        snapshots = [(item, snapshot_fn(item)) for item in items]
        self._stack.append(UndoEntry(label=label, snapshots=snapshots))

    def can_undo(self) -> bool:
        return bool(self._stack)

    def peek_label(self) -> Optional[str]:
        return self._stack[-1].label if self._stack else None

    def undo(self, restore_fn: Callable[[T, object], None]) -> list[T]:
        """Restore the most recently pushed entry via
        restore_fn(item, snapshot) -- mutates each item in place.
        Returns the list of items that were restored (empty if there
        was nothing to undo)."""
        if not self._stack:
            return []
        entry = self._stack.pop()
        affected: list[T] = []
        for item, snapshot in entry.snapshots:
            restore_fn(item, snapshot)
            affected.append(item)
        return affected

    def clear(self) -> None:
        """Drops every entry -- call when the items an existing entry
        would reference are about to become stale (e.g. the whole list
        is being reloaded from disk), since restoring into a
        since-replaced object wouldn't reach anything still on screen."""
        self._stack.clear()

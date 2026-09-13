"""
redactor_common/core/undo.py

A small bounded undo/redo stack for in-memory edits (bulk edits,
single-field edits, search & replace, case conversion, lookup-apply,
...).

Deliberately OUT of scope: physical file operations (Rename/Export,
Save, Delete). Those are already deliberate, explicitly-confirmed
actions with their own safety dialogs (or, for Delete, the Recycle
Bin's own undo), and reverting one would mean re-touching the
filesystem in ways that could surprise the user or collide with
changes made outside the app. Undo/redo here only ever restores
in-memory state.

Generalized from epubredactor's original version (which snapshotted
EpubBook/EpubMetadata fields directly, so the whole module was
EpubBook-specific) to work on any item type via caller-supplied
snapshot/restore callables -- `push()` takes a `snapshot_fn(item) ->
opaque snapshot`, `undo()`/`redo()` take the matching `restore_fn(item,
snapshot) -> None`. Neither UndoManager nor UndoEntry knows or cares
what's actually inside a snapshot.

Redo works by having undo() (and redo()) snapshot the item's CURRENT
state -- via the same snapshot_fn the original push() used -- right
before overwriting it, and stashing that onto the opposite stack. So
undo() moves an entry from the undo stack to the redo stack (after
using it to restore the OLDER state), and redo() moves it back (after
using it to restore the NEWER state). A fresh push() (i.e. a genuinely
new edit, not an undo/redo) clears the redo stack -- once you've done
something new, "redo" no longer has a coherent future to jump to,
same as every other app's undo/redo.

snapshot_fn is optional on undo()/redo() for backward compatibility
with a caller that doesn't want redo support at all -- omit it and
that call just won't populate the opposite stack, same as before this
module supported redo.
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
        self._undo_stack: deque[UndoEntry[T]] = deque(maxlen=max_entries)
        self._redo_stack: deque[UndoEntry[T]] = deque(maxlen=max_entries)

    def push(self, label: str, items: list[T], snapshot_fn: Callable[[T], object]) -> None:
        """Call BEFORE mutating `items`, to capture their pre-change
        state via snapshot_fn(item). Pushing a 6th entry (beyond
        max_entries) silently drops the oldest one -- that's the
        "last N changes" behavior. Clears any pending redo history:
        a genuinely new edit invalidates whatever "future" redo would
        have jumped back to."""
        snapshots = [(item, snapshot_fn(item)) for item in items]
        self._undo_stack.append(UndoEntry(label=label, snapshots=snapshots))
        self._redo_stack.clear()

    def can_undo(self) -> bool:
        return bool(self._undo_stack)

    def can_redo(self) -> bool:
        return bool(self._redo_stack)

    def peek_label(self) -> Optional[str]:
        return self._undo_stack[-1].label if self._undo_stack else None

    def peek_redo_label(self) -> Optional[str]:
        return self._redo_stack[-1].label if self._redo_stack else None

    def undo(
        self,
        restore_fn: Callable[[T, object], None],
        snapshot_fn: Callable[[T], object] | None = None,
    ) -> list[T]:
        """Restore the most recently pushed entry via
        restore_fn(item, snapshot) -- mutates each item in place.
        Returns the list of items that were restored (empty if there
        was nothing to undo).

        Pass `snapshot_fn` (the same one used with push()) to enable
        redo: the item's current state is captured before it's
        overwritten, and stashed so a later redo() can bring it back.
        """
        if not self._undo_stack:
            return []
        entry = self._undo_stack.pop()
        if snapshot_fn is not None:
            redo_snapshots = [(item, snapshot_fn(item)) for item, _snap in entry.snapshots]
            self._redo_stack.append(UndoEntry(label=entry.label, snapshots=redo_snapshots))
        affected: list[T] = []
        for item, snapshot in entry.snapshots:
            restore_fn(item, snapshot)
            affected.append(item)
        return affected

    def redo(
        self,
        restore_fn: Callable[[T, object], None],
        snapshot_fn: Callable[[T], object] | None = None,
    ) -> list[T]:
        """The mirror of undo(): re-applies the most recently undone
        entry via restore_fn(item, snapshot). Returns the list of
        items that were restored (empty if there was nothing to redo).

        Pass `snapshot_fn` to keep undo() available afterward -- same
        "capture current state before overwriting it" trade as undo()
        makes for redo.
        """
        if not self._redo_stack:
            return []
        entry = self._redo_stack.pop()
        if snapshot_fn is not None:
            undo_snapshots = [(item, snapshot_fn(item)) for item, _snap in entry.snapshots]
            self._undo_stack.append(UndoEntry(label=entry.label, snapshots=undo_snapshots))
        affected: list[T] = []
        for item, snapshot in entry.snapshots:
            restore_fn(item, snapshot)
            affected.append(item)
        return affected

    def clear(self) -> None:
        """Drops every entry, undo AND redo -- call when the items an
        existing entry would reference are about to become stale (e.g.
        the whole list is being reloaded from disk), since restoring
        into a since-replaced object wouldn't reach anything still on
        screen."""
        self._undo_stack.clear()
        self._redo_stack.clear()

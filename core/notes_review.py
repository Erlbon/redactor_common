"""
redactor_common/core/notes_review.py

The rules behind the Create Notes review page of every app that writes notes, Qt-free so they can be tested without a
window: which view the page opens in, what may be ticked, and what Skip / Write do to a proposal.

  choose_view(targets)      'one' (one owner folder with its children, or one single folder: walk through the notes one
                            at a time) or 'list' (several owner folders: a ticking list)
  ReviewSession             the proposals with their state (pending / written / skipped / exists / ...), the
                            ticks, and the writes. `write_fn` is injected (the app's write_proposal), so a test can use a fake one and nothing is ever written by accident.

Nothing is written unless a method of the session is called for a proposal a person has chosen to write: a
confident one (the only kind with a draft) that is ticked / being looked at. An uncertain or nothing-found
proposal has no draft: it cannot be ticked or written until the person has resolved it (picked a candidate or a
level, which rebuilds it through the engine: NotesEngine.rebuild) and the rebuilt proposal is confident.
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from redactor_common.core.notes_model import STATUS_SKIPPED, Confidence, Proposal, summarize
from redactor_common.core.notes import STATUS_CHANGED, STATUS_ERROR, STATUS_EXISTS, STATUS_TOO_LARGE, STATUS_WRITTEN, NoteWriteResult

VIEW_ONE = "one"
VIEW_LIST = "list"

PENDING = "pending"
WRITTEN = "written"
SKIPPED = "skipped"
EXISTS = "exists"  # an existing note blocked the write
TOO_LARGE = "too_large"
ERROR = "error"

CONFIDENCE_WORDS = {Confidence.CONFIDENT: "Confident", Confidence.UNCERTAIN: "Uncertain", Confidence.NONE: "Nothing found"}
STATE_WORDS = {PENDING: "", WRITTEN: "Written", SKIPPED: "Skipped", EXISTS: "Note exists", TOO_LARGE: "Too large", ERROR: "Error"}


def note_diff(old_text: str, new_text: str, context: int = 3) -> str:
    """A unified diff of an older note and the new draft ('' when they are the same): the before / after of an update."""
    lines = difflib.unified_diff(
        old_text.splitlines(), new_text.splitlines(), fromfile="the note now", tofile="the new note", lineterm="", n=context)
    return "\n".join(lines)


def _key(folder) -> str:
    return os.path.normcase(os.path.abspath(str(folder)))


def choose_view(targets: Iterable, owner_kinds: Iterable[str] = ("artist", "genre"), child_kind: str = "album") -> str:
    """'one' when the scan found a single owner folder (an artist with its album notes, a series with its season
    notes) or a single child, 'list' otherwise. A child belongs to the owner folder right above it when that folder
    is also a target; every other target is a folder of its own."""
    targets = list(targets)
    owner_kinds = tuple(owner_kinds)
    folders = {_key(t.folder) for t in targets if t.kind in owner_kinds}
    owners = set()
    for target in targets:
        if target.kind == child_kind and _key(Path(target.folder).parent) in folders:
            owners.add(_key(Path(target.folder).parent))
        else:
            owners.add(_key(target.folder))
    return VIEW_ONE if len(owners) <= 1 else VIEW_LIST


@dataclass
class ReviewItem:
    proposal: Proposal
    state: str = PENDING
    ticked: bool = False
    message: str = ""  # what the write said (path, or why it did not happen)
    written_path: str = ""
    user_resolved: bool = False  # rebuilt after a person picked a candidate / a level

    @property
    def confidence_word(self) -> str:
        return CONFIDENCE_WORDS[self.proposal.confidence]

    @property
    def state_word(self) -> str:
        return STATE_WORDS[self.state]

    @property
    def tickable(self) -> bool:
        return self.state == PENDING and self.proposal.writable

    @property
    def finished(self) -> bool:
        return self.state in (WRITTEN, SKIPPED)


@dataclass
class ReviewTotals:
    written: int = 0
    skipped: int = 0
    uncertain_left: int = 0  # not confident and not skipped
    blocked: int = 0  # an existing note was in the way
    errors: int = 0  # too large / write errors
    pending_confident: int = 0
    messages: list[str] = field(default_factory=list)

    def text(self) -> str:
        parts = [f"Written: {self.written}", f"Skipped: {self.skipped}", f"Uncertain left: {self.uncertain_left}"]
        if self.blocked:
            parts.append(f"Blocked by an existing note: {self.blocked}")
        if self.errors:
            parts.append(f"Errors: {self.errors}")
        return "\n".join(parts + ([""] + self.messages if self.messages else []))


class ReviewSession:
    def __init__(self, proposals: Iterable[Proposal], write_fn: Callable[..., NoteWriteResult]):
        self.items = [ReviewItem(p) for p in sorted(proposals, key=lambda p: str(p.folder).casefold())]
        self.write_fn = write_fn
        self.tick_confident()

    # -- ticks -----------------------------------------------------------------------------------------------

    def tick_confident(self) -> None:
        """Confident proposals are pre-ticked; uncertain and nothing-found ones never are, and neither is an UPDATE
        of an older note (it replaces something that exists: only an explicit tick or Write note does that)."""
        for item in self.items:
            item.ticked = item.tickable and not item.proposal.is_update

    def set_ticked(self, index: int, ticked: bool) -> bool:
        """Ticks / unticks one item. Returns False (and changes nothing) when it may not be ticked: an uncertain
        or nothing-found proposal, or one that is already written / skipped."""
        item = self.items[index]
        if ticked and not item.tickable:
            return False
        item.ticked = ticked
        return True

    def ticked_pending(self) -> list[int]:
        return [i for i, item in enumerate(self.items) if item.ticked and item.tickable]

    # -- decisions -------------------------------------------------------------------------------------------

    def skip(self, index: int) -> None:
        item = self.items[index]
        if item.state == PENDING:
            item.state, item.ticked = SKIPPED, False

    def write(self, index: int) -> NoteWriteResult:
        """Writes one proposal (never overwrites: write_proposal's own rule). Only a confident proposal with a
        draft is written; anything else comes back with status 'skipped' and nothing is touched."""
        item = self.items[index]
        if not item.tickable:
            return NoteWriteResult(STATUS_SKIPPED, item.proposal.path, "not written: it is not a confident match")
        result = self.write_fn(item.proposal)
        item.ticked = False
        item.message = result.message or result.path
        if result.status == STATUS_WRITTEN:
            item.state, item.written_path = WRITTEN, result.path or item.proposal.path
        elif result.status in (STATUS_EXISTS, STATUS_CHANGED):  # a note in the way, or one edited since it was looked at
            item.state = EXISTS
            item.message = result.message or f"{result.path} already exists"
        elif result.status == STATUS_TOO_LARGE:
            item.state = TOO_LARGE
        elif result.status == STATUS_ERROR:
            item.state = ERROR
        # 'skipped' (the engine refused): the item stays pending
        return result

    def write_ticked(self) -> list[NoteWriteResult]:
        return [self.write(i) for i in self.ticked_pending()]

    def write_remaining_confident(self) -> list[NoteWriteResult]:
        """Every confident NEW note not yet written. An update of an older note is only written when it was ticked."""
        return [self.write(i) for i, item in enumerate(self.items)
                if item.tickable and (not item.proposal.is_update or item.ticked)]

    def replace(self, index: int, proposal: Proposal, user_resolved: bool = True) -> ReviewItem:
        """The proposal rebuilt after a person's choice. A confident result is ticked (the person just asked
        for it); anything else stays unticked and untickable."""
        item = self.items[index]
        item.proposal, item.user_resolved = proposal, user_resolved
        item.state, item.message = PENDING, ""
        item.ticked = item.tickable and not proposal.is_update
        return item

    # -- reading ---------------------------------------------------------------------------------------------

    def next_pending(self, after: int = -1) -> Optional[int]:
        """The next item not yet written or skipped (and not blocked), after `after`, wrapping round."""
        n = len(self.items)
        for step in range(1, n + 1):
            i = (after + step) % n
            if self.items[i].state == PENDING:
                return i
        return None

    def totals(self) -> ReviewTotals:
        totals = ReviewTotals()
        for item in self.items:
            if item.state == WRITTEN:
                totals.written += 1
            elif item.state == SKIPPED:
                totals.skipped += 1
            elif item.state == EXISTS:
                totals.blocked += 1
                totals.messages.append(f"{item.proposal.folder.name}: {item.message}")
            elif item.state in (TOO_LARGE, ERROR):
                totals.errors += 1
                totals.messages.append(f"{item.proposal.folder.name}: {item.message}")
            elif not item.proposal.writable:
                totals.uncertain_left += 1
            else:
                totals.pending_confident += 1
        return totals

    def problems(self) -> list[str]:
        """The service problems of the run, each once, in plain words."""
        return summarize([item.proposal for item in self.items]).problems

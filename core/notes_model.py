"""
redactor_common/core/notes_model.py

What every app that writes folder notes (mp3redactor, videoredactor, later epub/cbz) shares about a *proposal*: the
draft of one note, how sure the engine is of it, and the rules for writing it. Qt-free. The engines (what to look up
and how to judge a match) stay in the apps; the review page, the summary, the write rules and the undo are the same.

  Confidence                   confident (verified: the only kind a batch run writes) / uncertain / none
  Candidate                    one artist / film / series the engine considered, kept for a person to pick
  Proposal                     the draft of one note (subclass it for app-specific fields)
  summarize(proposals, ...)    counts for a report
  write_proposal(proposal)     redactor_common's write_note / replace_note: never overwrites, status: draft
  undo_note_entry(entry, ...)  the opt-in journal's undo: Recycle Bin, only while the note is still as written

A proposal's `target` is any object with `.folder` (Path), `.kind` (the app's own word: 'artist', 'series', ...) and
`.note_kind` (the file's kind for redactor_common.core.notes: artist / author / series / album / about).
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from redactor_common.core.notes import (
    STATUS_WRITTEN, NoteState, NoteWriteResult, normalize_note_text, note_path, replace_note, write_note,
)

STATUS_SKIPPED = "skipped"  # write_proposal's own status: an uncertain / none proposal is never written
KIND_NOTE = "note"  # the journal's kind for a created note (not a tag change: never "restorable" as one)


class Confidence(str, Enum):
    CONFIDENT = "confident"
    UNCERTAIN = "uncertain"
    NONE = "none"


@dataclass
class Candidate:
    """One artist / release / title the engine considered, kept for the review page."""

    source: str  # "musicbrainz" / "discogs" / "tmdb"
    id: str
    name: str
    detail: str = ""
    hits: list[str] = field(default_factory=list)  # local items found in its own list (albums, episodes)
    score: float = 0.0
    note: str = ""
    also: str = ""  # a second source that names the SAME thing: one merged candidate, not two lines
    also_id: str = ""

    def to_dict(self) -> dict:
        out = {"source": self.source, "id": self.id, "name": self.name, "detail": self.detail,
               "hits": list(self.hits), "score": round(self.score, 3), "note": self.note}
        if self.also:
            out["also"], out["also_id"] = self.also, self.also_id
        return out


@dataclass
class Proposal:
    target: object
    confidence: Confidence = Confidence.NONE
    note_text: str = ""  # the draft; only built for a confident proposal
    title: str = ""
    reasons: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # network errors and the like, in plain words
    kind_override: str = ""  # the note kind when the app decided differently from the scan's guess (collection -> about)
    # An UPDATE proposal: the folder already has an older note of ours (state outdated-untouched or legacy); `note_text`
    # is the new draft, this is what the old note was when it was looked at.
    old_state: Optional[NoteState] = None

    @property
    def is_update(self) -> bool:
        return self.old_state is not None

    @property
    def old_text(self) -> str:
        return self.old_state.text if self.old_state else ""

    @property
    def folder(self) -> Path:
        return self.target.folder  # type: ignore[attr-defined]

    @property
    def kind(self) -> str:
        return self.target.kind  # type: ignore[attr-defined]

    @property
    def note_kind(self) -> str:
        """The note file's kind: artist / author / series / album / about."""
        return self.kind_override or self.target.note_kind  # type: ignore[attr-defined]

    @property
    def path(self) -> str:
        return note_path(self.folder, self.note_kind)

    @property
    def writable(self) -> bool:
        return self.confidence == Confidence.CONFIDENT and bool(self.note_text)

    def to_dict(self, with_text: bool = True) -> dict:
        out = {
            "folder": str(self.folder), "kind": self.kind, "note": self.path, "confidence": self.confidence.value,
            "title": self.title, "reasons": list(self.reasons), "sources": list(self.sources),
            "note_kind": self.note_kind, "candidates": [c.to_dict() for c in self.candidates],
            "problems": list(self.problems), "action": "update" if self.is_update else "create",
        }
        if self.old_state is not None:
            out["old_state"] = self.old_state.kind
            out["old_generator"] = self.old_state.generator
        if with_text:
            out["draft"] = self.note_text
        return out


@dataclass
class NotesSummary:
    total: int = 0
    confident: int = 0
    uncertain: int = 0
    none: int = 0
    written: int = 0
    would_write: int = 0
    exists: int = 0
    failed: int = 0
    not_written: int = 0  # uncertain / none: left for review
    updates: int = 0  # proposals that would refresh an older note of ours
    cancelled: bool = False
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def summarize(
    proposals: list[Proposal], results: Optional[dict[str, NoteWriteResult]] = None, cancelled: bool = False,
) -> NotesSummary:
    """Counts for a report. `results` maps a note path to its write_proposal() result (dry-run results count as
    'would_write')."""
    summary = NotesSummary(total=len(proposals), cancelled=cancelled)
    for proposal in proposals:
        summary.updates += 1 if proposal.is_update else 0
        setattr(summary, proposal.confidence.value, getattr(summary, proposal.confidence.value) + 1)
        for problem in proposal.problems:
            if problem not in summary.problems:
                summary.problems.append(problem)
        result = (results or {}).get(proposal.path)
        if result is None:
            continue
        if result.status == STATUS_WRITTEN:
            if result.dry_run:
                summary.would_write += 1
            else:
                summary.written += 1
        elif result.status == "exists":
            summary.exists += 1
        elif result.status == STATUS_SKIPPED:
            summary.not_written += 1
        else:
            summary.failed += 1
    return summary


# ---- building ----------------------------------------------------------------------------------------------------

Progress = Callable[[int, int, str], None]


def build_each(
    targets: list, build: Callable[[object], Proposal], progress: Optional[Progress] = None,
    cancel: Optional[Callable[[], bool]] = None, on_proposal: Optional[Callable[[Proposal], None]] = None,
) -> list[Proposal]:
    """One Proposal per target, in order, from the app engine's `build(target)`. `progress(done, total, label)` is called
    before each target; `cancel()` returning True ends the run early (the proposals built so far are returned);
    `on_proposal(p)` is called as each one is ready (the command line writes it there, so an interrupted run keeps what it
    did). Network only; this function writes no file."""
    proposals: list[Proposal] = []
    for index, target in enumerate(targets):
        if cancel is not None and cancel():
            break
        if progress is not None:
            progress(index, len(targets), f"{target.kind}: {target.folder.name}")  # type: ignore[attr-defined]
        proposal = build(target)
        proposals.append(proposal)
        if on_proposal is not None:
            on_proposal(proposal)
    if progress is not None and len(proposals) == len(targets):
        progress(len(targets), len(targets), "")
    return proposals


# ---- writing, the journal and its undo ---------------------------------------------------------------------------

def text_sha(text: str) -> str:
    """The SHA-256 of a note as stored: what a journal entry keeps so an undo can tell whether it was edited."""
    return hashlib.sha256(normalize_note_text(text).encode("utf-8")).hexdigest()


def write_proposal(
    proposal: Proposal, dry_run: bool = False, allow_uncertain: bool = False,
    record: Optional[Callable[[str, str, bool], None]] = None,
) -> NoteWriteResult:
    """Writes the draft of a CONFIDENT proposal (an uncertain / none one is refused with status 'skipped' unless
    a person chose it: `allow_uncertain`). Never overwrites (write_note: any existing .md note of any of the five
    names, any letter case, makes the result 'exists'). `record(path, text, replaced)` is the app's journal hook, called
    after a real write (the opt-in history; the app decides whether it is on)."""
    if proposal.confidence != Confidence.CONFIDENT and not allow_uncertain:
        return NoteWriteResult(
            STATUS_SKIPPED, proposal.path, f"not written: the match is {proposal.confidence.value}", dry_run)
    if not proposal.note_text:
        return NoteWriteResult(STATUS_SKIPPED, proposal.path, "there is no draft to write", dry_run)
    if proposal.is_update:  # refresh an older note of ours: only while it is still what was looked at, old one to the Recycle Bin
        result = replace_note(proposal.folder, proposal.note_kind, proposal.note_text, expect=proposal.old_state,
                              dry_run=dry_run)
    else:
        result = write_note(proposal.folder, proposal.note_kind, proposal.note_text, dry_run=dry_run)
    if result.ok and not dry_run and record is not None:
        record(result.path, proposal.note_text, bool(result.replaced))
    return result


def undo_note_entry(entry, journal, trash: Optional[Callable[[str], None]] = None) -> str:
    """Undoes a journal entry of a created note: the file goes to the Recycle Bin, but ONLY if it still holds
    exactly what was written (an edited note is the person's now). `journal.mark_restored(entry.id, None)` is called
    afterwards. Returns '' or a plain-language problem."""
    if getattr(entry, "kind", "") != KIND_NOTE:
        return "this entry is not a created note"
    match = re.match(r"sha256:([0-9a-f]{64})$", getattr(entry, "note", "") or "")
    if not match:
        return "the entry has no checksum, so the file cannot be checked"
    path = entry.path
    if not os.path.isfile(path):
        return "the note is no longer there"
    try:
        with open(path, "rb") as handle:
            current = hashlib.sha256(handle.read()).hexdigest()
    except OSError as exc:
        return f"the note could not be read ({exc})"
    if current != match.group(1):
        return "the note was changed after it was created; it is left alone"
    try:
        if trash is None:
            from redactor_common.core.trash import move_to_trash as trash
        trash(path)
    except Exception as exc:  # noqa: BLE001 -- TrashError, OSError
        return f"the note could not be moved to the Recycle Bin ({exc})"
    try:
        journal.mark_restored(entry.id, None)
    except Exception:  # noqa: BLE001 -- the undo itself happened
        pass
    return ""

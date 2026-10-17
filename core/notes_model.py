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
import inspect
import time

from redactor_common.core.notes import (
    STATUS_WRITTEN, NoteState, NoteWriteResult, _link_or_reserve, _temp_path, find_note, normalize_note_text, note_path, replace_note, write_note,
)

STATUS_SKIPPED = "skipped"  # write_proposal's own status: an uncertain / none proposal is never written
KIND_NOTE = "note"  # the journal's kind for a created note (Entry.restorable stays False: the History dialog undoes it through an app-registered handler)
KIND_NOTE_UPDATE = "note-update"  # the journal's kind for a refreshed note (an older note of ours was replaced)
NOTES_BACKUP_DIR = "notes_backup"  # next to the app's settings: the text a refresh replaced, <entry id>.md (History on only)


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


def _accepts_old_bytes(record: Callable) -> bool:
    try:
        parameters = inspect.signature(record).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == "old_bytes" or p.kind is p.VAR_KEYWORD for p in parameters)


def write_proposal(
    proposal: Proposal, dry_run: bool = False, allow_uncertain: bool = False,
    record: Optional[Callable[..., None]] = None, trash: Optional[Callable[[str], None]] = None,
) -> NoteWriteResult:
    """Writes the draft of a CONFIDENT proposal (an uncertain / none one is refused with status 'skipped' unless
    a person chose it: `allow_uncertain`). Never overwrites (write_note: any existing .md note of any of the five
    names, any letter case, makes the result 'exists'). `record(path, text, replaced[, old_bytes=])` is the app's
    journal hook, called after a real write (the opt-in history; the app decides whether it is on); a hook that
    accepts `old_bytes` also gets the bytes of the note a refresh replaced (for record_note_entry's backup).
    `trash` is the mover for the replaced note (default: the Recycle Bin; an app passes one with its backup
    folder so a refresh also works on a network drive)."""
    if proposal.confidence != Confidence.CONFIDENT and not allow_uncertain:
        return NoteWriteResult(
            STATUS_SKIPPED, proposal.path, f"not written: the match is {proposal.confidence.value}", dry_run)
    if not proposal.note_text:
        return NoteWriteResult(STATUS_SKIPPED, proposal.path, "there is no draft to write", dry_run)
    old_bytes: Optional[bytes] = None
    if proposal.is_update:  # refresh an older note of ours: only while it is still what was looked at, old one to the Recycle Bin
        if record is not None and not dry_run:
            old_path = find_note(proposal.folder, proposal.note_kind)
            if old_path:
                try:
                    with open(old_path, "rb") as handle:
                        data = handle.read(MAX_BACKUP_BYTES + 1)
                    old_bytes = data if len(data) <= MAX_BACKUP_BYTES else None
                except OSError:
                    old_bytes = None
        result = replace_note(proposal.folder, proposal.note_kind, proposal.note_text, expect=proposal.old_state,
                              dry_run=dry_run, trash=trash)
    else:
        result = write_note(proposal.folder, proposal.note_kind, proposal.note_text, dry_run=dry_run)
    if result.ok and not dry_run and record is not None:
        if _accepts_old_bytes(record):
            record(result.path, proposal.note_text, bool(result.replaced), old_bytes=old_bytes)
        else:
            record(result.path, proposal.note_text, bool(result.replaced))
    return result


# ---- the opt-in history of written notes, and its undo -----------------------------------------------------------

MAX_BACKUP_BYTES = 64 * 1024
_ENTRY_ID = re.compile(r"[0-9a-f]{1,32}$")
_SHA_NOTE = re.compile(r"sha256:([0-9a-f]{64})$")
NETWORK_HINT = " On a network drive there is no Recycle Bin: set a backup folder in Preferences to undo there."


def backup_folder(base_dir: str | os.PathLike) -> str:
    return os.path.join(os.fspath(base_dir), NOTES_BACKUP_DIR)


def store_note_backup(base_dir: str | os.PathLike, entry_id: str, data: bytes, max_days: float = 90,
                      clock: Callable[[], float] = time.time) -> str:
    """Keeps `data` (the text a refresh replaced) as <base_dir>/notes_backup/<entry id>.md and prunes backups older
    than `max_days` (the journal's retention). Returns the path. OSError when it cannot be written."""
    if not _ENTRY_ID.match(entry_id or ""):
        raise ValueError("bad entry id")
    folder = backup_folder(base_dir)
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, f"{entry_id}.md")
    temp = target + ".part"
    with open(temp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, target)
    prune_note_backups(base_dir, max_days, clock)
    return target


def load_note_backup(base_dir: str | os.PathLike | None, entry_id: str) -> Optional[bytes]:
    """The stored text of the note entry `entry_id` replaced, or None."""
    if not base_dir or not _ENTRY_ID.match(entry_id or ""):
        return None
    try:
        with open(os.path.join(backup_folder(base_dir), f"{entry_id}.md"), "rb") as handle:
            return handle.read(MAX_BACKUP_BYTES + 1)[:MAX_BACKUP_BYTES]
    except OSError:
        return None


def prune_note_backups(base_dir: str | os.PathLike, max_days: float = 90, clock: Callable[[], float] = time.time) -> int:
    """Deletes the backups older than `max_days` (same retention as the journal). Returns how many went."""
    folder = backup_folder(base_dir)
    cutoff = clock() - max_days * 86_400
    gone = 0
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    for name in names:
        full = os.path.join(folder, name)
        try:
            if os.path.isfile(full) and os.path.getmtime(full) < cutoff:
                os.unlink(full)
                gone += 1
        except OSError:
            continue
    return gone


def record_note_entry(journal, base_dir: str | os.PathLike | None, path: str, text: str, replaced: bool,
                      old_bytes: Optional[bytes] = None) -> Optional[str]:
    """The journal entry of a written note (call it only when the app's History is ON: opt-in logging). A created
    note is kind 'note'; a refresh is 'note-update' and, when `old_bytes` and `base_dir` are given, the replaced
    text is kept in <base_dir>/notes_backup/<entry id>.md so the undo can put it back. Both entries keep the
    SHA-256 of what was written, so an undo can tell whether the person edited the note since. Returns the id."""
    kind = KIND_NOTE_UPDATE if replaced else KIND_NOTE
    before = "an older note, now in the Recycle Bin" if replaced else ""
    entry_id = journal.record(path, kind, {"note": (before, Path(path).name)}, note=f"sha256:{text_sha(text)}")
    if replaced and entry_id and old_bytes is not None and base_dir:
        try:
            store_note_backup(base_dir, entry_id, old_bytes, getattr(journal, "max_days", 90))
        except (OSError, ValueError):
            pass  # the refresh stands; its undo will say the older text was not kept
    return entry_id


def undo_note_entry(entry, journal, trash: Optional[Callable[[str], None]] = None,
                    backup_base: str | os.PathLike | None = None) -> str:
    """Undoes a journal entry of a written note, ONLY if the note still holds exactly what was written (an edited
    note is the person's now). The note goes to the Recycle Bin (`trash`; never a permanent delete).
      - 'note' (created): that is all; the folder has no note again.
      - 'note-update' (refreshed): the older text kept in <backup_base>/notes_backup is written back in its place
        (the older note itself stays in the Recycle Bin too). Without that backup the undo is refused, so the
        folder is never left without a note.
    `journal.mark_restored(entry.id, None)` is called afterwards. Returns '' or a plain-language problem."""
    kind = getattr(entry, "kind", "")
    if kind not in (KIND_NOTE, KIND_NOTE_UPDATE):
        return "this entry is not a created note"
    if getattr(entry, "status", "") == "restored":
        return "this entry was already undone"
    match = _SHA_NOTE.match(getattr(entry, "note", "") or "")
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
        return "the note was changed after it was written; it is left alone"
    older: Optional[bytes] = None
    if kind == KIND_NOTE_UPDATE:
        older = load_note_backup(backup_base, entry.id)
        if older is None:
            return ("the older text of this note was not kept, so the refresh cannot be undone here (the older note "
                    "is in the Recycle Bin)")
    if trash is None:
        from redactor_common.core.trash import trash_with_retries

        trash = trash_with_retries()
    temp = ""
    try:
        if older is not None:  # stage the older text first: a failure here leaves the refreshed note alone
            temp = _temp_path(os.path.dirname(path), os.path.basename(path))
            try:
                fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o666)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(older)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                return f"the older text could not be prepared ({exc})"
        try:
            trash(path)
        except Exception as exc:  # noqa: BLE001 -- TrashError, OSError
            from redactor_common.core.trash import TrashUnavailable

            return (f"the note could not be moved to the Recycle Bin ({exc})."
                    + (NETWORK_HINT if isinstance(exc, TrashUnavailable) else ""))
        if older is not None:
            try:
                _link_or_reserve(temp, path)
            except OSError as exc:
                return (f"the refreshed note is in the Recycle Bin but the older text could not be put back ({exc}); "
                        f"it is kept in {backup_folder(backup_base)}")
    finally:
        if temp:
            try:
                os.unlink(temp)
            except OSError:
                pass
    try:
        journal.mark_restored(entry.id, None)
    except Exception:  # noqa: BLE001 -- the undo itself happened
        pass
    if older is not None:
        try:
            os.unlink(os.path.join(backup_folder(backup_base), f"{entry.id}.md"))
        except OSError:
            pass
    return ""


def make_note_undo_handler(journal, backup_base: str | os.PathLike | None,
                           fallback_folder: Callable[[], str] | str | None = None,
                           trash: Optional[Callable[[str], None]] = None) -> Callable:
    """The handler an app registers with HistoryDialog(undo_handlers={'note': h, 'note-update': h}):
    handler(entry) -> (ok, message). `fallback_folder` (text or a callable giving it, the app's backup folder
    setting) is where the note goes on a drive without a Recycle Bin; unset, an undo there is refused."""

    def handler(entry) -> tuple[bool, str]:
        mover = trash
        if mover is None:
            from redactor_common.core.trash import trash_with_fallback

            mover = trash_with_fallback(fallback_folder)
        problem = undo_note_entry(entry, journal, trash=mover, backup_base=backup_base)
        if problem:
            return False, problem
        if getattr(entry, "kind", "") == KIND_NOTE_UPDATE:
            return True, "Undone: the refreshed note went to the Recycle Bin and the older text is back."
        return True, "Undone: the note went to the Recycle Bin."

    return handler

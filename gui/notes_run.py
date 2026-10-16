"""
redactor_common/gui/notes_run.py

Tools > Create Notes, the part after the app has asked which folders: scan them under a progress dialog, look every folder
up under a cancellable counted progress dialog (with elapsed and remaining time), open the review page. The same for every
app that writes folder notes; what differs (the scan, the engine, how a folder is named in the progress label) is passed in.

  run_create_notes(parent, ...)    returns the (closed) NotesReviewDialog, or None when there was nothing to review
  parse_size("1100x700")           the review page's remembered size, or None

Nothing here touches a widget off the GUI thread: the lookups run in run_work_with_progress(), which marshals the label
and count to the GUI thread, and `engine` / `build` only talk to the network.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from PyQt6.QtWidgets import QMessageBox, QWidget

from redactor_common.core.notes_model import Candidate, Proposal, write_proposal as shared_write_proposal
from redactor_common.core.notes_review import ReviewSession, choose_view
from redactor_common.core.notes_timing import lookup_sentence, progress_suffix
from redactor_common.gui.notes_review_dialog import NotesReviewDialog, open_in_system
from redactor_common.gui.progress import run_busy
from redactor_common.gui.work_progress import run_work_with_progress

log = logging.getLogger("redactor_common.notes")


def parse_size(text: str) -> Optional[tuple[int, int]]:
    try:
        w, h = (int(part) for part in text.lower().split("x"))
    except (ValueError, AttributeError):
        return None
    return (w, h) if 500 <= w <= 5000 and 350 <= h <= 4000 else None


def run_create_notes(
    parent: QWidget, *, title: str, scan: Callable[[], object], engine, build: Callable[..., list],
    write_fn: Callable[..., object] = shared_write_proposal, label_for: Callable[[object], str] = lambda t: t.folder.name,
    what: str = "folders", nothing_found: str = "No folders without a note were found.",
    intro_notes: Optional[list[str]] = None, owner_kinds: tuple[str, ...] = ("series",), child_kind: str = "season",
    size_text: str = "", on_size: Optional[Callable[[str], None]] = None, open_path: Callable[[str], None] = open_in_system,
    include_outdated: bool = False, left_alone: Callable[[object], str] = lambda found: "",
    candidate_columns: tuple[str, ...] = ("Name", "Details", "Score", "Source", "Matches"),
    history_note: str = "",
) -> Optional[NotesReviewDialog]:
    """Scan, look up, review.

    `scan()` returns an object with `.targets`, `.skipped` and `.missing` (the app's ScanResult); `engine` has `.phases`,
    `.stats`, `.rebuild(proposal, candidate)` and `.can_choose(proposal, candidate)`; `build(targets, progress=, cancel=,
    on_proposal=, engine=)` returns the proposals (the app's build_proposals). `write_fn` is what the review page writes
    with. `label_for(target)` names a folder in the progress label."""
    scan_started = time.perf_counter()
    try:
        found = run_busy(parent, "Looking at the folders…", scan, title=title)
    except Exception as exc:  # noqa: BLE001
        QMessageBox.warning(parent, title, f"The folders could not be read: {exc}")
        return None
    if not found.targets:
        why = []
        if found.skipped:
            why.append(f"{len(found.skipped)} folder(s) already have a note and were left alone.")
        if found.missing:
            why.append("Not found: " + ", ".join(str(m) for m in found.missing))
        if include_outdated:
            why.append(left_alone(found))
        why = [w for w in why if w]
        QMessageBox.information(parent, title, nothing_found + ("\n\n" + "\n".join(why) if why else ""))
        return None

    targets = found.targets
    built: list[Proposal] = []
    failure: list[str] = []
    engine.phases.add("scan", time.perf_counter() - scan_started, count=len(targets))
    started = time.perf_counter()
    marks: list[float] = []  # when each folder was started: the gaps are how long the folders took

    def work(progress):
        # Runs on the worker thread: it only talks to the progress object (which marshals to the GUI thread).
        def report(done: int, total: int, _label: str) -> None:
            if done < total:
                now = time.perf_counter()
                marks.append(now)
                durations = [b - a for a, b in zip(marks, marks[1:])]
                progress.set_value(done)
                progress.set_label(f"Looking up {label_for(targets[done])} ({done + 1} of {total})…"
                                   + progress_suffix(done, total, now - started, durations))

        try:
            build(targets, progress=report, cancel=progress.should_cancel, on_proposal=built.append, engine=engine)
        except Exception as exc:  # noqa: BLE001 -- the proposals built so far are kept
            failure.append(str(exc) or exc.__class__.__name__)

    _result, cancelled = run_work_with_progress(
        parent, len(targets), f"Looking up the {what}…", work, cancellable=True, title=title)

    if not built:
        QMessageBox.information(parent, title, "Cancelled before anything was looked up." if cancelled
                                else "Nothing could be looked up." + (f"\n\n{failure[0]}" if failure else ""))
        return None
    session = ReviewSession(built, write_fn=write_fn)
    notes = list(intro_notes or [])
    if cancelled:
        notes.append(f"Cancelled: {len(built)} of {len(targets)} {what} were looked up; the rest are not in this list.")
    if failure:
        notes.append(f"The lookup stopped early: {failure[0]}")
    if found.skipped:
        notes.append(f"{len(found.skipped)} folder(s) already have a note and are not listed.")
    if include_outdated:
        updates = sum(1 for t in found.targets if getattr(t, "update", None) is not None)
        notes.append(f"Notes made by an older version: {updates} can be updated (never pre-ticked). " + left_alone(found))
    unreachable = [p for p in session.problems() if p.lower().startswith("could not reach")]
    if unreachable:
        notes.append(f"The internet services could not be reached: {unreachable[0]}")
    lookup_note = lookup_sentence(len(built), time.perf_counter() - started, engine.stats.requests, engine.stats.from_cache)
    notes.append(lookup_note)
    time_line = engine.phases.time_line()
    log.debug("%s | %s", lookup_note, engine.phases.detail())

    def rebuild(proposal: Proposal, candidate: Optional[Candidate]) -> Proposal:
        return engine.rebuild(proposal, candidate=candidate)

    dialog = NotesReviewDialog(
        session, choose_view(targets, owner_kinds=owner_kinds, child_kind=child_kind), rebuild, engine.can_choose,
        banner="  ".join(n for n in notes if n) + (f"\n{time_line}" if time_line else ""), parent=parent, title=title,
        size=parse_size(size_text), on_size=(lambda w, h: on_size(f"{w}x{h}")) if on_size else None, open_path=open_path,
        candidate_columns=candidate_columns, lookup_note=lookup_note, history_note=history_note)
    dialog.exec()
    dialog.deleteLater()  # parented to `parent`: otherwise it (and the whole ReviewSession) lives as long as the window
    return dialog

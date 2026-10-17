"""
redactor_common/core/save_report.py

The result of a Save All, in words, the same in every Redactor app: after EVERY Save All the user gets an
unambiguous answer -- a status-bar line with the counts (saved N, failed N, skipped N with load errors, still
unsaved N, or the reason nothing was saved) and, when anything failed or was skipped, a box listing each file with
its reason. A file that is still marked unsaved after a save attempt without a save error is a bug, so it is
named in the report instead of staying silent. Pure text logic, no Qt.

Opt-in logging (the owner's rule: logging user data is opt-in, default off): append_save_log() writes ONE line per
Save All -- counts and outcome only, never a file name -- and only when the caller says the data-logging
preference is on.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

NOTHING_NO_CHANGES = "Nothing to save: no file has unsaved changes"
MAX_NAMES = 5


def _names(names: Iterable[str], limit: int = MAX_NAMES) -> str:
    names = list(names)
    shown = ", ".join(names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


def nothing_to_save_reason(changed_count: int, load_error_names: list[str]) -> str:
    """Why a Save All with nothing to write wrote nothing."""
    if changed_count == 0 or not load_error_names:
        return NOTHING_NO_CHANGES
    return (f"Nothing to save: all {len(load_error_names)} changed file(s) have load errors "
            f"(they are never written): {_names(load_error_names)}")


@dataclass
class SaveReport:
    """What one Save All did. Fill it in as the save runs; `nothing` is set instead when there was nothing to do."""
    saved: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)  # (file name, reason)
    skipped_load_errors: list[str] = field(default_factory=list)  # file names
    still_dirty: list[str] = field(default_factory=list)  # still marked unsaved with no save error: a bug
    not_attempted: int = 0  # the user cancelled the run
    crashed: str = ""  # an unexpected exception stopped the run (its text)
    nothing: str = ""  # the reason nothing was saved

    @property
    def attempted(self) -> int:
        return self.saved + len(self.failed) + self.not_attempted

    @property
    def has_problems(self) -> bool:
        return bool(self.failed or self.skipped_load_errors or self.still_dirty or self.crashed)

    def status_line(self) -> str:
        if self.nothing:
            return self.nothing
        parts = [f"Saved {self.saved} file(s)"]
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        if self.skipped_load_errors:
            parts.append(f"{len(self.skipped_load_errors)} skipped (load errors)")
        if self.still_dirty:
            parts.append(f"{len(self.still_dirty)} still marked unsaved (a bug)")
        if self.not_attempted:
            parts.append(f"cancelled, {self.not_attempted} not attempted")
        if self.crashed:
            parts.append(f"stopped by an unexpected error: {self.crashed}")
        return ", ".join(parts)

    def problem_lines(self) -> list[str]:
        """One line per problem file with its reason, for the failure box ([] when all went well)."""
        lines = [f"{name}: {reason}" for name, reason in self.failed]
        lines += [f"{name}: skipped, it failed to load (a file with a load error is never written)"
                  for name in self.skipped_load_errors]
        if self.still_dirty:
            lines.append(f"{len(self.still_dirty)} file(s) are still marked unsaved after saving: "
                         f"{_names(self.still_dirty)}")
        if self.crashed:
            lines.append(f"Save All stopped because of an unexpected error: {self.crashed}")
        return lines

    def log_line(self) -> str:
        """Counts and outcome only: no file names."""
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        outcome = ("nothing to save" if self.nothing else "problems" if self.has_problems else "ok")
        return (f"{stamp} save-all outcome={outcome} saved={self.saved} failed={len(self.failed)} "
                f"skipped={len(self.skipped_load_errors)} still_dirty={len(self.still_dirty)} "
                f"not_attempted={self.not_attempted}")


def files_still_dirty(
    attempted: Iterable, is_dirty: Callable[[object], bool], has_error: Callable[[object], bool],
    name_of: Callable[[object], str],
) -> list[str]:
    """Names of attempted files that are still unsaved although no save error was recorded for them."""
    return [name_of(item) for item in attempted if is_dirty(item) and not has_error(item)]


def append_save_log(log_dir: str | Path, enabled: bool, report: SaveReport, filename: str = "save_log.txt") -> bool:
    """Appends report.log_line() to <log_dir>/<filename> ONLY when `enabled` (the opt-in data-logging
    preference). Never raises; returns whether a line was written."""
    if not enabled:
        return False
    try:
        folder = Path(log_dir)
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / filename, "a", encoding="utf-8") as handle:
            handle.write(report.log_line() + "\n")
        return True
    except OSError:
        return False

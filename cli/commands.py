"""
redactor_common/cli/commands.py

The command-line building blocks the apps share, so rename, move and redact behave the same way in every
Redactor app and are written once:

  - add_pattern_options(): -p/--pattern, --zero-pad, --ascii, -n/--dry-run
  - rename_items(), move_items(): plan from each item's fields, never overwrite, skip an item the pattern has
    no name for; finish_run() ends them with the summary and the exit code
  - add_redact_options(), parse_threshold(), build_recipe(), list_steps(), redact_items(): run the app's
    Redact recipe
  - trash_to(): a trash function that moves originals into a folder instead of the Recycle Bin
  - say(): one text line per result row

There is deliberately no undo for what the command line does (it is not recorded in the apps' Undo Last
Rename log): scripts are expected to preview with --dry-run, and nothing is ever overwritten or deleted for
good.

An app supplies small callables for what is its own (an item's path, its field values, why it can't be
processed); everything else is here. Rows are plain dicts: {"path", "status", "message", "new_path"} (Redact
rows have their own shape, see redact_items()).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import time
from typing import Any, Callable, Iterable, Sequence

from redactor_common.cli import EXIT_OK, EXIT_PARTIAL, CliError, Output
from redactor_common.core.move_plan import execute_move, plan_moves
from redactor_common.core.os_utils import rename_no_clobber
from redactor_common.core.pipeline import FileStatus, Recipe, Step, run_recipe
from redactor_common.core.rename_pattern import render_filename, unique_path
from redactor_common.core.trash import TrashError, move_to_trash

ItemPath = Callable[[Any], str]
Values = Callable[[Any], dict]
SkipReason = Callable[[Any], str]


def new_row(path: str) -> dict:
    return {"path": path, "status": "", "message": "", "new_path": ""}


def say(out: Output, row: dict, verb_width: int = 9) -> None:
    """The text line for a result row: status, path, "-> new path", "(message)"."""
    target = f"  ->  {row['new_path']}" if row.get("new_path") else ""
    note = f"  ({row['message']})" if row.get("message") else ""
    out.line(f"{row['status']:{verb_width}} {row['path']}{target}{note}")


def finish_run(out: Output, failed: int, **summary: Any) -> int:
    """The tail every file command shares: prints the JSON summary ({"failed": n, **summary}) and returns the
    exit code, EXIT_PARTIAL when any file failed."""
    out.finish({**summary, "failed": failed})
    return EXIT_PARTIAL if failed else EXIT_OK


def add_pattern_options(parser: argparse.ArgumentParser, pattern_required: bool) -> None:
    parser.add_argument(
        "-p", "--pattern", required=pattern_required, metavar="PATTERN",
        help="filename pattern with %%field%% tokens, e.g. \"%%artist%% - %%title%%\"",
    )
    parser.add_argument(
        "--zero-pad", type=int, default=None, metavar="N",
        help="pad the number to N digits (001); default: the choice saved in the app's Rename dialog",
    )
    parser.add_argument(
        "--ascii", action="store_true",
        help="ASCII-safe names (é -> e, æ -> ae, ...); also on when the app's Rename dialog has it saved",
    )
    parser.add_argument("-n", "--dry-run", action="store_true", help="show what would happen, change nothing")


def trash_to(folder: str) -> Callable[[str], None]:
    """A trash function that moves a file into `folder` (numbered if the name is taken). The folder is created
    when the first file arrives, so a dry run or a --list-steps leaves nothing behind. A failure is a TrashError,
    like the Recycle Bin's, so the callers that expect one (a repair, a commit) handle it."""

    def trash(path: str) -> None:
        stem, ext = os.path.splitext(os.path.basename(path))
        try:
            os.makedirs(folder, exist_ok=True)
            target, n = os.path.join(folder, stem + ext), 2
            while os.path.lexists(target):
                target = os.path.join(folder, f"{stem} ({n}){ext}")
                n += 1
            shutil.move(path, target)
        except OSError as exc:
            raise TrashError(f"couldn't move it to {folder}: {exc}") from exc

    return trash


def trash_with_retries(trash: Callable[[str], None] = move_to_trash, attempts: int = 8, delay: float = 0.25) -> Callable[[str], None]:
    """`trash` tried again for a moment when it fails: on Windows an antivirus scan or the search indexer can hold
    a file for an instant. A file that is gone after a failed attempt was already taken (the shell can report an
    error after doing the move). A lasting failure still raises TrashError."""

    def wrapper(path: str) -> None:
        for attempt in range(attempts):
            try:
                trash(path)
                return
            except TrashError:
                if not os.path.exists(path):
                    return
                if attempt == attempts - 1:
                    raise
                time.sleep(delay)

    return wrapper


def check_pattern_tokens(pattern: str, items: Sequence[Any], values_for: Values) -> None:
    """A usage error for a %token% no item has a value for (a typo such as %tittle% would silently render as
    nothing). Skipped when there are no items."""
    tokens = set(re.findall(r"%(\w+)%", pattern))
    if not tokens or not items:
        return
    known: set[str] = set()
    for item in items:
        known |= set(values_for(item))
    unknown = sorted(tokens - known)
    if unknown:
        raise CliError(
            f"unknown token {', '.join('%' + t + '%' for t in unknown)} in the pattern. Tokens: "
            + ", ".join(f"%{k}%" for k in sorted(known))
        )


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


# --- rename ---------------------------------------------------------------------------------------------


def rename_items(
    items: Sequence[Any], *, pattern: str, values_for: Values, path_of: ItemPath, skip_reason: SkipReason,
    out: Output, dry_run: bool, ascii_only: bool,
    on_renamed: Callable[[Any, str], None] | None = None,
    companions: Callable[[str, str], list[tuple[str, str]]] | None = None,
) -> int:
    """Renames each item in its own folder to `pattern` rendered from its values. Returns how many failed.
    A name that is taken gets (2), (3)...; nothing is overwritten. A change of letter case alone
    ("song.mp3" to "Song.mp3") is a rename. `on_renamed(item, new_path)` lets the app keep its in-memory
    item in step. `companions(old, new)` lists the other files that belong to the item by name (a video's
    poster and subtitles) as (old, new) pairs; they are renamed with it, and never replace an existing file."""
    if "/" in pattern or "\\" in pattern:
        raise CliError("a rename pattern cannot contain / or \\ (folders are made by the move command)")
    check_pattern_tokens(pattern, items, values_for)
    taken: set[str] = set()
    failed = 0
    for index, item in enumerate(items, start=1):
        path = path_of(item)
        out.progress(index, len(items), path)
        row = new_row(path)
        reason = skip_reason(item)
        if reason:
            row["status"], row["message"] = "skipped", reason
        else:
            # fallback="": a file the pattern has nothing for is skipped, not renamed to "untitled"
            stem = render_filename(values_for(item), pattern, fallback="", ascii_only=ascii_only)
            if not stem.strip():
                row["status"], row["message"] = "skipped", "the pattern gives an empty name for this file"
            else:
                new_path = unique_path(os.path.dirname(path), stem, os.path.splitext(path)[1], taken, path)
                taken.add(os.path.normcase(os.path.abspath(new_path)))
                row["new_path"] = new_path
                if os.path.abspath(new_path) == os.path.abspath(path):
                    row["status"], row["new_path"] = "unchanged", ""
                else:
                    failed += _rename_one(item, path, new_path, row, dry_run, on_renamed, companions)
        out.record(row)
        say(out, row)
    return failed


def _rename_one(item, path, new_path, row, dry_run, on_renamed, companions) -> int:
    """Renames one file (and its companions) and fills in its row. Returns 1 if the rename failed."""
    extra = companions(path, new_path) if companions else []
    note = f"+{len(extra)} companion file(s)" if extra else ""
    if dry_run:
        row["status"], row["message"] = "planned", note
        return 0
    case_only = _same_path(path, new_path)  # the same file under another spelling: not a collision
    try:
        if case_only:
            os.rename(path, new_path)
        else:
            rename_no_clobber(path, new_path)
    except OSError as exc:
        row["status"], row["message"], row["new_path"] = "failed", str(exc), ""
        return 1
    row["status"], row["message"] = "renamed", note
    problems = []
    for old, new in extra:
        try:
            if _same_path(old, new):
                os.rename(old, new)
            else:
                rename_no_clobber(old, new)
        except OSError as exc:
            problems.append(f"{os.path.basename(old)} not renamed ({exc})")
    if problems:
        row["message"] = "; ".join(filter(None, [note, *problems]))
    if on_renamed is not None:
        on_renamed(item, new_path)
    return 0


# --- move -------------------------------------------------------------------------------------------------


def _describe_error(exc: Exception, path: str) -> str:
    if isinstance(exc, FileNotFoundError):
        return f"the file is no longer there: {path}"
    if isinstance(exc, FileExistsError):
        return f"already exists: {exc}"
    return str(exc)


def move_items(
    items: Sequence[Any], *, root: str, pattern: str, values_for: Values, path_of: ItemPath,
    skip_reason: SkipReason, out: Output, dry_run: bool, copy: bool, ascii_only: bool,
    companions: Callable[[Any], list[Any]] | None = None,
    trash: Callable[[str], None] | None = None,
) -> int:
    """Moves (or copies) each item to root/pattern. Returns how many failed. Nothing is overwritten (a taken
    name gets (2)). `companions(planned_move)` lists the PlannedMoves of the other files that belong to the
    item by name (a video's poster and subtitles); they travel with it into the same folders. `trash` is
    what takes the original after a verified copy across volumes (the Recycle Bin, with retries, unless the
    caller says otherwise)."""
    trash = trash or trash_with_retries()
    if not root:
        raise CliError("no library folder: give --root FOLDER (or set one in the app's Rename / Export / Move window)")
    if not os.path.isdir(root):
        raise CliError(f"the library folder does not exist: {root}")
    check_pattern_tokens(pattern, items, values_for)
    reasons = [skip_reason(item) for item in items]  # once per item: a caller's rule may be costly
    movable = [item for item, reason in zip(items, reasons) if not reason]
    plans = plan_moves(movable, root, pattern, values_for, path_of, copy=copy, ascii_only=ascii_only)
    by_path = {p.old_path: p for p in plans}
    failed = 0
    for index, (item, reason) in enumerate(zip(items, reasons), start=1):
        path = path_of(item)
        out.progress(index, len(items), path)
        row = new_row(path)
        plan = by_path.get(path)
        if plan is None:
            row["status"], row["message"] = "skipped", reason
        elif plan.blocking:
            row["status"], row["message"] = "failed", plan.warning
            failed += 1
        elif plan.is_noop:
            row["status"] = "unchanged"
        elif plan.nameless:  # the planner would call it "untitled"; a batch should not do that silently
            row["status"], row["message"] = "skipped", "the pattern gives an empty name for this file"
        else:
            row["new_path"] = plan.new_path
            failed += _move_one(plan, row, dry_run, copy, companions, trash)
        out.record(row)
        say(out, row)
    return failed


def _move_one(plan, row, dry_run: bool, copy: bool, companions, trash) -> int:
    """Moves (or copies) one planned file and its companions and fills in its row. Returns 1 on failure."""
    extra = companions(plan) if companions else []
    problems = [plan.warning] if plan.warning else []
    note = f"+{len(extra)} companion file(s)" if extra else ""
    if dry_run:
        row["status"], row["message"] = "planned", "; ".join(filter(None, [*problems, note]))
        return 0
    try:
        result = execute_move(plan.old_path, plan.new_path, copy=copy, trash=trash)
    except (OSError, ValueError) as exc:
        row["status"], row["message"] = "failed", _describe_error(exc, plan.old_path)
        return 1
    row["status"] = "copied" if copy else "moved"
    if result.warning:
        problems.append(result.warning)
    for companion in extra:
        try:
            done = execute_move(companion.old_path, companion.new_path, copy=copy, trash=trash)
        except (OSError, ValueError) as exc:
            problems.append(f"{os.path.basename(companion.old_path)} not moved ({_describe_error(exc, companion.old_path)})")
            continue
        if done.warning:
            problems.append(done.warning)
    row["message"] = "; ".join(filter(None, [*problems, note]))
    return 0


# --- redact -------------------------------------------------------------------------------------------------


def add_redact_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--recipe", metavar="FILE", help="a recipe JSON file (default: the one saved in the app)")
    parser.add_argument("--enable", action="append", default=[], metavar="STEP", help="turn a step on for this run (repeatable)")
    parser.add_argument("--disable", action="append", default=[], metavar="STEP", help="turn a step off for this run (repeatable)")
    parser.add_argument(
        "--threshold", metavar="N",
        help="confidence needed to apply a guess: 0-1 (0.9), or a percentage (90 or 90%%)",
    )
    parser.add_argument("--list-steps", action="store_true", help="show the steps and whether the recipe has them on, then stop")
    parser.add_argument("--trash-dir", metavar="FOLDER", help="move originals here instead of the Recycle Bin")


def read_recipe_file(path: str) -> str:
    """The text of a recipe file, checked: it must be a JSON object with the keys a saved recipe has. Recipe
    parsing otherwise turns anything unreadable into the DEFAULT recipe, which would run steps the file never
    asked for."""
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise CliError(f"cannot read the recipe file: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise CliError(f"the recipe file is not valid JSON ({exc})") from exc
    if not isinstance(data, dict) or not ({"order", "enabled", "options"} & set(data)):
        raise CliError("the recipe file is not a recipe (a JSON object with order, enabled and options)")
    return text


def parse_threshold(raw: Any) -> float:
    """A confidence threshold as a fraction. 0 to 1 is a fraction ("0.9", "1" is 100%), "90%" and plain numbers
    from 5 to 100 are percentages; a plain number between 1 and 5 is ambiguous ("1.5": 1.5% or 150%?) and an
    error, as is anything outside 0-100%."""
    text = str(raw).strip()
    percent = text.endswith("%")
    try:
        number = float(text.rstrip("%"))
    except ValueError:
        raise CliError(f"--threshold must be a number, not {text!r}") from None
    if percent:
        value = number / 100
    elif number <= 1:
        value = number
    elif number < 5:
        raise CliError(f"--threshold {text} is ambiguous: write {number / 100:g} or {text}% for a percentage")
    else:
        value = number / 100
    if not 0 <= value <= 1:
        raise CliError("--threshold must be between 0 and 1 (or 0% and 100%)")
    return value


def build_recipe(args: argparse.Namespace, recipe: Recipe, catalogue: Iterable[Step]) -> Recipe:
    """`recipe` with this run's --enable / --disable / --threshold applied (it is changed in place and returned;
    an unknown step is a usage error)."""
    keys = {step.key for step in catalogue if not step.hidden}
    for key in args.enable + args.disable:
        if key not in keys:
            raise CliError(f"unknown step {key!r}. Steps: {', '.join(sorted(keys))}")
    for key in args.enable:
        recipe.enabled[key] = True
    for key in args.disable:
        recipe.enabled[key] = False
    if args.threshold is not None:
        recipe.confidence_threshold = parse_threshold(args.threshold)
    return recipe


def list_steps(recipe: Recipe, catalogue: Sequence[Step], out: Output) -> int:
    resolved = {step.key for step, _opts in recipe.resolve(catalogue)}
    for step in catalogue:
        if step.hidden:
            continue
        on = step.key in resolved
        out.record({"step": step.key, "label": step.label, "enabled": on})
        out.line(f"{'on ' if on else 'off'}  {step.key:22} {step.label}")
    out.finish({"confidence_threshold": recipe.confidence_threshold})
    return EXIT_OK


def redact_items(
    items: Sequence[Any], recipe: Recipe, catalogue: Iterable[Step], *, make_context: Callable[[Any], Any],
    describe: Callable[[Any], str], finalize: Callable, finalize_label: str, path_of: ItemPath, out: Output,
    after_run: Callable[[], Iterable[str] | None] | None = None,
) -> int:
    """Runs the recipe over `items` and reports. Returns EXIT_PARTIAL if any file failed, else EXIT_OK.
    `after_run()` is called once the run is over and may return run-wide notes to add to the report."""

    def progress(done: int, total: int, item: Any) -> None:
        if item is not None:
            out.progress(done + 1, total, describe(item))

    report = run_recipe(
        items, recipe, catalogue, make_context, progress=progress, describe=describe,
        finalize=finalize, finalize_label=finalize_label,
    )
    if after_run is not None:
        report.run_notes += list(after_run() or [])
    for entry in report.entries:
        out.record({
            "file": entry.file, "path": entry.saved_path or (path_of(entry.item) if entry.item is not None else ""),
            "status": entry.status.value, "applied": entry.applied, "needs_review": [
                {"step": r.step_label, "value": r.value, "confidence": r.confidence, "reason": r.reason}
                for r in entry.review
            ], "failures": entry.failures, "notes": entry.notes, "skipped": entry.skips, "not_saved": entry.not_saved,
        })
    failed = report.count(FileStatus.FAILED) + report.count(FileStatus.ABORTED)
    if not out.json_mode:
        out.line(report.to_text())
    out.finish({
        "files": len(report.entries), "failed": failed, "needs_review": len(report.needs_review()),
        "cancelled": report.cancelled, "confidence_threshold": report.confidence_threshold, "run_notes": report.run_notes,
    })
    return EXIT_PARTIAL if failed else EXIT_OK

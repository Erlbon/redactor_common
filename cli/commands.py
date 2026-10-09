"""
redactor_common/cli/commands.py

The command-line building blocks the apps share, so rename, move and redact behave the same way in every
Redactor app and are written once:

  - add_pattern_options(): -p/--pattern, --zero-pad, --ascii, -n/--dry-run
  - rename_items(), move_items(): plan from each item's fields, never overwrite, skip an item the pattern has
    no name for, record what was done in the app's undo log
  - add_redact_options(), build_recipe(), list_steps(), redact_items(): run the app's Redact recipe
  - trash_to(): a trash function that moves originals into a folder instead of the Recycle Bin
  - say(): one text line per result row

An app supplies small callables for what is its own (an item's path, its field values, why it can't be
processed); everything else is here. Rows are plain dicts: {"path", "status", "message", "new_path"} (Redact
rows have their own shape, see redact_items()).
"""

from __future__ import annotations

import argparse
import os
import shutil
from typing import Any, Callable, Iterable, Sequence

from redactor_common.cli import EXIT_OK, EXIT_PARTIAL, CliError, Output
from redactor_common.core.move_plan import execute_move, plan_moves
from redactor_common.core.pipeline import FileStatus, Recipe, Step, run_recipe
from redactor_common.core.rename_pattern import render_filename, unique_path
from redactor_common.core.trash import move_to_trash

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


def add_pattern_options(parser: argparse.ArgumentParser, pattern_required: bool) -> None:
    parser.add_argument(
        "-p", "--pattern", required=pattern_required, metavar="PATTERN",
        help="filename pattern with %%field%% tokens, e.g. \"%%artist%% - %%title%%\"",
    )
    parser.add_argument("--zero-pad", type=int, default=0, metavar="N", help="pad the number to N digits (001)")
    parser.add_argument("--ascii", action="store_true", help="ASCII-safe names (é -> e, æ -> ae, ...)")
    parser.add_argument("-n", "--dry-run", action="store_true", help="show what would happen, change nothing")


def trash_to(folder: str) -> Callable[[str], None]:
    """A trash function that moves a file into `folder` (created if needed; numbered if the name is taken)."""
    os.makedirs(folder, exist_ok=True)

    def trash(path: str) -> None:
        stem, ext = os.path.splitext(os.path.basename(path))
        target, n = os.path.join(folder, stem + ext), 2
        while os.path.lexists(target):
            target = os.path.join(folder, f"{stem} ({n}){ext}")
            n += 1
        shutil.move(path, target)

    return trash


# --- rename ---------------------------------------------------------------------------------------------


def rename_items(
    items: Sequence[Any], *, pattern: str, values_for: Values, path_of: ItemPath, skip_reason: SkipReason,
    out: Output, dry_run: bool, ascii_only: bool, log: Any, log_label: str,
    on_renamed: Callable[[Any, str], None] | None = None,
    companions: Callable[[str, str], list[tuple[str, str]]] | None = None,
) -> int:
    """Renames each item in its own folder to `pattern` rendered from its values. Returns how many failed.
    `on_renamed(item, new_path)` lets the app keep its in-memory item in step. `companions(old, new)` lists
    the other files that belong to the item by name (a video's poster and subtitles) as (old, new) pairs;
    they are renamed with it, and logged for undo."""
    taken: set[str] = set()
    renamed: list[tuple[str, str]] = []
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
                if os.path.normcase(os.path.abspath(new_path)) == os.path.normcase(os.path.abspath(path)):
                    row["status"], row["new_path"] = "unchanged", ""
                else:
                    extra = companions(path, new_path) if companions else []
                    note = f"+{len(extra)} companion file(s)" if extra else ""
                    if dry_run:
                        row["status"], row["message"] = "planned", note
                    else:
                        try:
                            os.rename(path, new_path)
                        except OSError as exc:
                            row["status"], row["message"], row["new_path"] = "failed", str(exc), ""
                            failed += 1
                        else:
                            row["status"], row["message"] = "renamed", note
                            renamed.append((path, new_path))
                            problems = []
                            for old, new in extra:
                                try:
                                    os.rename(old, new)
                                    renamed.append((old, new))
                                except OSError as exc:
                                    problems.append(f"{os.path.basename(old)} not renamed ({exc})")
                            if problems:
                                row["message"] = "; ".join(filter(None, [note, *problems]))
                            if on_renamed is not None:
                                on_renamed(item, new_path)
        out.record(row)
        say(out, row)
    if renamed:
        log.record(log_label, renamed)
    return failed


# --- move -------------------------------------------------------------------------------------------------


def move_items(
    items: Sequence[Any], *, root: str, pattern: str, values_for: Values, path_of: ItemPath,
    skip_reason: SkipReason, out: Output, dry_run: bool, copy: bool, ascii_only: bool, log: Any, log_label: str,
    companions: Callable[[Any], list[Any]] | None = None,
) -> int:
    """Moves (or copies) each item to root/pattern. Returns how many failed. `companions(planned_move)` lists
    the PlannedMoves of the other files that belong to the item by name (a video's poster and subtitles);
    they travel with it into the same folders."""
    if not root:
        raise CliError("no library folder: give --root FOLDER (or set one in the app's Rename / Export / Move window)")
    if not os.path.isdir(root):
        raise CliError(f"the library folder does not exist: {root}")
    movable = [item for item in items if not skip_reason(item)]
    plans = plan_moves(movable, root, pattern, values_for, path_of, copy=copy, ascii_only=ascii_only)
    by_path = {p.old_path: p for p in plans}
    failed = 0
    for index, item in enumerate(items, start=1):
        path = path_of(item)
        out.progress(index, len(items), path)
        row = new_row(path)
        plan = by_path.get(path)
        if plan is None:
            row["status"], row["message"] = "skipped", skip_reason(item)
        elif plan.blocking:
            row["status"], row["message"] = "failed", plan.warning
            failed += 1
        elif plan.is_noop:
            row["status"] = "unchanged"
        elif "empty" in plan.warning:  # the planner would call it "untitled"; a batch should not do that silently
            row["status"], row["message"] = "skipped", "the pattern gives an empty name for this file"
        else:
            row["new_path"] = plan.new_path
            extra = companions(plan) if companions else []
            problems = [plan.warning] if plan.warning else []
            note = f"+{len(extra)} companion file(s)" if extra else ""
            if dry_run:
                row["status"], row["message"] = "planned", "; ".join(filter(None, [*problems, note]))
            else:
                try:
                    result = execute_move(plan.old_path, plan.new_path, copy=copy, trash=move_to_trash)
                except (OSError, ValueError) as exc:
                    row["status"], row["message"] = "failed", str(exc)
                    failed += 1
                else:
                    row["status"] = "copied" if copy else "moved"
                    if result.warning:
                        problems.append(result.warning)
                    pairs = [] if result.original_kept else [(plan.old_path, result.new_path)]
                    created = list(result.created_dirs)
                    trashed = [(plan.old_path, result.new_path)] if result.original_trashed else []
                    for companion in extra:
                        try:
                            done = execute_move(companion.old_path, companion.new_path, copy=copy, trash=move_to_trash)
                        except (OSError, ValueError) as exc:
                            problems.append(f"{os.path.basename(companion.old_path)} not moved ({exc})")
                            continue
                        created += done.created_dirs
                        if not done.original_kept:
                            pairs.append((companion.old_path, done.new_path))
                        if done.original_trashed:
                            trashed.append((companion.old_path, done.new_path))
                        if done.warning:
                            problems.append(done.warning)
                    row["message"] = "; ".join(filter(None, [*problems, note]))
                    if not copy and pairs:
                        log.record(log_label, pairs, created_dirs=created, trashed=trashed, root=plan.root)
        out.record(row)
        say(out, row)
    return failed


# --- redact -------------------------------------------------------------------------------------------------


def add_redact_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--recipe", metavar="FILE", help="a recipe JSON file (default: the one saved in the app)")
    parser.add_argument("--enable", action="append", default=[], metavar="STEP", help="turn a step on for this run (repeatable)")
    parser.add_argument("--disable", action="append", default=[], metavar="STEP", help="turn a step off for this run (repeatable)")
    parser.add_argument("--threshold", type=float, metavar="N", help="confidence needed to apply a guess, 0-1 (or a percentage)")
    parser.add_argument("--list-steps", action="store_true", help="show the steps and whether the recipe has them on, then stop")
    parser.add_argument("--trash-dir", metavar="FOLDER", help="move originals here instead of the Recycle Bin")


def read_recipe_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise CliError(f"cannot read the recipe file: {exc}") from exc


def build_recipe(args: argparse.Namespace, recipe: Recipe, catalogue: Iterable[Step]) -> Recipe:
    """The recipe with this run's --enable / --disable / --threshold applied (an unknown step is a usage error)."""
    keys = {step.key for step in catalogue if not step.hidden}
    for key in args.enable + args.disable:
        if key not in keys:
            raise CliError(f"unknown step {key!r}. Steps: {', '.join(sorted(keys))}")
    for key in args.enable:
        recipe.enabled[key] = True
    for key in args.disable:
        recipe.enabled[key] = False
    if args.threshold is not None:
        value = args.threshold / 100 if args.threshold > 1 else args.threshold
        if not 0 <= value <= 1:
            raise CliError("--threshold must be between 0 and 1 (or 0 and 100)")
        recipe.confidence_threshold = value
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
    `after_run()` is called once the run is over (flush the app's undo log, say) and may return run-wide
    notes to add to the report."""

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

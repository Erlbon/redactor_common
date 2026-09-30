"""
redactor_common/core/pipeline.py

The engine behind every app's "Redact" button: run an ordered RECIPE of
steps on each loaded/selected file with no operator input, and report
what happened. Qt-free -- gui/redact_dialog.py wraps it in progress and
results dialogs.

Generalized via accessors, not tied to any app's data model: an "item"
is whatever the app loads (a book row, a path, ...), a "context" is
whatever `make_context(item)` builds for it, and a Step only ever talks
to its context. The engine never touches files itself, except through
commit_in_place() below, which a step (or the app's context) calls when
it has produced a corrected temp file.

Autonomy policy (decided by the user, 2026-09-30):
  - deterministic/reversible steps just run and return APPLIED;
  - a GUESS (language, series, lookup match...) is returned as a
    SUGGESTION carrying a confidence. The engine applies it through the
    step's own apply_suggestion() only when confidence >= the recipe's
    threshold (default 0.9); otherwise it is listed as NEEDS REVIEW in
    the report. Never silently guessed.

A step may also return StepResult.skipped(reason) -- "deliberately not
processed" (unsaved edits, a load error the app already explained): the
file ends as SKIPPED (neither a failure nor an abort), its remaining
steps and the finalize hook don't run, and the report lists it in its
own SKIPPED section. StepResult.nothing(note=...) attaches an FYI that
lands in the report's NOTES section.

Phases of one file (Step.position): "first" steps (guards/skip checks), the
normal steps in recipe order, "last" steps, then the `finalize` save, then
"after_save" steps (Rename/Move: they need the saved file, and may learn
its new path via `ctx.saved_path`). A batch-wide pre-pass (Step.prepare,
RunContext) runs once before the first file.

Isolation: one file's failure never affects another file. Inside a
file, a step that raises is recorded as FAILED and later steps still
run -- unless the step is `required`, in which case the file is
ABORTED (its remaining steps are skipped; steps only edit through the
context, and commit_in_place() is all-or-nothing, so the original is
left untouched).
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Sequence

from redactor_common.core.trash import move_to_trash

DEFAULT_CONFIDENCE_THRESHOLD = 0.9


# --- step declaration ------------------------------------------------------


class StepStatus(Enum):
    APPLIED = "applied"
    NOTHING = "nothing"
    SUGGESTION = "suggestion"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class StepResult:
    """What one step reports for one file. Build with the classmethods."""

    status: StepStatus
    changes: list[str] = field(default_factory=list)  # APPLIED: human-readable
    value: Any = None  # SUGGESTION: what would be applied
    confidence: float = 0.0  # SUGGESTION: 0..1
    reason: str = ""  # SUGGESTION: why the step thinks so
    message: str = ""  # FAILED: what went wrong; SKIPPED: why
    note: str = ""  # any non-failed status: an FYI for the report's NOTES section
    # finalize / after_save steps only: the file's path after this step
    # (a save that changed the extension, a rename, a move). The engine
    # exposes it as ctx.saved_path and FileReport.saved_path.
    new_path: str | None = None

    @classmethod
    def applied(cls, *changes: str, new_path: str | None = None) -> "StepResult":
        return cls(StepStatus.APPLIED, changes=[c for c in changes if c], new_path=new_path)

    @classmethod
    def nothing(cls, note: str = "") -> "StepResult":
        """Nothing to do. `note` (optional) is a message for the NOTES
        section of the report, e.g. "no cover art found"."""
        return cls(StepStatus.NOTHING, note=note)

    @classmethod
    def suggestion(cls, value: Any, confidence: float, reason: str = "") -> "StepResult":
        return cls(StepStatus.SUGGESTION, value=value, confidence=float(confidence), reason=reason)

    @classmethod
    def failed(cls, message: str) -> "StepResult":
        return cls(StepStatus.FAILED, message=message)

    @classmethod
    def skipped(cls, reason: str) -> "StepResult":
        """The file was deliberately not processed (not a failure). The
        engine stops the file here and marks it SKIPPED."""
        return cls(StepStatus.SKIPPED, message=reason)


@dataclass(frozen=True)
class OptionSpec:
    """One per-step option, declared generically so the recipe editor can
    build a widget for it without knowing the step. kind is "bool",
    "int", "float", "choice" (`choices` = allowed string values) or
    "str" (single-line text; `max_length`, if set, is a hard limit --
    a longer stored value falls back to the default)."""

    key: str
    label: str
    kind: str = "bool"
    default: Any = False
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    tooltip: str = ""
    max_length: int | None = None  # "str" only
    # "str" pattern options only (all optional; the recipe JSON is unchanged,
    # it still stores just the string): `suggestions` = the app's pattern
    # history, newest first; `fallback` = the pattern used when the stored
    # value is empty (described to the user by `fallback_label`); `preview`
    # renders an example result for a pattern. The editor shows these as a
    # "pattern trail"; a saved non-empty value is always kept as saved.
    suggestions: Callable[[], list[str]] | None = None
    fallback: Callable[[], str] | None = None
    fallback_label: str = ""
    preview: Callable[[str], str] | None = None

    def coerce(self, raw: Any) -> Any:
        """Validate a stored/edited value; fall back to the default when
        it's the wrong type or out of range (a hand-edited settings file
        must never crash the app)."""
        try:
            if self.kind == "bool":
                return raw if isinstance(raw, bool) else self.default
            if self.kind in ("int", "float"):
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    return self.default
                value = int(raw) if self.kind == "int" else float(raw)
                if self.minimum is not None and value < self.minimum:
                    return self.default
                if self.maximum is not None and value > self.maximum:
                    return self.default
                return value
            if self.kind == "choice":
                return raw if raw in self.choices else self.default
            if self.kind == "str":
                if not isinstance(raw, str):
                    return self.default
                if self.max_length is not None and len(raw) > self.max_length:
                    return self.default
                return raw
        except (TypeError, ValueError):
            pass
        return self.default


def effective_option_source(spec: OptionSpec, stored_value: Any) -> tuple[str, str]:
    """(pattern in effect, where it came from) for a "str" option.
    A non-empty stored value wins ("set in this recipe"); an empty one
    follows `spec.fallback` ("follows: <fallback_label>"); with no fallback
    it is the spec default ("default")."""
    stored = stored_value if isinstance(stored_value, str) else ""
    if stored:
        return stored, "set in this recipe"
    if spec.fallback is not None:
        try:
            value = spec.fallback()
        except Exception:
            value = ""
        label = spec.fallback_label or "the app's current setting"
        return (value if isinstance(value, str) else ""), f"follows: {label}"
    return (spec.default if isinstance(spec.default, str) else ""), "default"


class Step:
    """Base class for a step. Subclass, set the class attributes, and
    override run(); override apply_suggestion() too if run() can return
    a SUGGESTION. `ctx` is whatever the app's make_context() built;
    this step's current options come from `self.options_for(ctx)` (the
    supported accessor; it's also `ctx.step_options`, a dict with every
    declared option present).

    `default_enabled` is a class attribute; to vary it per instance pass
    it to the constructor (`MyStep(default_enabled=False)`) or assign it
    on the instance. A subclass with its own __init__ need not call
    super().__init__() unless it wants the kwarg.

    `position` is one of (recipe order is honoured inside each group, the
    groups themselves always run in this order, whatever the stored order):
      "first"      before every normal step -- guards that may return
                   StepResult.skipped (unsaved edits, load error, DRM);
      "normal"     in recipe order;
      "last"       after every normal step, before the save;
      "after_save" after `finalize` succeeded and the file was actually
                   saved/changed (Rename, Move). Skipped with a note when
                   the file was unchanged/skipped/failed, unless
                   `run_when_unchanged` is True. A failure marks the file
                   FAILED under the step's own label, but what was saved
                   stays saved and is still reported as applied. It gets the
                   same ctx; `ctx.saved_path` is the file's current path
                   (see StepResult.new_path). A SKIPPED result here is just
                   a note. A `required` failure stops the remaining
                   after_save steps (the file is not ABORTED: it was saved).
    The editor pins each group in its place.

    `after` is a tuple of step keys this step must run after ("move" after
    ("rename",)): enforced by ordered_keys()/Recipe.resolve() whatever the
    stored or dragged order; the editor refuses the illegal move. A key the
    catalogue lacks is ignored; a key in a LATER position group, or a
    cycle, is a programming error (ValueError).

    `hidden = True`: an internal step the engine always runs (whatever the
    stored recipe says) but the editor doesn't list and recipes never store.

    `prepare(items, env)` is the optional batch pre-pass, see below."""

    key: str = ""
    label: str = ""
    description: str = ""
    default_enabled: bool = True
    required: bool = False  # a failure aborts the file (original untouched)
    options: Sequence[OptionSpec] = ()
    position: str = "normal"  # "first" | "normal" | "last" | "after_save"
    after: tuple[str, ...] = ()  # keys of steps this one must run after
    hidden: bool = False  # run by the engine, not listed/stored by the editor/recipe
    run_when_unchanged: bool = False  # "after_save" only: run even if nothing was saved

    def __init__(self, *, default_enabled: bool | None = None) -> None:
        if default_enabled is not None:
            self.default_enabled = default_enabled

    def options_for(self, ctx: Any) -> dict[str, Any]:
        """This step's current, validated options while run() is called
        for `ctx`: every declared option is present (declared defaults
        fill any the context lacks, e.g. when called outside the engine)."""
        current = getattr(ctx, "step_options", None)
        if not isinstance(current, dict):
            current = {}
        return {o.key: o.coerce(current.get(o.key, o.default)) for o in self.options}

    def prepare(self, items: Sequence[Any], env: Any) -> Any:
        """Optional batch pre-pass: called ONCE per run, for enabled steps
        only, with every run item (in run order) and the `env` the caller
        gave run_redact/run_recipe, before the first file is processed
        (e.g. build the folder-value counts a path-tag step corroborates
        against). Keep it cheap, or do per-file work under your own
        progress. A non-None return value is stored as
        `ctx.batch[self.key]` for run(); `ctx.batch` is also a free dict
        for anything else. If it raises, the step is marked unavailable
        for this run (skipped for every file, one line in the report's
        RUN NOTES) and the run goes on."""
        return None

    def run(self, ctx: Any) -> StepResult:
        raise NotImplementedError

    def apply_suggestion(self, ctx: Any, result: StepResult) -> str | Iterable[str] | None:
        """Apply `result.value` (only called when its confidence met the
        threshold). Return a description of the change (or several)."""
        raise NotImplementedError(f"{self.key or type(self).__name__} can't apply suggestions")


def _index_catalogue(catalogue: Iterable[Step]) -> dict[str, Step]:
    by_key: dict[str, Step] = {}
    for step in catalogue:
        if not step.key:
            raise ValueError(f"step {type(step).__name__} has no key")
        if step.key in by_key:
            raise ValueError(f"duplicate step key {step.key!r}")
        by_key[step.key] = step
    for step in by_key.values():
        for dep in step.after:
            if dep in by_key and _rank(by_key[dep]) > _rank(step):
                raise ValueError(f"step {step.key!r} must run after {dep!r}, which is in a later position group")
    return by_key


_POSITION_RANK = {"first": 0, "normal": 1, "last": 2, "after_save": 3}


def _rank(step: Step) -> int:
    return _POSITION_RANK.get(step.position, 1)


def ordered_keys(stored_order: Iterable[str], catalogue: Iterable[Step]) -> list[str]:
    """The run order for `stored_order` against `catalogue`: stored keys
    the catalogue still has keep their stored order; catalogue steps the
    stored order lacks (added by a newer app version, and every `hidden`
    step) are inserted right after their nearest preceding catalogue step
    that is already placed (or first, if none precedes them) rather than
    appended at the end; then the position groups are applied (stable:
    "first", normal, "last", "after_save"); finally each step's `after`
    keys are enforced by a stable topological sort (a step only moves as
    far as it must)."""
    steps = list(catalogue)
    by_key = _index_catalogue(steps)
    keys = list(dict.fromkeys(k for k in stored_order if k in by_key and not by_key[k].hidden))
    placed = set(keys)
    for i, step in enumerate(steps):
        if step.key in placed:
            continue
        at = 0
        for prev in reversed(steps[:i]):
            if prev.key in placed:
                at = keys.index(prev.key) + 1
                break
        keys.insert(at, step.key)
        placed.add(step.key)
    keys = [k for r in range(4) for k in keys if _rank(by_key[k]) == r]
    if not any(step.after for step in steps):
        return keys
    out: list[str] = []
    pending = list(keys)
    while pending:
        for k in pending:
            if all(d in out or d not in by_key for d in by_key[k].after):
                out.append(k)
                pending.remove(k)
                break
        else:
            raise ValueError("the steps' `after` constraints form a cycle: " + ", ".join(pending))
    return out


# --- recipe ----------------------------------------------------------------


@dataclass
class Recipe:
    """Ordered step keys + per-step enabled flags and options + the
    confidence threshold. Steps are identified by key only, so a recipe
    saved by an older app version keeps working: keys the catalogue no
    longer has are skipped, steps it gained are inserted at their
    catalogue position (at their default_enabled state), and steps with
    position "last" always run after the normal ones."""

    order: list[str] = field(default_factory=list)
    enabled: dict[str, bool] = field(default_factory=dict)
    options: dict[str, dict[str, Any]] = field(default_factory=dict)
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD

    @classmethod
    def default_for(cls, catalogue: Iterable[Step]) -> "Recipe":
        steps = list(catalogue)
        hidden = {s.key for s in steps if s.hidden}
        return cls(
            order=[k for k in ordered_keys([s.key for s in steps], steps) if k not in hidden],
            enabled={s.key: s.default_enabled for s in steps if not s.hidden},
            options={s.key: {o.key: o.default for o in s.options} for s in steps if not s.hidden},
        )

    def resolve(self, catalogue: Iterable[Step]) -> list[tuple[Step, dict[str, Any]]]:
        """The enabled steps in run order (position groups and `after`
        constraints applied; `hidden` steps always included), each with its
        validated, fully-populated options dict."""
        by_key = _index_catalogue(catalogue)
        out: list[tuple[Step, dict[str, Any]]] = []
        for key in ordered_keys(self.order, by_key.values()):
            step = by_key[key]
            if not step.hidden and not self.enabled.get(key, step.default_enabled):
                continue
            stored = {} if step.hidden else self.options.get(key, {})
            opts = {o.key: o.coerce(stored.get(o.key, o.default)) for o in step.options}
            out.append((step, opts))
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "order": list(self.order),
            "enabled": dict(self.enabled),
            "options": {k: dict(v) for k, v in self.options.items()},
            "confidence_threshold": self.confidence_threshold,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Recipe":
        """Tolerant: unknown keys are ignored, missing ones default, a
        wrong-typed value falls back to its default."""
        recipe = cls()
        if not isinstance(data, dict):
            return recipe
        order = data.get("order")
        if isinstance(order, list):
            recipe.order = [k for k in order if isinstance(k, str)]
        enabled = data.get("enabled")
        if isinstance(enabled, dict):
            recipe.enabled = {k: v for k, v in enabled.items() if isinstance(k, str) and isinstance(v, bool)}
        options = data.get("options")
        if isinstance(options, dict):
            recipe.options = {k: dict(v) for k, v in options.items() if isinstance(k, str) and isinstance(v, dict)}
        threshold = data.get("confidence_threshold")
        if isinstance(threshold, (int, float)) and not isinstance(threshold, bool) and 0.0 <= threshold <= 1.0:
            recipe.confidence_threshold = float(threshold)
        return recipe

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_json(cls, text: str | None) -> "Recipe":
        """Garbage or empty text yields the empty recipe (= every step
        at its default), never an exception."""
        try:
            return cls.from_dict(json.loads(text or ""))
        except (ValueError, TypeError):
            return cls()


# --- report ----------------------------------------------------------------


class FileStatus(Enum):
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    NEEDS_REVIEW = "needs review"  # nothing applied, but guesses are waiting
    FAILED = "failed"  # at least one step failed (applied changes still listed)
    ABORTED = "aborted"  # a required step failed; original left untouched
    SKIPPED = "skipped"  # deliberately not processed (a step said so); not a failure


@dataclass
class ReviewItem:
    step_key: str
    step_label: str
    value: Any
    confidence: float
    reason: str = ""


@dataclass
class FileReport:
    file: str
    item: Any = None
    status: FileStatus = FileStatus.UNCHANGED
    applied: list[str] = field(default_factory=list)
    review: list[ReviewItem] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    duration: float = 0.0
    notes: list[str] = field(default_factory=list)  # FYIs ("<step>: <note>")
    skips: list[str] = field(default_factory=list)  # why the file was skipped
    # Changes that steps applied but the final save never wrote (finalize
    # failed or skipped): the engine moves them here out of `applied`.
    not_saved: list[str] = field(default_factory=list)
    saved_path: str | None = None  # the file's path after save/rename/move, when a step reported one


@dataclass
class RedactReport:
    entries: list[FileReport] = field(default_factory=list)
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    cancelled: bool = False
    not_processed: int = 0  # files skipped because the run was cancelled
    duration: float = 0.0
    run_notes: list[str] = field(default_factory=list)  # run-wide FYIs (a step unavailable for this run)

    def count(self, status: FileStatus) -> int:
        return sum(1 for e in self.entries if e.status is status)

    def needs_review(self) -> list[FileReport]:
        return [e for e in self.entries if e.review]

    def with_notes(self) -> list[FileReport]:
        return [e for e in self.entries if e.notes]

    def skipped(self) -> list[FileReport]:
        return [e for e in self.entries if e.status is FileStatus.SKIPPED]

    def to_text(self) -> str:
        lines = ["Redact report", "============="]
        summary = [f"{len(self.entries)} file(s)"]
        for status in FileStatus:
            n = self.count(status)
            if n:
                summary.append(f"{n} {status.value}")
        lines.append(", ".join(summary))
        lines.append(f"Auto-apply guesses at confidence >= {self.confidence_threshold:.0%}")
        lines.append(f"Took {self.duration:.1f} s")
        if self.cancelled:
            lines.append(f"CANCELLED: {self.not_processed} file(s) were not processed.")

        changed = [e for e in self.entries if e.applied]
        lines += ["", "CHANGES", "-------"]
        if not changed:
            lines.append("(nothing was changed)")
        for e in changed:
            lines.append(f"{e.file}")
            lines += [f"  - {c}" for c in e.applied]

        reviews = self.needs_review()
        lines += ["", "NEEDS REVIEW  (guesses below the threshold -- NOT applied)", "-" * 58]
        if not reviews:
            lines.append("(none)")
        for e in reviews:
            lines.append(f"{e.file}")
            for r in e.review:
                lines.append(f"  ? {r.step_label}: {r.value}  ({r.confidence:.0%})")
                if r.reason:
                    lines.append(f"      because: {r.reason}")

        problems = [e for e in self.entries if e.failures]
        lines += ["", "FAILURES", "--------"]
        if not problems:
            lines.append("(none)")
        for e in problems:
            head = f"{e.file}"
            if e.status is FileStatus.ABORTED:
                head += "  [aborted -- original left untouched]"
            lines.append(head)
            lines += [f"  ! {f}" for f in e.failures]

        # Only when non-empty, so a report from code that never uses
        # notes/skips/not_saved is text-identical to what it always was.
        unsaved = [e for e in self.entries if e.not_saved]
        if unsaved:
            head = "NOT SAVED  (applied in memory, the save failed or was skipped)"
            lines += ["", head, "-" * len(head)]
            for e in unsaved:
                lines.append(f"{e.file}")
                lines += [f"  - {c}" for c in e.not_saved]
        if self.run_notes:
            lines += ["", "RUN NOTES", "---------"]
            lines += [f"  * {n}" for n in self.run_notes]
        noted = self.with_notes()
        if noted:
            lines += ["", "NOTES", "-----"]
            for e in noted:
                lines.append(f"{e.file}")
                lines += [f"  * {n}" for n in e.notes]
        skipped = self.skipped()
        if skipped:
            lines += ["", "SKIPPED  (deliberately not processed)", "-" * 37]
            for e in skipped:
                lines.append(f"{e.file}")
                lines += [f"  - {r}" for r in e.skips]
        return "\n".join(lines) + "\n"


# --- engine ----------------------------------------------------------------


def _as_changes(value: str | Iterable[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return [str(v) for v in value if v]


def _set_options(ctx: Any, opts: dict[str, Any]) -> None:
    """Private setter the engine uses (kept for compatibility); steps read
    the result through Step.options_for(ctx)."""
    try:
        ctx.step_options = opts
    except AttributeError:
        pass  # a context that can't take it just doesn't get option access


FinalizeFn = Callable[[Any, FileReport], "StepResult | None"]


@dataclass
class RunContext:
    """What one Redact run shares across its files. Built by begin_run()
    (run_redact/run_recipe do it for you); every file's context gets it as
    `ctx.run_context`, and `ctx.batch` is its `batch` dict.

    `items`/`env`: the run items and the app's own object (settings,
    trash function, ...) given to prepare(). `batch`: free storage; a
    step's prepare() return value lands in `batch[step.key]`, e.g.
    `Step.prepare` returning the folder value counts, read in run() as
    `ctx.batch[self.key]`. `unavailable`: step key -> why its prepare()
    failed (the engine skips that step for every file); `notes`: the
    matching lines for the report's RUN NOTES."""

    items: list[Any] = field(default_factory=list)
    env: Any = None
    batch: dict[str, Any] = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def begin_run(items: Iterable[Any], resolved: list[tuple[Step, dict[str, Any]]], env: Any = None) -> RunContext:
    """Build the RunContext and call prepare(items, env) once on each
    resolved (enabled) step. Never raises: a prepare() that raises marks
    its step unavailable for the run, with a note."""
    run = RunContext(items=list(items), env=env)
    for step, _opts in resolved:
        prepare = getattr(step, "prepare", None)
        if not callable(prepare):
            continue
        try:
            value = prepare(run.items, env)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            run.unavailable[step.key] = reason
            run.notes.append(f"{step.label or step.key}: unavailable for this run ({reason})")
            continue
        if value is not None:
            run.batch[step.key] = value
    return run


def run_recipe_on_item(
    item: Any,
    resolved: list[tuple[Step, dict[str, Any]]],
    threshold: float,
    make_context: Callable[[Any], Any],
    describe: Callable[[Any], str] = str,
    finalize: FinalizeFn | None = None,
    finalize_label: str = "Final save",
    run: "RunContext | None" = None,
) -> FileReport:
    """One file through the whole recipe. Never raises (short of
    KeyboardInterrupt): every failure lands in the returned FileReport.
    `resolved` comes from Recipe.resolve(). Exposed so the GUI can drive
    the per-file loop through run_with_progress().

    `finalize(ctx, file_report)` (optional) runs once after all "first",
    normal and "last" steps -- only if the file wasn't aborted or skipped
    -- typically to save the context once. It returns a StepResult (or
    None = nothing); its changes join `applied` under `finalize_label`, a
    failure (or exception) marks the file FAILED under that label. The
    file report so far is passed so it can tell whether anything changed.
    If it returns `StepResult.applied(..., new_path=...)` the file's new
    path becomes `ctx.saved_path` / `file_report.saved_path`. When
    finalize fails or skips, the changes the steps had applied move from
    `applied` to `not_saved` (the report's NOT SAVED section).

    Then the "after_save" steps run (see Step), if finalize (when given)
    neither failed nor skipped and something was applied/saved.

    `run` (optional RunContext from begin_run()) carries the batch dict and
    the steps marked unavailable by their prepare(); the context gets it as
    `ctx.run_context` and `ctx.batch` (only set where the context doesn't
    already define those names)."""
    started = time.monotonic()
    try:
        name = describe(item)
    except Exception:
        name = repr(item)
    entry = FileReport(file=name, item=item)
    ctx = None
    aborted = False
    skipped = False
    try:
        try:
            ctx = make_context(item)
            _attach_run(ctx, run)
        except Exception as exc:
            entry.failures.append(f"couldn't prepare the file: {exc}")
            aborted = True
        # Groups are separated here too, so a hand-built `resolved` gets
        # "first" before and "after_save" after everything else; every
        # other step keeps the order it was given (old behaviour).
        before = [r for r in resolved if r[0].position == "first"]
        after_save = [r for r in resolved if r[0].position == "after_save"]
        main = [r for r in resolved if r[0].position not in ("first", "after_save")]
        for step, opts in (before + main if not aborted else ()):
            if run is not None and step.key in run.unavailable:
                continue
            label = step.label or step.key
            failure = _run_step(step, opts, threshold, ctx, label, entry)
            if failure and step.required:
                aborted = True
                break
            if entry.skips:
                skipped = True
                break
        save_bad = False
        if finalize is not None and not aborted and not skipped:
            before_counts = (len(entry.failures), len(entry.skips))
            _run_finalize(finalize, ctx, entry, finalize_label)
            skipped = bool(entry.skips)  # finalize may skip too
            save_bad = (len(entry.failures), len(entry.skips)) != before_counts
            if save_bad and entry.applied:
                entry.not_saved += entry.applied
                entry.applied = []
        if after_save:
            _run_after_save(after_save, threshold, ctx, entry, run, aborted, skipped, save_bad)
    finally:
        closer = getattr(ctx, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception as exc:
                entry.failures.append(f"cleanup failed: {exc}")
        entry.duration = time.monotonic() - started

    if aborted:
        entry.status = FileStatus.ABORTED
    elif entry.failures:
        entry.status = FileStatus.FAILED
    elif skipped:
        entry.status = FileStatus.SKIPPED
    elif entry.applied:
        entry.status = FileStatus.CHANGED
    elif entry.review:
        entry.status = FileStatus.NEEDS_REVIEW
    return entry


def _run_finalize(finalize: FinalizeFn, ctx: Any, entry: FileReport, label: str) -> None:
    try:
        result = finalize(ctx, entry)
        if result is None:
            return
        if not isinstance(result, StepResult):
            raise TypeError("finalize returned no StepResult")
        if not _fold_result(result, label, entry):
            _note_path(result, ctx, entry)
    except Exception as exc:
        entry.failures.append(f"{label}: {type(exc).__name__}: {exc}")


def _note_path(result: StepResult, ctx: Any, entry: FileReport) -> None:
    """A result that reports the file's new path: remember it on the
    report and on the context (`ctx.saved_path`)."""
    if result.new_path:
        entry.saved_path = str(result.new_path)
        _set_attr(ctx, "saved_path", entry.saved_path, force=True)


def _set_attr(ctx: Any, name: str, value: Any, force: bool = False) -> None:
    if ctx is None or (not force and hasattr(ctx, name)):
        return
    try:
        setattr(ctx, name, value)
    except (AttributeError, TypeError):
        pass  # a context that can't take it just doesn't get it


def _attach_run(ctx: Any, run: "RunContext | None") -> None:
    _set_attr(ctx, "saved_path", None)
    if run is not None:
        _set_attr(ctx, "batch", run.batch)
        _set_attr(ctx, "run_context", run)


def _run_after_save(
    steps: list[tuple[Step, dict[str, Any]]],
    threshold: float,
    ctx: Any,
    entry: FileReport,
    run: "RunContext | None",
    aborted: bool,
    skipped: bool,
    save_bad: bool,
) -> None:
    """The after_save phase. Steps that can't run get one note each; a
    SKIPPED result here is a note too (the file is already saved), and a
    failure only marks the file FAILED -- a required one stops the rest."""
    if aborted:
        why = "the file was aborted"
    elif save_bad:
        why = "the save didn't happen"
    elif skipped:
        why = "the file was skipped"
    else:
        why = ""
    changed = bool(entry.applied)
    for step, opts in steps:
        if run is not None and step.key in run.unavailable:
            continue
        label = step.label or step.key
        reason = why or ("" if changed or step.run_when_unchanged else "nothing was saved")
        if reason:
            entry.notes.append(f"{label}: not run ({reason})")
            continue
        failure = _run_step(step, opts, threshold, ctx, label, entry, after_save=True)
        if failure and step.required:
            break


def _fold_result(result: StepResult, label: str, entry: FileReport, skip_as_note: bool = False) -> bool:
    """Folds a non-suggestion result into `entry`; True if it failed."""
    if result.note:
        entry.notes.append(f"{label}: {result.note}")
    if result.status is StepStatus.APPLIED:
        entry.applied += [f"{label}: {c}" for c in result.changes] or [f"{label}: done"]
    elif result.status is StepStatus.FAILED:
        entry.failures.append(f"{label}: {result.message or 'failed'}")
        return True
    elif result.status is StepStatus.SKIPPED:
        text = f"{label}: {result.message or 'skipped'}"
        if skip_as_note:
            entry.notes.append(text)
        else:
            entry.skips.append(text)
    return False


def _run_step(
    step: Step, opts: dict, threshold: float, ctx: Any, label: str, entry: FileReport, after_save: bool = False
) -> bool:
    """Runs one step, folding its outcome into `entry`. Returns True if
    it failed."""
    try:
        _set_options(ctx, opts)
        result = step.run(ctx)
        if not isinstance(result, StepResult):
            raise TypeError("the step returned no StepResult")
        if result.status is not StepStatus.SUGGESTION:
            failed = _fold_result(result, label, entry, skip_as_note=after_save)
            if after_save and not failed:
                _note_path(result, ctx, entry)
            return failed
        else:
            if result.note:
                entry.notes.append(f"{label}: {result.note}")
            if result.confidence >= threshold:
                done = _as_changes(step.apply_suggestion(ctx, result))
                pct = f"auto-applied at {result.confidence:.0%}"
                if not done:
                    done = [str(result.value)]
                entry.applied += [f"{label}: {c} ({pct})" for c in done]
            else:
                entry.review.append(
                    ReviewItem(step.key, label, result.value, result.confidence, result.reason)
                )
    except Exception as exc:
        entry.failures.append(f"{label}: {type(exc).__name__}: {exc}")
        return True
    return False


def run_recipe(
    items: Iterable[Any],
    recipe: Recipe,
    catalogue: Iterable[Step],
    make_context: Callable[[Any], Any],
    progress: Callable[[int, int, Any], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    describe: Callable[[Any], str] = str,
    finalize: FinalizeFn | None = None,
    finalize_label: str = "Final save",
    env: Any = None,
) -> RedactReport:
    """Run `recipe` over `items`, each isolated from the others.
    `progress(done, total, item)` is called before each file (item is
    None on the final call, done == total); `should_cancel()` is polled
    between files. Returns the RedactReport (cancelled runs report what
    was done and how many files were skipped). `env` (anything) is handed
    to every enabled step's prepare(items, env) once, before the first
    file."""
    items = list(items)
    resolved = recipe.resolve(catalogue)
    report = RedactReport(confidence_threshold=recipe.confidence_threshold)
    started = time.monotonic()
    run = begin_run(items, resolved, env)
    report.run_notes = run.notes
    for index, item in enumerate(items):
        if should_cancel is not None and should_cancel():
            report.cancelled = True
            report.not_processed = len(items) - index
            break
        if progress is not None:
            progress(index, len(items), item)
        report.entries.append(
            run_recipe_on_item(
                item, resolved, recipe.confidence_threshold, make_context, describe, finalize, finalize_label,
                run=run,
            )
        )
    else:
        if progress is not None:
            progress(len(items), len(items), None)
    report.duration = time.monotonic() - started
    return report


# --- safe in-place commit --------------------------------------------------


class CommitError(Exception):
    """The commit didn't happen; the original is intact at its path
    (or, if even the rollback failed -- message says so -- preserved at
    the backup path named in the message). `reason` is the verifier's
    explanation when verification was what failed (else "")."""

    def __init__(self, message: str = "", reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


class VerifyFailed(Exception):
    """Raise from a `verify` callback to fail the commit with a reason
    (it lands in CommitError.reason and the error text)."""

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class CommitResult:
    original: str
    backup: str | None = None  # set only when the backup was KEPT
    warning: str = ""  # why it was kept
    new_path: str | None = None  # set only when the new file went to a different path

    @property
    def backup_kept(self) -> bool:
        return self.backup is not None

    @property
    def final_path(self) -> str:
        """Where the new file now lives (new_path, else the original's path)."""
        return self.new_path or self.original


def _backup_path(original: str) -> str:
    """`<stem>.redact-orig<ext>` beside the original: the extension is
    preserved on purpose, so if the backup ever survives (crash, bin
    unavailable) it is still an openable file, and a Recycle Bin
    restore gives back something usable. Numbered if taken."""
    stem, ext = os.path.splitext(original)
    candidate = f"{stem}.redact-orig{ext}"
    n = 2
    while os.path.lexists(candidate):
        candidate = f"{stem}.redact-orig{n}{ext}"
        n += 1
    return candidate


def _rename_no_clobber(src: str, dst: str) -> None:
    """Rename src -> dst, raising FileExistsError instead of overwriting
    (os.rename already refuses on Windows; POSIX would replace silently,
    so use a hard link, which never does, and fall back to check+rename
    on filesystems without links)."""
    if sys.platform == "win32":
        os.rename(src, dst)
        return
    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except OSError:
        if os.path.lexists(dst):
            raise FileExistsError(dst)
        os.rename(src, dst)
        return
    os.unlink(src)


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _verify_reason(verify: Callable[[str], Any], temp: str) -> tuple[bool, str]:
    """Run a verify callback: bool, (bool, reason) or raising VerifyFailed."""
    try:
        outcome = verify(temp)
    except VerifyFailed as exc:
        return False, exc.reason
    if isinstance(outcome, tuple) and len(outcome) == 2 and isinstance(outcome[0], bool):
        return outcome[0], str(outcome[1] or "")
    return bool(outcome), ""


def commit_in_place(
    original_path: str,
    new_temp_path: str,
    trash: Callable[[str], None] = move_to_trash,
    verify: Callable[[str], Any] | None = None,
    *,
    new_path: str | None = None,
) -> CommitResult:
    """Replace `original_path` with the finished `new_temp_path`, never
    losing the original. Order (each arrow is crash-safe: at every
    instant the old content exists under SOME name, and the new content
    is complete before it takes the real name):

      1. temp must exist, be non-empty, and pass `verify(temp)` (if
         given: truthy, or a (bool, reason) tuple; raising VerifyFailed
         (reason) or any other exception counts as failure -- the reason
         ends up in the CommitError);
      2. rename original -> sibling `<stem>.redact-orig<ext>`;
      3. os.replace(temp, original_path);
      4. sanity-check the new file is there and non-empty;
      5. trash(sibling) -- the Recycle Bin.

    A failure in 2-4 rolls back (sibling renamed back) and raises
    CommitError. A failure in 5 (or a trash call that "succeeds" but
    leaves the sibling behind) KEEPS the sibling and returns a
    CommitResult naming it: the bin can be bypassed on network shares
    and very large files, so a failed or doubtful trash never becomes
    a delete. The only window where the original's own path is empty
    is between steps 2 and 3; a crash there leaves the backup sibling
    (and the intact temp) on disk for manual recovery. Keep the temp
    in the same directory/volume as the original so step 3 is an
    atomic rename.

    `new_path` (keyword): the new file belongs at a DIFFERENT path (an
    extension/name change: CBR -> CBZ, MKV -> MP4). Then the order is:
    refuse if `new_path` already exists (or its folder doesn't); verify;
    rename temp -> new_path WITHOUT overwriting anything; sanity-check
    it; only then set the original aside and trash it as above. If the
    rename fails the original was never touched and the temp is left
    where it was (the caller cleans it up); if the original can't be set
    aside the new file is moved back to the temp name and nothing has
    changed. A failed trash keeps the `.redact-orig` sibling as before.
    The result's `new_path` is set (`final_path` is always the new
    file's location). A `new_path` equal to the original (compared
    case-insensitively on Windows) means the ordinary same-path commit."""
    original_path = os.fspath(original_path)
    temp = os.fspath(new_temp_path)
    target = os.fspath(new_path) if new_path is not None else None
    moving = target is not None and not _same_path(target, original_path)
    if not os.path.isfile(original_path):
        raise CommitError(f"the original file is missing: {original_path}")
    if moving:
        if os.path.lexists(target):
            raise CommitError(f"{target} already exists; nothing was changed")
        if not os.path.isdir(os.path.dirname(os.path.abspath(target))):
            raise CommitError(f"the folder for {target} doesn't exist; nothing was changed")
    try:
        if not os.path.isfile(temp) or os.path.getsize(temp) == 0:
            raise CommitError(f"the new file is missing or empty: {temp}")
        if verify is not None:
            ok, reason = _verify_reason(verify, temp)
            if not ok:
                detail = f": {reason}" if reason else ""
                raise CommitError(
                    f"the new file failed verification{detail}; original left untouched", reason
                )
    except CommitError:
        raise
    except Exception as exc:
        raise CommitError(f"couldn't verify the new file ({exc}); original left untouched", str(exc)) from exc

    if moving:
        return _commit_to_new_path(original_path, temp, target, trash)

    backup = _backup_path(original_path)
    try:
        os.rename(original_path, backup)
    except OSError as exc:
        raise CommitError(f"couldn't set the original aside ({exc}); nothing was changed") from exc

    try:
        os.replace(temp, original_path)
        if not os.path.isfile(original_path) or os.path.getsize(original_path) == 0:
            raise OSError("the replaced file is missing or empty")
    except Exception as exc:
        try:
            os.replace(backup, original_path)
        except OSError as rb_exc:
            raise CommitError(
                f"replace failed ({exc}) AND the rollback failed ({rb_exc}); "
                f"your original is safe at: {backup}"
            ) from exc
        raise CommitError(f"couldn't put the new file in place ({exc}); original restored") from exc

    return _trash_backup(original_path, backup, trash, None)


def _trash_backup(original_path: str, backup: str, trash: Callable[[str], None], new_path: str | None) -> CommitResult:
    try:
        trash(backup)
    except Exception as exc:
        return CommitResult(original_path, backup, f"the original is kept at {backup}: {exc}", new_path)
    if os.path.lexists(backup):
        return CommitResult(
            original_path,
            backup,
            f"the Recycle Bin didn't take it, so the original is kept at {backup}",
            new_path,
        )
    return CommitResult(original_path, new_path=new_path)


def _commit_to_new_path(original_path: str, temp: str, target: str, trash: Callable[[str], None]) -> CommitResult:
    """commit_in_place()'s different-path branch (see its docstring). The
    new file takes its name first; the original is only touched after."""
    try:
        _rename_no_clobber(temp, target)
        if not os.path.isfile(target) or os.path.getsize(target) == 0:
            raise OSError("the renamed file is missing or empty")
    except FileExistsError as exc:
        raise CommitError(f"{target} already exists; nothing was changed ({exc})") from exc
    except Exception as exc:
        # The original was never touched. If the temp already moved,
        # put it back so the caller's cleanup finds it.
        if not os.path.lexists(temp) and os.path.lexists(target):
            try:
                os.rename(target, temp)
            except OSError:
                raise CommitError(
                    f"couldn't put the new file in place ({exc}); original untouched, "
                    f"the new file is at: {target}"
                ) from exc
        raise CommitError(f"couldn't put the new file in place ({exc}); original untouched") from exc

    backup = _backup_path(original_path)
    try:
        os.rename(original_path, backup)
    except OSError as exc:
        try:
            os.rename(target, temp)
        except OSError as rb_exc:
            raise CommitError(
                f"couldn't set the original aside ({exc}) AND couldn't undo the new file ({rb_exc}); "
                f"your original is untouched, the new file is at: {target}"
            ) from exc
        raise CommitError(f"couldn't set the original aside ({exc}); nothing was changed") from exc
    return _trash_backup(original_path, backup, trash, target)

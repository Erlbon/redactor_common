"""Redact dry run (2026-11-05) and the undo depth: a dry run runs only the steps that declare they never touch
the disk, never calls finalize or an after_save step, and says so in the report; `redact -n` reaches it."""

import argparse
import io
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from redactor_common import cli  # noqa: E402
from redactor_common.cli import commands  # noqa: E402
from redactor_common.core.pipeline import FileStatus, Recipe, Step, StepResult, run_recipe  # noqa: E402
from redactor_common.core.undo import DEFAULT_MAX_ENTRIES, UndoManager  # noqa: E402


class Ctx:
    def __init__(self, item):
        self.item = item
        self.step_options = {}


class Tidy(Step):
    """Changes the in-memory context only."""

    key = "tidy"
    label = "Tidy"
    dry_run_safe = True

    def run(self, ctx):
        ctx.item["title"] = "Tidy"
        return StepResult.applied("title -> Tidy")


class Convert(Step):
    """Writes a file: the default, not dry-run safe."""

    key = "convert"
    label = "Convert"

    def __init__(self):
        self.calls = 0
        self.prepared = 0

    def prepare(self, items, env):
        self.prepared += 1

    def run(self, ctx):
        self.calls += 1
        return StepResult.applied("converted")


class Rename(Step):
    key = "rename"
    label = "Rename"
    position = "after_save"
    dry_run_safe = True  # even a safe-flagged after_save step can't run: nothing was saved

    def __init__(self):
        self.calls = 0

    def run(self, ctx):
        self.calls += 1
        return StepResult.applied("renamed")


def _run(dry_run):
    tidy, convert, rename = Tidy(), Convert(), Rename()
    catalogue = [tidy, convert, rename]
    saves = []
    report = run_recipe(
        [{"title": "x"}], Recipe.default_for(catalogue), catalogue, Ctx,
        finalize=lambda ctx, entry: saves.append(ctx.item) or StepResult.applied("saved"), dry_run=dry_run,
    )
    return report, convert, rename, saves


def test_a_dry_run_runs_only_safe_steps_and_never_saves():
    report, convert, rename, saves = _run(dry_run=True)
    entry = report.entries[0]
    assert convert.calls == 0 and convert.prepared == 0
    assert rename.calls == 0 and saves == []
    assert entry.applied == ["Tidy: title -> Tidy"]
    assert entry.status is FileStatus.CHANGED and entry.not_saved == []
    assert report.dry_run is True
    assert any("Convert" in note for note in report.run_notes)


def test_a_real_run_is_unchanged():
    report, convert, rename, saves = _run(dry_run=False)
    assert convert.calls == 1 and rename.calls == 1 and len(saves) == 1
    assert report.dry_run is False and "DRY RUN" not in report.to_text()


def test_the_dry_run_report_says_nothing_was_written():
    report, *_ = _run(dry_run=True)
    text = report.to_text()
    assert "DRY RUN" in text and "nothing was written" in text


def test_redact_options_have_dry_run_and_redact_items_passes_it_on():
    parser = argparse.ArgumentParser()
    commands.add_redact_options(parser)
    assert parser.parse_args(["-n"]).dry_run is True
    assert parser.parse_args(["--dry-run"]).dry_run is True
    assert parser.parse_args([]).dry_run is False

    convert = Convert()
    out = cli.Output(json_mode=True, stdout=(buffer := io.StringIO()), stderr=io.StringIO(), quiet=True)
    saves = []
    code = commands.redact_items(
        [{"title": "x"}], Recipe.default_for([convert]), [convert], make_context=Ctx, describe=str,
        finalize=lambda c, r: saves.append(c) or StepResult.applied("saved"), finalize_label="Save",
        path_of=lambda i: "", out=out, dry_run=True,
    )
    document = json.loads(buffer.getvalue())
    assert code == 0 and convert.calls == 0 and saves == [] and document["dry_run"] is True


def test_undo_keeps_fifty_steps_by_default():
    assert DEFAULT_MAX_ENTRIES == 50
    manager = UndoManager()
    item = SimpleNamespace(value=0)
    for n in range(60):
        manager.push(f"edit {n}", [item], lambda i: i.value)
        item.value = n + 1
    undone = 0
    while manager.can_undo():
        manager.undo(lambda i, snap: setattr(i, "value", snap))
        undone += 1
    assert undone == 50

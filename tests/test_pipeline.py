"""Tests for core/pipeline.py (recipe, engine, report, commit_in_place) and
gui/redact_dialog.py (run_redact under offscreen Qt, results + editor)."""

import os

import pytest

from redactor_common.core import pipeline
from redactor_common.core.pipeline import (
    CommitError,
    FileStatus,
    OptionSpec,
    Recipe,
    Step,
    StepResult,
    commit_in_place,
    run_recipe,
)


# --- fake steps ---------------------------------------------------------------


class Ctx:
    def __init__(self, item):
        self.item = item
        self.step_options = {}
        self.closed = False

    def close(self):
        self.closed = True


class Fix(Step):
    key = "fix"
    label = "Fix"
    options = (OptionSpec("level", "Level", "int", 2, 1, 5),)

    def run(self, ctx):
        return StepResult.applied(f"fixed level {ctx.step_options['level']}")


class Boom(Step):
    key = "boom"
    label = "Boom"

    def run(self, ctx):
        raise RuntimeError("kaput")


class Guess(Step):
    key = "guess"
    label = "Language"

    def __init__(self, conf):
        self.conf = conf
        self.applied = []

    def run(self, ctx):
        return StepResult.suggestion("nb", self.conf, "looks Norwegian")

    def apply_suggestion(self, ctx, result):
        self.applied.append((ctx.item, result.value))
        return f"language set to {result.value}"


class Nothing(Step):
    key = "noop"
    label = "Noop"

    def run(self, ctx):
        return StepResult.nothing()


class Required(Step):
    key = "req"
    label = "Must"
    required = True

    def run(self, ctx):
        return StepResult.failed("cannot open")


def _run(items, steps, **recipe_kw):
    recipe = Recipe.default_for(steps)
    for k, v in recipe_kw.items():
        setattr(recipe, k, v)
    return run_recipe(items, recipe, steps, Ctx)


# --- recipe -------------------------------------------------------------------


def test_recipe_round_trip():
    steps = [Fix(), Nothing()]
    r = Recipe.default_for(steps)
    r.order = ["noop", "fix"]
    r.enabled["noop"] = False
    r.options["fix"]["level"] = 4
    r.confidence_threshold = 0.75
    r2 = Recipe.from_json(r.to_json())
    assert r2 == r
    resolved = r2.resolve(steps)
    assert [s.key for s, _ in resolved] == ["fix"]
    assert resolved[0][1] == {"level": 4}


def test_recipe_unknown_and_missing_keys():
    steps = [Fix(), Nothing()]
    r = Recipe.from_json('{"order": ["gone", "noop"], "enabled": {"fix": false}, "zzz": 1,'
                         ' "options": {"fix": {"level": 99, "extra": 1}}}')
    resolved = r.resolve(steps)
    assert [s.key for s, _ in resolved] == ["noop"]  # "gone" skipped, fix disabled
    r.enabled["fix"] = True
    resolved = r.resolve(steps)
    assert [s.key for s, _ in resolved] == ["noop", "fix"]  # new step appended
    assert resolved[1][1] == {"level": 2}  # out-of-range option -> default
    assert r.confidence_threshold == 0.9


def test_recipe_garbage_json():
    for text in ("", "not json", "[]", "null", None):
        assert Recipe.from_json(text).confidence_threshold == 0.9


def test_duplicate_step_keys_rejected():
    with pytest.raises(ValueError):
        Recipe().resolve([Fix(), Fix()])


# --- engine -------------------------------------------------------------------


def test_step_exception_isolated_and_later_steps_run():
    report = _run(["a", "b"], [Boom(), Fix()])
    for e in report.entries:
        assert e.status is FileStatus.FAILED
        assert any("kaput" in f for f in e.failures)
        assert e.applied == ["Fix: fixed level 2"]  # Fix still ran
    assert len(report.entries) == 2


def test_required_failure_aborts_file_only():
    class Sometimes(Required):
        def run(self, ctx):
            if ctx.item == "bad":
                return StepResult.failed("cannot open")
            return StepResult.nothing()

    report = _run(["bad", "good"], [Sometimes(), Fix()])
    bad, good = report.entries
    assert bad.status is FileStatus.ABORTED and bad.applied == []  # Fix never ran
    assert good.status is FileStatus.CHANGED


def test_required_raising_aborts_too():
    class Raiser(Required):
        def run(self, ctx):
            raise OSError("disk")

    (e,) = _run(["x"], [Raiser(), Fix()]).entries
    assert e.status is FileStatus.ABORTED and e.applied == []


def test_make_context_failure_aborts_that_file():
    def bad(item):
        if item == "x":
            raise ValueError("unreadable")
        return Ctx(item)

    steps = [Fix()]
    report = run_recipe(["x", "y"], Recipe.default_for(steps), steps, bad)
    assert report.entries[0].status is FileStatus.ABORTED
    assert report.entries[1].status is FileStatus.CHANGED


def test_context_closed():
    made = []

    def mk(item):
        made.append(Ctx(item))
        return made[-1]

    steps = [Boom()]
    run_recipe(["a"], Recipe.default_for(steps), steps, mk)
    assert made[0].closed


def test_confidence_routing():
    hi, lo = Guess(0.95), Guess(0.5)
    (e,) = _run(["f"], [hi]).entries
    assert e.status is FileStatus.CHANGED and hi.applied == [("f", "nb")]
    assert "auto-applied at 95%" in e.applied[0]
    (e,) = _run(["f"], [lo]).entries
    assert e.status is FileStatus.NEEDS_REVIEW and lo.applied == []
    assert e.review[0].confidence == 0.5 and e.review[0].reason == "looks Norwegian"
    # exactly at the threshold applies
    at = Guess(0.9)
    _run(["f"], [at])
    assert at.applied
    # configurable threshold
    lo2 = Guess(0.5)
    _run(["f"], [lo2], confidence_threshold=0.4)
    assert lo2.applied


def test_suggestion_apply_failure_recorded():
    class NoApply(Step):
        key = "na"
        label = "NA"

        def run(self, ctx):
            return StepResult.suggestion("v", 1.0)

    (e,) = _run(["f"], [NoApply()]).entries
    assert e.status is FileStatus.FAILED


def test_unchanged_and_bad_return():
    class Bad(Step):
        key = "bad"

        def run(self, ctx):
            return None

    (e,) = _run(["f"], [Nothing()]).entries
    assert e.status is FileStatus.UNCHANGED
    (e,) = _run(["f"], [Bad()]).entries
    assert e.status is FileStatus.FAILED


def test_cancel_and_progress():
    calls = []
    cancel_after = {"n": 0}

    def cancel():
        cancel_after["n"] += 1
        return cancel_after["n"] > 2

    steps = [Fix()]
    report = run_recipe(list("abcd"), Recipe.default_for(steps), steps, Ctx,
                        progress=lambda d, t, i: calls.append((d, t, i)), should_cancel=cancel)
    assert report.cancelled and len(report.entries) == 2 and report.not_processed == 2
    assert calls == [(0, 4, "a"), (1, 4, "b")]
    assert "CANCELLED" in report.to_text()


def test_report_text():
    steps = [Fix(), Guess(0.5), Boom()]
    report = _run(["one.cbz", "two.cbz"], steps)
    text = report.to_text()
    assert "2 file(s)" in text and "2 failed" in text
    assert "one.cbz" in text and "Fix: fixed level 2" in text
    assert "NEEDS REVIEW" in text and "Language: nb  (50%)" in text and "looks Norwegian" in text
    assert "FAILURES" in text and "Boom: RuntimeError: kaput" in text
    assert text.index("CHANGES") < text.index("NEEDS REVIEW") < text.index("FAILURES")


# --- commit_in_place ----------------------------------------------------------


def _files(tmp_path, old=b"old", new=b"new"):
    orig = tmp_path / "book.cbz"
    tmp = tmp_path / "book.cbz.tmp"
    orig.write_bytes(old)
    tmp.write_bytes(new)
    return str(orig), str(tmp)


def test_commit_success(tmp_path):
    orig, tmp = _files(tmp_path)
    trashed = []

    def trash(p):
        assert os.path.exists(orig)  # new file already in place when trashing
        assert open(p, "rb").read() == b"old"
        trashed.append(p)
        os.remove(p)  # emulate the bin taking it

    res = commit_in_place(orig, tmp, trash=trash)
    assert open(orig, "rb").read() == b"new"
    assert not os.path.exists(tmp) and not res.backup_kept
    assert trashed and trashed[0].endswith("book.redact-orig.cbz")
    assert sorted(os.listdir(tmp_path)) == ["book.cbz"]


def test_commit_rejects_bad_temp(tmp_path):
    orig, tmp = _files(tmp_path, new=b"")
    with pytest.raises(CommitError):
        commit_in_place(orig, tmp, trash=lambda p: None)
    assert open(orig, "rb").read() == b"old"
    os.remove(tmp)
    with pytest.raises(CommitError):
        commit_in_place(orig, tmp, trash=lambda p: None)
    assert open(orig, "rb").read() == b"old"


def test_commit_verify_failure_and_exception(tmp_path):
    orig, tmp = _files(tmp_path)
    with pytest.raises(CommitError):
        commit_in_place(orig, tmp, trash=lambda p: None, verify=lambda p: False)
    assert open(orig, "rb").read() == b"old" and os.path.exists(tmp)

    def boom(p):
        raise RuntimeError("corrupt")

    with pytest.raises(CommitError, match="corrupt"):
        commit_in_place(orig, tmp, trash=lambda p: None, verify=boom)
    assert open(orig, "rb").read() == b"old"


def test_commit_rollback_when_replace_fails(tmp_path, monkeypatch):
    orig, tmp = _files(tmp_path)
    real_replace = os.replace
    seen = {}

    def flaky(src, dst):
        if src == tmp:
            seen["backup_exists"] = any(n.startswith("book.redact-orig") for n in os.listdir(tmp_path))
            raise PermissionError("locked")
        return real_replace(src, dst)

    monkeypatch.setattr(pipeline.os, "replace", flaky)
    trashed = []
    with pytest.raises(CommitError, match="original restored"):
        commit_in_place(orig, tmp, trash=trashed.append)
    assert seen["backup_exists"]  # old content always existed under some name
    assert open(orig, "rb").read() == b"old"
    assert open(tmp, "rb").read() == b"new"  # temp untouched, too
    assert not trashed
    assert sorted(os.listdir(tmp_path)) == ["book.cbz", "book.cbz.tmp"]


def test_commit_rollback_also_fails_points_at_backup(tmp_path, monkeypatch):
    orig, tmp = _files(tmp_path)

    def always(src, dst):
        raise PermissionError("nope")

    monkeypatch.setattr(pipeline.os, "replace", always)
    with pytest.raises(CommitError, match="original is safe at"):
        commit_in_place(orig, tmp, trash=lambda p: None)
    backup = str(tmp_path / "book.redact-orig.cbz")
    assert open(backup, "rb").read() == b"old"


def test_commit_first_rename_fails_changes_nothing(tmp_path, monkeypatch):
    orig, tmp = _files(tmp_path)

    def nope(src, dst):
        raise PermissionError("in use")

    monkeypatch.setattr(pipeline.os, "rename", nope)
    with pytest.raises(CommitError, match="nothing was changed"):
        commit_in_place(orig, tmp, trash=lambda p: None)
    assert open(orig, "rb").read() == b"old" and open(tmp, "rb").read() == b"new"


def test_commit_keeps_backup_when_trash_fails(tmp_path):
    orig, tmp = _files(tmp_path)

    def bad_trash(p):
        raise OSError("network share")

    res = commit_in_place(orig, tmp, trash=bad_trash)
    assert open(orig, "rb").read() == b"new"
    assert res.backup_kept and open(res.backup, "rb").read() == b"old"
    assert "network share" in res.warning


def test_commit_keeps_backup_when_trash_silently_leaves_it(tmp_path):
    orig, tmp = _files(tmp_path)
    res = commit_in_place(orig, tmp, trash=lambda p: None)
    assert res.backup_kept and os.path.exists(res.backup)


def test_commit_backup_name_is_unique(tmp_path):
    orig, tmp = _files(tmp_path)
    (tmp_path / "book.redact-orig.cbz").write_bytes(b"older leftover")
    res = commit_in_place(orig, tmp, trash=lambda p: None)
    assert res.backup.endswith("book.redact-orig2.cbz")
    assert (tmp_path / "book.redact-orig.cbz").read_bytes() == b"older leftover"


def test_commit_original_never_missing_at_rest(tmp_path, monkeypatch):
    """At each filesystem call, the old bytes must exist under some name
    (or already be replaced by the complete new file)."""
    orig, tmp = _files(tmp_path)

    def contents():
        return [(tmp_path / n).read_bytes() for n in os.listdir(tmp_path)]

    real_replace, real_rename = os.replace, os.rename

    def check(fn):
        def wrapped(src, dst):
            assert b"old" in contents()
            out = fn(src, dst)
            assert b"old" in contents() or b"new" in contents()
            return out
        return wrapped

    monkeypatch.setattr(pipeline.os, "replace", check(real_replace))
    monkeypatch.setattr(pipeline.os, "rename", check(real_rename))

    def trash(p):
        assert b"old" in contents()
        os.remove(p)

    commit_in_place(orig, tmp, trash=trash)
    assert open(orig, "rb").read() == b"new"


# --- GUI ------------------------------------------------------------------------


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_run_redact_and_results(qapp, tmp_path):
    from redactor_common.gui.redact_dialog import RedactResultsDialog, run_redact

    steps = [Fix(), Guess(0.4), Boom()]
    recipe = Recipe.default_for(steps)
    report = run_redact(None, ["a.cbz", "b.cbz"], recipe, steps, Ctx, show_results=False)
    assert len(report.entries) == 2 and not report.cancelled
    assert run_redact(None, [], recipe, steps, Ctx, show_results=False) is None

    dlg = RedactResultsDialog(report)
    assert "Needs review (2)" in dlg.tabs.tabText(1)
    assert dlg.review_tree.topLevelItemCount() == 2
    assert "a.cbz" in dlg.report_view.toPlainText()
    out = tmp_path / "r.txt"
    dlg.save_report_to(str(out))
    assert out.read_text(encoding="utf-8") == report.to_text()
    dlg.copy_report()
    from PyQt6.QtWidgets import QApplication

    assert QApplication.clipboard().text() == report.to_text()


def test_redact_menu_actions(qapp):
    from redactor_common.gui.redact_dialog import edit_recipe_menu_action, redact_menu_action
    from redactor_common.gui.standard_shortcuts import REDACT

    a = redact_menu_action(lambda: None)
    assert a.shortcut == REDACT == "Ctrl+Shift+E" and a.key == "redact"
    assert edit_recipe_menu_action(lambda: None).shortcut is None


def test_recipe_editor_smoke(qapp):
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QSpinBox

    from redactor_common.gui.redact_dialog import RecipeEditorDialog

    steps = [Fix(), Nothing(), Guess(0.5)]
    recipe = Recipe.default_for(steps)
    recipe.confidence_threshold = 0.8
    dlg = RecipeEditorDialog(steps, recipe)
    assert dlg.list.count() == 3 and dlg.threshold.value() == 0.8
    assert dlg.recipe().order == ["fix", "noop", "guess"]

    # edit the first step's int option through its generated widget
    spin = dlg.options_host.findChild(QSpinBox)
    assert spin is not None and spin.value() == 2
    spin.setValue(5)
    dlg.list.item(1).setCheckState(Qt.CheckState.Unchecked)
    dlg.list.setCurrentRow(0)
    dlg.move_current(1)
    dlg.threshold.setValue(0.95)
    out = dlg.recipe()
    assert out.order == ["noop", "fix", "guess"]
    assert out.enabled == {"noop": False, "fix": True, "guess": True}
    assert out.options["fix"] == {"level": 5}
    assert out.confidence_threshold == 0.95
    assert [s.key for s, _ in out.resolve(steps)] == ["fix", "guess"]

    dlg._reset()
    back = dlg.recipe()
    assert back.order == ["fix", "noop", "guess"] and back.enabled["noop"]
    assert back.options["fix"] == {"level": 2} and back.confidence_threshold == 0.9

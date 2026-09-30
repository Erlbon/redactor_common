"""Tests for the Redact engine additions: notes, skipped, finalize hook,
catalogue-position ordering, Step.position, str options, Step.options_for,
per-instance default_enabled, and the byte-for-byte old report format."""

import os

import pytest

from redactor_common.core.pipeline import (
    FileReport,
    FileStatus,
    OptionSpec,
    RedactReport,
    Recipe,
    ReviewItem,
    Step,
    StepResult,
    run_recipe,
    run_recipe_on_item,
)


class Ctx:
    def __init__(self, item):
        self.item = item
        self.step_options = {}
        self.saved = 0
        self.closed = False

    def close(self):
        self.closed = True


class S(Step):
    """Configurable fake step: `result` is a StepResult or a callable(ctx)."""

    def __init__(self, key, result=None, position="normal", **kw):
        super().__init__(**kw)
        self.key = key
        self.label = key.title()
        self._result = result
        self.position = position
        self.ran = []

    def run(self, ctx):
        self.ran.append(ctx.item)
        r = self._result
        return r(ctx) if callable(r) else (r or StepResult.nothing())


def _run(items, steps, **kw):
    return run_recipe(items, Recipe.default_for(steps), steps, Ctx, **kw)


# --- golden: old usage is byte-for-byte unchanged -----------------------------


def test_to_text_golden_old_usage():
    report = RedactReport(
        entries=[
            FileReport("a.cbz", applied=["Fix: fixed"], status=FileStatus.CHANGED),
            FileReport(
                "b.cbz",
                review=[ReviewItem("guess", "Guess", "en", 0.5, "looks english")],
                status=FileStatus.NEEDS_REVIEW,
            ),
            FileReport("c.cbz", failures=["Boom: bad"], status=FileStatus.ABORTED),
        ],
        duration=1.25,
    )
    expected = (
        "Redact report\n"
        "=============\n"
        "3 file(s), 1 changed, 1 needs review, 1 aborted\n"
        "Auto-apply guesses at confidence >= 90%\n"
        "Took 1.2 s\n"
        "\n"
        "CHANGES\n"
        "-------\n"
        "a.cbz\n"
        "  - Fix: fixed\n"
        "\n"
        "NEEDS REVIEW  (guesses below the threshold -- NOT applied)\n"
        "----------------------------------------------------------\n"
        "b.cbz\n"
        "  ? Guess: en  (50%)\n"
        "      because: looks english\n"
        "\n"
        "FAILURES\n"
        "--------\n"
        "c.cbz  [aborted -- original left untouched]\n"
        "  ! Boom: bad\n"
    )
    assert report.to_text() == expected


# --- notes and skipped ---------------------------------------------------------


def test_nothing_note_and_skipped_reporting():
    def skip_b(ctx):
        return StepResult.skipped("unsaved edits") if ctx.item == "b" else StepResult.applied("ok")

    a = S("a", StepResult.nothing(note="no cover found"))
    b = S("b", skip_b)
    c = S("c", StepResult.applied("late"))
    report = _run(["a", "b"], [a, b, c])
    ea, eb = report.entries
    assert ea.status is FileStatus.CHANGED and ea.notes == ["A: no cover found"]
    assert eb.status is FileStatus.SKIPPED and eb.skips == ["B: unsaved edits"]
    assert c.ran == ["a"]  # the skipped file's remaining steps don't run
    assert report.count(FileStatus.SKIPPED) == 1 and report.skipped() == [eb]
    text = report.to_text()
    assert "1 changed, 1 skipped" in text
    assert "\nNOTES\n-----\na\n  * A: no cover found\n" in text
    assert text.index("FAILURES") < text.index("NOTES") < text.index("SKIPPED")
    assert "SKIPPED  (deliberately not processed)\n" + "-" * 37 + "\nb\n  - B: unsaved edits\n" in text


def test_skip_is_not_failure_and_nothing_without_note_is_silent():
    report = _run(["x"], [S("s", StepResult.skipped("load error"))])
    assert report.entries[0].status is FileStatus.SKIPPED and not report.entries[0].failures
    plain = _run(["x"], [S("n")])
    assert "NOTES" not in plain.to_text() and "SKIPPED" not in plain.to_text()
    assert StepResult.nothing().note == ""


def test_failed_beats_skipped():
    report = _run(
        ["x"],
        [S("f", StepResult.failed("bad")), S("s", StepResult.skipped("eh"))],
    )
    assert report.entries[0].status is FileStatus.FAILED


# --- finalize ------------------------------------------------------------------


def test_finalize_runs_after_steps_and_adds_changes():
    def fin(ctx, entry):
        ctx.saved += 1
        assert entry.applied == ["A: did"]
        return StepResult.applied("saved")

    seen = []

    def mk(item):
        ctx = Ctx(item)
        seen.append(ctx)
        return ctx

    steps = [S("a", StepResult.applied("did"))]
    report = run_recipe(["x"], Recipe.default_for(steps), steps, mk, finalize=fin)
    assert report.entries[0].applied == ["A: did", "Final save: saved"]
    assert report.entries[0].status is FileStatus.CHANGED
    assert seen[0].saved == 1 and seen[0].closed


def test_finalize_failure_label_and_exception_and_none():
    steps = [S("a", StepResult.applied("did"))]
    recipe = Recipe.default_for(steps)
    e = run_recipe_on_item(
        "x", recipe.resolve(steps), 0.9, Ctx, finalize=lambda c, r: StepResult.failed("disk full"),
        finalize_label="Save"
    )
    assert e.status is FileStatus.FAILED and e.failures == ["Save: disk full"]
    # 2026-09-30#14: changes a failed save never wrote move to not_saved
    assert e.applied == [] and e.not_saved == ["A: did"]

    def boom(c, r):
        raise OSError("nope")

    e = run_recipe_on_item("x", recipe.resolve(steps), 0.9, Ctx, finalize=boom)
    assert e.status is FileStatus.FAILED and e.failures == ["Final save: OSError: nope"]

    e = run_recipe_on_item("x", recipe.resolve(steps), 0.9, Ctx, finalize=lambda c, r: None)
    assert e.status is FileStatus.CHANGED and not e.failures

    e = run_recipe_on_item("x", recipe.resolve(steps), 0.9, Ctx, finalize=lambda c, r: "junk")
    assert e.status is FileStatus.FAILED


def test_finalize_not_run_when_aborted_or_skipped_or_no_context():
    calls = []

    def fin(c, r):
        calls.append(1)

    req = S("r", StepResult.failed("x"))
    req.required = True
    for steps in ([req], [S("s", StepResult.skipped("no"))]):
        run_recipe(["x"], Recipe.default_for(steps), steps, Ctx, finalize=fin)

    def bad(item):
        raise RuntimeError("no ctx")

    steps = [S("a")]
    run_recipe(["x"], Recipe.default_for(steps), steps, bad, finalize=fin)
    assert calls == []
    # a non-required failure still lets finalize run
    steps = [S("f", StepResult.failed("x"))]
    run_recipe(["x"], Recipe.default_for(steps), steps, Ctx, finalize=fin)
    assert calls == [1]


def test_finalize_can_skip_and_note():
    steps = [S("a")]
    e = run_recipe_on_item(
        "x", Recipe.default_for(steps).resolve(steps), 0.9, Ctx,
        finalize=lambda c, r: StepResult.skipped("read-only"),
    )
    assert e.status is FileStatus.SKIPPED and e.skips == ["Final save: read-only"]


# --- ordering ------------------------------------------------------------------


def _keys(recipe, steps):
    return [s.key for s, _ in recipe.resolve(steps)]


def test_new_catalogue_steps_inserted_at_catalogue_position():
    steps = [S(k) for k in "abcde"]
    stored = Recipe(order=["a", "c", "e"])  # b and d were added later
    assert _keys(stored, steps) == ["a", "b", "c", "d", "e"]
    # no preceding known step: goes first
    assert _keys(Recipe(order=["c"]), steps) == ["a", "b", "c", "d", "e"]
    # user's custom order is respected; newcomers follow their predecessor
    assert _keys(Recipe(order=["e", "a"]), steps) == ["e", "a", "b", "c", "d"]
    # unknown stored keys are dropped, empty recipe = catalogue order
    assert _keys(Recipe(order=["zzz", "b"]), steps) == ["a", "b", "c", "d", "e"]
    assert _keys(Recipe(), steps) == ["a", "b", "c", "d", "e"]


def test_position_last_always_after_normal():
    steps = [S("a"), S("save", position="last"), S("b")]
    assert _keys(Recipe.default_for(steps), steps) == ["a", "b", "save"]
    assert Recipe.default_for(steps).order == ["a", "b", "save"]
    # even if a stored recipe puts it first
    assert _keys(Recipe(order=["save", "b", "a"]), steps) == ["b", "a", "save"]
    # a hand-built recipe that already pins keeps working
    assert _keys(Recipe(order=["a", "b", "save"]), steps) == ["a", "b", "save"]
    two = [S("x", position="last"), S("a"), S("y", position="last")]
    assert _keys(Recipe(), two) == ["a", "x", "y"]


# --- str option ----------------------------------------------------------------


def test_str_option_coerce():
    o = OptionSpec("prefix", "Prefix", "str", "", max_length=5)
    assert o.coerce("abc") == "abc" and o.coerce("") == ""
    assert o.coerce("toolong") == ""
    assert o.coerce(3) == "" and o.coerce(None) == "" and o.coerce(True) == ""
    assert OptionSpec("p", "P", "str", "dflt").coerce(["x"]) == "dflt"
    assert OptionSpec("p", "P", "str", "d").coerce("x" * 500) == "x" * 500

    step = S("s")
    step.options = (o,)
    recipe = Recipe(options={"s": {"prefix": 7}})
    assert recipe.resolve([step])[0][1] == {"prefix": ""}


# --- options_for / default_enabled ----------------------------------------------


def test_options_for_accessor():
    step = S("s")
    step.options = (OptionSpec("n", "N", "int", 3), OptionSpec("t", "T", "str", "hi"))
    assert step.options_for(object()) == {"n": 3, "t": "hi"}  # no ctx option access: defaults

    seen = []

    def grab(ctx):
        seen.append(step.options_for(ctx))

    step._result = grab
    recipe = Recipe(options={"s": {"n": 9, "t": "yo"}})
    run_recipe(["x"], recipe, [step], Ctx)
    assert seen == [{"n": 9, "t": "yo"}]

    class Ctx2:
        step_options = {"n": "bogus"}

    assert step.options_for(Ctx2()) == {"n": 3, "t": "hi"}


def test_default_enabled_per_instance():
    off = S("off", default_enabled=False)
    on = S("on")
    assert on.default_enabled is True and off.default_enabled is False
    assert S("z").default_enabled is True  # class default untouched
    recipe = Recipe.default_for([off, on])
    assert recipe.enabled == {"off": False, "on": True}
    assert _keys(Recipe(), [off, on]) == ["on"]
    on.default_enabled = False  # plain attribute assignment also works
    assert _keys(Recipe(), [off, on]) == []

    class Legacy(Step):  # old-style subclass with its own __init__, no super()
        key = "legacy"

        def __init__(self):
            self.x = 1

    assert Legacy().default_enabled is True


# --- GUI ------------------------------------------------------------------------


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_results_dialog_header_and_extra_notes(qapp):
    from redactor_common.gui.redact_dialog import RedactResultsDialog

    report = _run(["a"], [S("s", StepResult.skipped("unsaved"))])
    plain = RedactResultsDialog(report)
    assert plain.header_label is None and plain.notes_label is None
    dlg = RedactResultsDialog(report, None, "T", header="Done.", extra_notes=["one", "", "two"])
    assert dlg.header_label.text() == "Done." and dlg.notes_label.text() == "one\ntwo"
    assert dlg.windowTitle() == "T"
    assert "SKIPPED" in dlg.report_view.toPlainText()
    assert "Done." not in dlg.report_view.toPlainText()


def test_run_redact_finalize_passthrough(qapp):
    from redactor_common.gui.redact_dialog import run_redact

    steps = [S("a", StepResult.applied("did"))]
    report = run_redact(
        None, ["x"], Recipe.default_for(steps), steps, Ctx, show_results=False,
        finalize=lambda c, r: StepResult.applied("saved"), finalize_label="Save",
    )
    assert report.entries[0].applied == ["A: did", "Save: saved"]


def test_editor_str_widget_and_pinned_last(qapp):
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QLineEdit

    from redactor_common.gui.redact_dialog import RecipeEditorDialog

    a = S("a")
    a.options = (OptionSpec("prefix", "Prefix", "str", "p-", max_length=6),)
    save = S("save", position="last")
    b = S("b")
    steps = [a, save, b]
    dlg = RecipeEditorDialog(steps, Recipe(order=["save", "a", "b"]))
    keys = lambda: [dlg.list.item(r).data(Qt.ItemDataRole.UserRole) for r in range(dlg.list.count())]
    assert keys() == ["a", "b", "save"]

    edit = dlg.options_host.findChild(QLineEdit)
    assert edit.text() == "p-" and edit.maxLength() == 6
    edit.setText("zz")
    dlg.list.setCurrentRow(1)  # b
    dlg.move_current(1)  # can't go below the pinned step
    assert keys() == ["a", "b", "save"]
    dlg.list.setCurrentRow(2)  # save
    dlg.move_current(-1)  # can't go above normal ones
    assert keys() == ["a", "b", "save"]
    dlg.list.setCurrentRow(0)
    dlg.move_current(1)  # normal-to-normal still fine
    assert keys() == ["b", "a", "save"]

    # simulate a drag that dropped "save" at the top
    item = dlg.list.takeItem(2)
    dlg.list.insertItem(0, item)
    dlg._repin()
    assert keys() == ["b", "a", "save"]

    out = dlg.recipe()
    assert out.order == ["b", "a", "save"] and out.options["a"] == {"prefix": "zz"}
    dlg._reset()
    assert keys() == ["a", "b", "save"]

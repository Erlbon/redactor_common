"""Tests for the Redact engine pass 2: after_save / first positions, `after`
constraints, hidden steps, commit_in_place(new_path=...), verify reasons,
the prepare() batch hook, not_saved reports and the results header."""

import os

import pytest

from redactor_common.core import pipeline
from redactor_common.core.pipeline import (
    CommitError,
    FileReport,
    FileStatus,
    RedactReport,
    Recipe,
    Step,
    StepResult,
    VerifyFailed,
    commit_in_place,
    ordered_keys,
    run_recipe,
    run_recipe_on_item,
)


class Ctx:
    def __init__(self, item):
        self.item = item
        self.step_options = {}
        self.log = []


class S(Step):
    def __init__(self, key, result=None, position="normal", after=(), **kw):
        super().__init__(**kw)
        self.key = key
        self.label = key.title()
        self._result = result
        self.position = position
        self.after = after
        self.ran = 0

    def run(self, ctx):
        self.ran += 1
        ctx.log.append(self.key)
        r = self._result
        return r(ctx) if callable(r) else (r or StepResult.nothing())


def _go(steps, items=("x",), recipe=None, **kw):
    recipe = recipe or Recipe.default_for(steps)
    return run_recipe(items, recipe, steps, Ctx, **kw)


# --- 2. position "first" ------------------------------------------------------


def test_first_runs_before_normal_regardless_of_order():
    guard = S("guard", position="first")
    a = S("a")
    resolved_order = Recipe(order=["a", "guard"]).resolve([a, guard])
    assert [s.key for s, _ in resolved_order] == ["guard", "a"]


def test_first_skip_stops_the_file():
    guard = S("guard", StepResult.skipped("unsaved edits"), position="first")
    a = S("a", StepResult.applied("x"))
    fin = []
    report = _go([a, guard], finalize=lambda c, r: fin.append(1))
    assert report.entries[0].status is FileStatus.SKIPPED
    assert a.ran == 0 and not fin


def test_hand_built_resolved_still_puts_first_before_and_after_save_after():
    order = []
    a, g, z = S("a"), S("g", position="first"), S("z", StepResult.applied("d"), position="after_save")
    resolved = [(a, {}), (z, {}), (g, {})]
    a._result = StepResult.applied("x")
    entry = run_recipe_on_item("f", resolved, 0.9, Ctx, finalize=lambda c, r: StepResult.applied("saved"))
    assert entry.status is FileStatus.CHANGED and a.ran == g.ran == z.ran == 1


# --- 1. after_save --------------------------------------------------------------


def test_after_save_runs_after_finalize_and_learns_new_path():
    seen = {}

    def fin(ctx, rep):
        ctx.log.append("save")
        return StepResult.applied("saved", new_path="/lib/a.cbz")

    def rename(ctx):
        seen["path_in_rename"] = ctx.saved_path
        return StepResult.applied("renamed", new_path="/lib/b.cbz")

    a = S("a", StepResult.applied("x"))
    last = S("last", position="last")
    ren = S("ren", rename, position="after_save")
    logs = []
    ren_run = ren.run
    ren.run = lambda ctx: (logs.append(list(ctx.log)), ren_run(ctx))[1]
    report = _go([ren, last, a], finalize=fin, finalize_label="Save")
    e = report.entries[0]
    assert logs == [["a", "last", "save"]]  # the log as ren starts: save came before it
    assert seen["path_in_rename"] == "/lib/a.cbz"
    assert e.saved_path == "/lib/b.cbz"
    assert e.applied == ["A: x", "Save: saved", "Ren: renamed"]
    assert e.status is FileStatus.CHANGED


def test_after_save_not_run_when_unchanged_with_note():
    ren = S("ren", StepResult.applied("renamed"), position="after_save")
    e = _go([S("a"), ren], finalize=lambda c, r: None).entries[0]
    assert ren.ran == 0
    assert e.status is FileStatus.UNCHANGED
    assert e.notes == ["Ren: not run (nothing was saved)"]


def test_after_save_run_when_unchanged_opt_in():
    ren = S("ren", StepResult.applied("renamed"), position="after_save")
    ren.run_when_unchanged = True
    e = _go([S("a"), ren]).entries[0]
    assert ren.ran == 1 and e.status is FileStatus.CHANGED


def test_after_save_not_run_when_finalize_fails_or_skips_or_file_skipped_or_aborted():
    for fin, step_result, why in [
        (lambda c, r: StepResult.failed("disk full"), StepResult.applied("x"), "the save didn't happen"),
        (lambda c, r: StepResult.skipped("busy"), StepResult.applied("x"), "the save didn't happen"),
        (None, StepResult.skipped("unsaved"), "the file was skipped"),
    ]:
        ren = S("ren", position="after_save")
        e = _go([S("a", step_result), ren], finalize=fin).entries[0]
        assert ren.ran == 0, why
        assert e.notes == [f"Ren: not run ({why})"]
    req = S("a", StepResult.failed("bad"))
    req.required = True
    ren = S("ren", position="after_save")
    e = _go([req, ren]).entries[0]
    assert e.status is FileStatus.ABORTED and ren.ran == 0
    assert e.notes == ["Ren: not run (the file was aborted)"]


def test_after_save_failure_marks_failed_but_saved_data_stays():
    ren = S("ren", StepResult.failed("name taken"), position="after_save")
    e = _go([S("a", StepResult.applied("x")), ren], finalize=lambda c, r: StepResult.applied("saved")).entries[0]
    assert e.status is FileStatus.FAILED
    assert e.failures == ["Ren: name taken"]
    assert e.applied == ["A: x", "Final save: saved"]
    assert not e.not_saved


def test_after_save_exception_and_required_stops_the_rest_without_abort():
    def boom(ctx):
        raise OSError("locked")

    r1 = S("r1", boom, position="after_save")
    r1.required = True
    r2 = S("r2", StepResult.applied("y"), position="after_save")
    e = _go([S("a", StepResult.applied("x")), r1, r2]).entries[0]
    assert e.status is FileStatus.FAILED and r2.ran == 0
    assert e.failures == ["R1: OSError: locked"]


def test_after_save_skipped_result_is_only_a_note():
    ren = S("ren", StepResult.skipped("already fine"), position="after_save")
    e = _go([S("a", StepResult.applied("x")), ren]).entries[0]
    assert e.status is FileStatus.CHANGED and not e.skips
    assert e.notes == ["Ren: already fine"]


def test_position_last_unchanged_semantics():
    last = S("last", StepResult.applied("L"), position="last")
    a = S("a", StepResult.applied("A"))
    steps = [last, a]
    assert [s.key for s, _ in Recipe(order=["last", "a"]).resolve(steps)] == ["a", "last"]
    e = _go(steps, finalize=lambda c, r: StepResult.applied("saved")).entries[0]
    assert e.applied == ["A: A", "Last: L", "Final save: saved"]


# --- 1b. `after` constraints ------------------------------------------------------


def test_after_constraint_enforced_in_resolve():
    rename = S("rename", position="after_save")
    move = S("move", position="after_save", after=("rename",))
    for stored in (["move", "rename"], ["rename", "move"], []):
        keys = ordered_keys(stored, [rename, move])
        assert keys.index("rename") < keys.index("move")
    assert [s.key for s, _ in Recipe(order=["move", "rename"]).resolve([rename, move])] == ["rename", "move"]


def test_after_constraint_among_normal_steps_is_stable_and_minimal():
    a, b, c = S("a"), S("b", after=("c",)), S("c")
    d = S("d")
    assert ordered_keys(["a", "b", "c", "d"], [a, b, c, d]) == ["a", "c", "b", "d"]


def test_after_unknown_key_ignored_later_group_and_cycle_rejected():
    assert ordered_keys([], [S("a", after=("ghost",))]) == ["a"]
    with pytest.raises(ValueError):
        ordered_keys([], [S("a", after=("z",)), S("z", position="after_save")])
    with pytest.raises(ValueError):
        ordered_keys([], [S("a", after=("b",)), S("b", after=("a",))])


def test_old_steps_without_new_attributes_order_as_before():
    # No position/after overrides at all -> the old rules.
    a, b, c = S("a"), S("b"), S("c", position="last")
    assert ordered_keys(["c", "b", "a"], [a, b, c]) == ["b", "a", "c"]


# --- editor ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _editor_keys(dlg):
    return [dlg.list.item(r).data(0x100) for r in range(dlg.list.count())]


def test_editor_pins_groups_and_refuses_illegal_moves(qapp):
    from redactor_common.gui.redact_dialog import RecipeEditorDialog

    g = S("guard", position="first")
    a, b = S("a"), S("b")
    last = S("last", position="last")
    ren = S("rename", position="after_save")
    move = S("move", position="after_save", after=("rename",))
    steps = [ren, move, last, a, b, g]
    dlg = RecipeEditorDialog(steps, Recipe(order=["move", "rename", "b", "a", "last", "guard"]))
    assert _editor_keys(dlg) == ["guard", "b", "a", "last", "rename", "move"]
    # 'a' can't go above the first-group guard, nor below 'last'
    assert not dlg.can_move(1, -1)  # b above the first-group guard
    assert dlg.can_move(1, 1) and not dlg.can_move(2, 1)  # b<->a fine, a below last illegal
    assert not dlg.can_move(0, 1)  # guard below a normal step
    assert not dlg.can_move(5, -1)  # move above rename
    assert not dlg.can_move(4, 1)  # rename below move
    dlg.list.setCurrentRow(5)
    dlg.move_current(-1)
    assert _editor_keys(dlg)[-2:] == ["rename", "move"]
    # simulated illegal drag: put move on top, then let the repin settle
    item = dlg.list.takeItem(5)
    dlg.list.insertItem(0, item)
    dlg._repin()
    assert _editor_keys(dlg) == ["guard", "b", "a", "last", "rename", "move"]


def test_editor_hides_hidden_steps_and_recipe_omits_them(qapp):
    from redactor_common.gui.redact_dialog import RecipeEditorDialog

    internal = S("catalogue", position="first")
    internal.hidden = True
    a = S("a")
    dlg = RecipeEditorDialog([internal, a], Recipe())
    assert _editor_keys(dlg) == ["a"]
    r = dlg.recipe()
    assert r.order == ["a"] and "catalogue" not in r.enabled and "catalogue" not in r.options


# --- 8. hidden -----------------------------------------------------------------


def test_hidden_step_always_runs_and_is_not_stored():
    h = S("h", StepResult.applied("hid"), position="first")
    h.hidden = True
    a = S("a")
    default = Recipe.default_for([h, a])
    assert default.order == ["a"] and "h" not in default.enabled
    # even a recipe that disables it (hand-edited JSON) still runs it
    r = Recipe(order=["h", "a"], enabled={"h": False})
    assert [s.key for s, _ in r.resolve([h, a])] == ["h", "a"]
    e = run_recipe(["x"], r, [h, a], Ctx).entries[0]
    assert e.applied == ["H: hid"]
    assert "h" not in Recipe.from_json(default.to_json()).order


# --- 6. not_saved ---------------------------------------------------------------


def test_finalize_failure_moves_applied_to_not_saved_and_report_section():
    steps = [S("a", StepResult.applied("one", "two"))]
    e = _go(steps, finalize=lambda c, r: StepResult.failed("disk full"), finalize_label="Save").entries[0]
    assert e.status is FileStatus.FAILED and e.failures == ["Save: disk full"]
    assert e.applied == [] and e.not_saved == ["A: one", "A: two"]
    rep = RedactReport(entries=[e])
    text = rep.to_text()
    assert "NOT SAVED" in text and "  - A: one" in text
    assert text.index("FAILURES") < text.index("NOT SAVED")


def test_finalize_skip_also_moves_to_not_saved():
    steps = [S("a", StepResult.applied("one"))]
    e = _go(steps, finalize=lambda c, r: StepResult.skipped("busy")).entries[0]
    assert e.status is FileStatus.SKIPPED and e.not_saved == ["A: one"] and e.applied == []


def test_app_that_clears_applied_itself_is_not_duplicated():
    def fin(ctx, rep):
        rep.applied.clear()
        return StepResult.failed("couldn't save")

    e = _go([S("a", StepResult.applied("one"))], finalize=fin).entries[0]
    assert e.applied == [] and e.not_saved == []
    assert "NOT SAVED" not in RedactReport(entries=[e]).to_text()


def test_to_text_golden_without_not_saved_or_run_notes():
    rep = RedactReport(
        entries=[FileReport("a.cbz", status=FileStatus.CHANGED, applied=["T: x"]),
                 FileReport("b.cbz", status=FileStatus.FAILED, failures=["T: boom"])],
        duration=0.0,
    )
    assert rep.to_text() == (
        "Redact report\n=============\n2 file(s), 1 changed, 1 failed\n"
        "Auto-apply guesses at confidence >= 90%\nTook 0.0 s\n\n"
        "CHANGES\n-------\na.cbz\n  - T: x\n\n"
        "NEEDS REVIEW  (guesses below the threshold -- NOT applied)\n"
        "----------------------------------------------------------\n(none)\n\n"
        "FAILURES\n--------\nb.cbz\n  ! T: boom\n"
    )


# --- 5. prepare / batch ---------------------------------------------------------


def test_prepare_called_once_with_all_items_and_env_and_batch_available():
    calls = []

    class Counting(S):
        def prepare(self, items, env):
            calls.append((list(items), env))
            return {"n": len(items)}

    seen = []
    step = Counting("c", lambda ctx: (seen.append((ctx.batch["c"], ctx.run_context.env)), StepResult.nothing())[1])
    disabled = Counting("d", default_enabled=False)
    report = run_recipe(["a", "b", "c"], Recipe.default_for([step, disabled]), [step, disabled], Ctx, env="ENV")
    assert calls == [(["a", "b", "c"], "ENV")]
    assert seen == [({"n": 3}, "ENV")] * 3
    assert report.run_notes == []


def test_prepare_failure_marks_step_unavailable_and_run_continues():
    class Bad(S):
        def prepare(self, items, env):
            raise RuntimeError("no network")

    bad, ok = Bad("bad", StepResult.applied("x")), S("ok", StepResult.applied("y"))
    report = _go([bad, ok], items=("a", "b"))
    assert bad.ran == 0 and ok.ran == 2
    assert [e.status for e in report.entries] == [FileStatus.CHANGED] * 2
    assert report.run_notes == ["Bad: unavailable for this run (RuntimeError: no network)"]
    text = report.to_text()
    assert "RUN NOTES" in text and "unavailable for this run" in text
    assert report.entries[0].applied == ["Ok: y"]


def test_prepare_failure_on_after_save_step_skips_it_silently():
    class Bad(S):
        def prepare(self, items, env):
            raise RuntimeError("x")

    ren = Bad("ren", position="after_save")
    e = _go([S("a", StepResult.applied("x")), ren], finalize=lambda c, r: StepResult.applied("s")).entries[0]
    assert ren.ran == 0 and e.status is FileStatus.CHANGED


def test_run_recipe_on_item_without_run_context_still_works():
    a = S("a", StepResult.applied("x"))
    e = run_recipe_on_item("f", Recipe.default_for([a]).resolve([a]), 0.9, Ctx)
    assert e.status is FileStatus.CHANGED


# --- 3. commit to a new path ------------------------------------------------------


def _files(tmp_path, orig_name="book.cbr", temp_name="book.tmp"):
    orig = tmp_path / orig_name
    orig.write_bytes(b"old")
    tmp = tmp_path / temp_name
    tmp.write_bytes(b"new")
    return str(orig), str(tmp)


def _trash_remove(p):
    os.remove(p)


def test_commit_new_path_success(tmp_path):
    orig, tmp = _files(tmp_path)
    new = str(tmp_path / "book.cbz")
    res = commit_in_place(orig, tmp, trash=_trash_remove, new_path=new)
    assert res.new_path == new and res.final_path == new and not res.backup_kept
    assert open(new, "rb").read() == b"new"
    assert sorted(os.listdir(tmp_path)) == ["book.cbz"]  # original trashed, temp consumed


def test_commit_new_path_refuses_existing_file(tmp_path):
    orig, tmp = _files(tmp_path)
    (tmp_path / "book.cbz").write_bytes(b"someone else's")
    with pytest.raises(CommitError, match="already exists"):
        commit_in_place(orig, tmp, trash=_trash_remove, new_path=str(tmp_path / "book.cbz"))
    assert open(orig, "rb").read() == b"old" and open(tmp, "rb").read() == b"new"
    assert (tmp_path / "book.cbz").read_bytes() == b"someone else's"


def test_commit_new_path_missing_folder_refused(tmp_path):
    orig, tmp = _files(tmp_path)
    with pytest.raises(CommitError, match="doesn't exist"):
        commit_in_place(orig, tmp, new_path=str(tmp_path / "nope" / "book.cbz"))
    assert open(orig, "rb").read() == b"old"


def test_commit_new_path_rename_failure_keeps_original_and_temp(tmp_path, monkeypatch):
    orig, tmp = _files(tmp_path)

    def fail(src, dst):
        raise OSError("disk on fire")

    monkeypatch.setattr(pipeline, "_rename_no_clobber", fail)
    with pytest.raises(CommitError, match="original untouched"):
        commit_in_place(orig, tmp, trash=_trash_remove, new_path=str(tmp_path / "book.cbz"))
    assert open(orig, "rb").read() == b"old" and open(tmp, "rb").read() == b"new"
    assert not (tmp_path / "book.cbz").exists()


def test_commit_new_path_rolls_back_when_original_cant_be_set_aside(tmp_path, monkeypatch):
    orig, tmp = _files(tmp_path)
    real = os.rename

    def picky(src, dst):
        if src == orig:
            raise PermissionError("in use")
        return real(src, dst)

    monkeypatch.setattr(pipeline.os, "rename", picky)
    with pytest.raises(CommitError, match="set the original aside"):
        commit_in_place(orig, tmp, trash=_trash_remove, new_path=str(tmp_path / "book.cbz"))
    assert open(orig, "rb").read() == b"old"
    assert open(tmp, "rb").read() == b"new"  # new file moved back to the temp name
    assert not (tmp_path / "book.cbz").exists()


def test_commit_new_path_trash_failure_keeps_backup(tmp_path):
    orig, tmp = _files(tmp_path)

    def bad_trash(p):
        raise OSError("bin unavailable")

    new = str(tmp_path / "book.cbz")
    res = commit_in_place(orig, tmp, trash=bad_trash, new_path=new)
    assert res.backup_kept and res.new_path == new and "bin unavailable" in res.warning
    assert open(res.backup, "rb").read() == b"old" and res.backup.endswith("book.redact-orig.cbr")
    assert open(new, "rb").read() == b"new"


def test_commit_new_path_verify_failure_leaves_everything(tmp_path):
    orig, tmp = _files(tmp_path)
    with pytest.raises(CommitError):
        commit_in_place(orig, tmp, verify=lambda p: False, new_path=str(tmp_path / "book.cbz"))
    assert open(orig, "rb").read() == b"old" and open(tmp, "rb").read() == b"new"
    assert not (tmp_path / "book.cbz").exists()


def test_commit_same_path_unchanged_even_when_new_path_equals_original(tmp_path):
    orig, tmp = _files(tmp_path, "book.cbz")
    res = commit_in_place(orig, tmp, trash=_trash_remove, new_path=orig)
    assert res.new_path is None and res.final_path == orig
    assert open(orig, "rb").read() == b"new"
    orig, tmp = _files(tmp_path, "b2.cbz", "b2.tmp")
    res = commit_in_place(orig, tmp, trash=_trash_remove)
    assert res.new_path is None and open(orig, "rb").read() == b"new"


def test_rename_no_clobber_never_overwrites(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"1")
    b.write_bytes(b"2")
    with pytest.raises(FileExistsError):
        pipeline._rename_no_clobber(str(a), str(b))
    assert b.read_bytes() == b"2" and a.read_bytes() == b"1"


# --- 4. verify reasons ---------------------------------------------------------------


def test_verify_bool_old_message_unchanged(tmp_path):
    orig, tmp = _files(tmp_path, "book.cbz")
    with pytest.raises(CommitError) as ei:
        commit_in_place(orig, tmp, verify=lambda p: False)
    assert str(ei.value) == "the new file failed verification; original left untouched"
    assert ei.value.reason == ""


def test_verify_tuple_reason(tmp_path):
    orig, tmp = _files(tmp_path, "book.cbz")
    with pytest.raises(CommitError) as ei:
        commit_in_place(orig, tmp, verify=lambda p: (False, "3 pages missing"))
    assert ei.value.reason == "3 pages missing" and "3 pages missing" in str(ei.value)
    assert open(orig, "rb").read() == b"old"
    commit_in_place(orig, tmp, trash=_trash_remove, verify=lambda p: (True, "fine"))
    assert open(orig, "rb").read() == b"new"


def test_verify_failed_exception_reason(tmp_path):
    orig, tmp = _files(tmp_path, "book.cbz")

    def verify(p):
        raise VerifyFailed("duration differs by 12 s")

    with pytest.raises(CommitError) as ei:
        commit_in_place(orig, tmp, verify=verify)
    assert ei.value.reason == "duration differs by 12 s"
    assert "duration differs by 12 s" in str(ei.value) and open(orig, "rb").read() == b"old"


def test_verify_other_exception_old_message(tmp_path):
    orig, tmp = _files(tmp_path, "book.cbz")

    def verify(p):
        raise ValueError("kaboom")

    with pytest.raises(CommitError, match=r"couldn't verify the new file \(kaboom\)"):
        commit_in_place(orig, tmp, verify=verify)


def test_commit_error_still_constructible_with_a_message_only():
    assert str(CommitError("x")) == "x" and CommitError("x").reason == ""


# --- 7. results header -------------------------------------------------------------------


def test_run_redact_header_extra_notes_and_prepare_env(qapp, monkeypatch):
    from redactor_common.gui import redact_dialog
    from redactor_common.gui.redact_dialog import run_redact

    shown = []

    class FakeDialog:
        def __init__(self, report, parent, title="", header="", extra_notes=None):
            shown.append((title, header, extra_notes, report))

        def exec(self):
            return 0

    monkeypatch.setattr(redact_dialog, "RedactResultsDialog", FakeDialog)
    got = []

    class P(S):
        def prepare(self, items, env):
            got.append((list(items), env))

    step = P("a", StepResult.applied("x"))
    report = run_redact(
        None, ["f1", "f2"], Recipe.default_for([step]), [step], Ctx,
        header="3 skipped", extra_notes=["note"], env="E",
    )
    assert shown[0][1:3] == ("3 skipped", ["note"]) and shown[0][3] is report
    assert got == [(["f1", "f2"], "E")]


def test_run_redact_after_save_and_not_saved_flow(qapp):
    from redactor_common.gui.redact_dialog import run_redact

    ren = S("ren", StepResult.applied("renamed", new_path="/n"), position="after_save")
    a = S("a", StepResult.applied("x"))
    report = run_redact(
        None, ["f"], Recipe.default_for([a, ren]), [a, ren], Ctx,
        show_results=False, finalize=lambda c, r: StepResult.applied("saved"),
    )
    assert report.entries[0].saved_path == "/n"

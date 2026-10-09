"""redactor_common.cli.commands: rename/move/redact helpers on plain stand-in items."""
import argparse
import io
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from redactor_common import cli  # noqa: E402
from redactor_common.cli import commands  # noqa: E402
from redactor_common.core.pipeline import Recipe  # noqa: E402


def output():
    out, err = io.StringIO(), io.StringIO()
    return cli.Output(json_mode=True, stdout=out, stderr=err, quiet=True), out


def item(tmp_path, name, **fields):
    path = tmp_path / name
    path.write_bytes(b"x")
    return SimpleNamespace(path=str(path), fields=fields, bad="")


KW = dict(values_for=lambda i: i.fields, path_of=lambda i: i.path, skip_reason=lambda i: i.bad)


def rename(items, tmp_path, pattern="%t%", dry_run=False, **extra):
    out, _ = output()
    failed = commands.rename_items(items, pattern=pattern, out=out, dry_run=dry_run, ascii_only=False, **KW, **extra)
    return failed, out.records


def move(items, root, pattern, dry_run=False, copy=False, **extra):
    out, _ = output()
    failed = commands.move_items(
        items, root=str(root), pattern=pattern, out=out, dry_run=dry_run, copy=copy, ascii_only=False, **KW, **extra
    )
    return failed, out.records


# --- rename --------------------------------------------------------------------------------------------


def test_rename_numbers_collisions_and_skips_empty_names(tmp_path):
    a, b, empty = item(tmp_path, "a.mp3", t="Song"), item(tmp_path, "b.mp3", t="Song"), item(tmp_path, "c.mp3")
    failed, rows = rename([a, b, empty], tmp_path)
    assert failed == 0 and [r["status"] for r in rows] == ["renamed", "renamed", "skipped"]
    assert sorted(n for n in os.listdir(tmp_path) if n.endswith(".mp3")) == ["Song (2).mp3", "Song.mp3", "c.mp3"]


def test_rename_dry_run_unchanged_and_item_skip_reasons(tmp_path):
    same = item(tmp_path, "Song.mp3", t="Song")
    other = item(tmp_path, "o.mp3", t="Other")
    broken = item(tmp_path, "bad.mp3", t="X")
    broken.bad = "could not be read"
    _failed, rows = rename([same, other, broken], tmp_path, dry_run=True)
    assert [r["status"] for r in rows] == ["unchanged", "planned", "skipped"]
    assert rows[2]["message"] == "could not be read" and os.path.exists(other.path)


def test_a_case_only_rename_is_done(tmp_path):
    song = item(tmp_path, "song.mp3", t="SONG")
    failed, rows = rename([song], tmp_path)
    assert failed == 0 and rows[0]["status"] == "renamed"
    assert [n for n in os.listdir(tmp_path) if n.endswith(".mp3")] == ["SONG.mp3"]
    song2 = item(tmp_path, "keep.mp3", t="keep")  # exactly the same name: nothing to do
    assert rename([song2], tmp_path)[1][0]["status"] == "unchanged"


def test_rename_reports_the_app_callback(tmp_path):
    a = item(tmp_path, "a.mp3", t="Same")
    seen = []
    rename([a], tmp_path, on_renamed=lambda it, new: seen.append(new))
    assert seen == [str(tmp_path / "Same.mp3")]


def test_rename_reports_a_failed_rename_and_keeps_going(tmp_path, monkeypatch):
    a, b = item(tmp_path, "a.mp3", t="Alpha"), item(tmp_path, "b.mp3", t="Beta")
    real = commands.rename_no_clobber

    def flaky(src, dst):
        if src.endswith("a.mp3"):
            raise PermissionError("locked by another program")
        real(src, dst)

    monkeypatch.setattr(commands, "rename_no_clobber", flaky)
    failed, rows = rename([a, b], tmp_path)
    assert failed == 1 and [r["status"] for r in rows] == ["failed", "renamed"]
    assert "locked" in rows[0]["message"] and rows[0]["new_path"] == "" and (tmp_path / "Beta.mp3").exists()


def _poster_companions(old, new):
    return [(os.path.splitext(old)[0] + "-poster.jpg", os.path.splitext(new)[0] + "-poster.jpg")]


def test_rename_carries_companion_files(tmp_path):
    a = item(tmp_path, "movie.mp3", t="New Name")
    poster = tmp_path / "movie-poster.jpg"
    poster.write_bytes(b"s")
    _failed, rows = rename([a], tmp_path, dry_run=True, companions=_poster_companions)
    assert rows[0]["status"] == "planned" and "+1 companion" in rows[0]["message"] and poster.exists()
    _failed, rows = rename([a], tmp_path, companions=_poster_companions)
    assert (tmp_path / "New Name.mp3").exists() and (tmp_path / "New Name-poster.jpg").exists() and not poster.exists()


def test_a_companion_never_replaces_an_existing_file(tmp_path):
    a = item(tmp_path, "movie.mp3", t="New")
    (tmp_path / "movie-poster.jpg").write_bytes(b"mine")
    (tmp_path / "New-poster.jpg").write_bytes(b"someone else's")
    _failed, rows = rename([a], tmp_path, companions=_poster_companions)
    assert (tmp_path / "New.mp3").exists() and (tmp_path / "New-poster.jpg").read_bytes() == b"someone else's"
    assert "not renamed" in rows[0]["message"] and (tmp_path / "movie-poster.jpg").exists()


# --- move ----------------------------------------------------------------------------------------------


def test_move_plans_creates_folders_and_skips_nameless(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    a, nameless = item(tmp_path, "a.mp3", artist="Band", t="Song"), item(tmp_path, "n.mp3")
    failed, rows = move([a, nameless], root, "%artist%/%t%")
    assert failed == 0 and [r["status"] for r in rows] == ["moved", "skipped"]
    assert (root / "Band" / "Song.mp3").exists() and not os.path.exists(a.path) and os.path.exists(nameless.path)


def test_move_copy_dry_run_and_the_library_folder_checks(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    a = item(tmp_path, "a.mp3", t="Song")
    assert move([a], root, "%t%", dry_run=True)[1][0]["status"] == "planned" and os.path.exists(a.path)
    assert move([a], root, "%t%", copy=True)[1][0]["status"] == "copied"
    assert os.path.exists(a.path) and (root / "Song.mp3").exists()
    with pytest.raises(cli.CliError, match="--root"):
        move([a], "", "%t%")
    with pytest.raises(cli.CliError, match="does not exist"):
        move([a], tmp_path / "no", "%t%")


def test_move_says_clearly_when_the_source_has_vanished(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    a = item(tmp_path, "a.mp3", t="Song")
    out, _ = output()
    # the file disappears between planning and moving
    real = commands.plan_moves

    def vanishing(*args, **kwargs):
        plans = real(*args, **kwargs)
        os.remove(a.path)
        return plans

    commands.plan_moves = vanishing
    try:
        failed = commands.move_items(
            [a], root=str(root), pattern="%t%", out=out, dry_run=False, copy=False, ascii_only=False, **KW
        )
    finally:
        commands.plan_moves = real
    assert failed == 1 and "no longer there" in out.records[0]["message"]


def test_move_carries_companion_files_into_the_same_folders(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    a = item(tmp_path, "m.mp3", artist="Band", t="Song")
    poster = tmp_path / "m-poster.jpg"
    poster.write_bytes(b"s")

    def companions(plan):
        base = os.path.splitext(plan.new_path)[0]
        return [SimpleNamespace(old_path=str(poster), new_path=base + "-poster.jpg")]

    _failed, rows = move([a], root, "%artist%/%t%", companions=companions)
    assert (root / "Band" / "Song.mp3").exists() and (root / "Band" / "Song-poster.jpg").exists() and not poster.exists()
    assert "+1 companion" in rows[0]["message"]


def test_move_uses_the_trash_function_it_is_given_across_volumes(tmp_path, monkeypatch):
    root = tmp_path / "lib"
    root.mkdir()
    a = item(tmp_path, "a.mp3", t="Song")
    seen = []
    monkeypatch.setattr(commands, "execute_move", lambda old, new, copy, trash: seen.append(trash) or SimpleNamespace(warning=""))
    mine = lambda path: None  # noqa: E731
    move([a], root, "%t%", trash=mine)
    assert seen == [mine]


def test_skip_reason_runs_once_per_item(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    a = item(tmp_path, "a.mp3", t="Song")
    calls = []
    out, _ = output()
    commands.move_items(
        [a], root=str(root), pattern="%t%", out=out, dry_run=True, copy=False, ascii_only=False,
        values_for=lambda i: i.fields, path_of=lambda i: i.path, skip_reason=lambda i: calls.append(1) or "",
    )
    assert calls == [1]


def test_trash_to_moves_and_numbers(tmp_path):
    folder = tmp_path / "bin"
    trash = commands.trash_to(str(folder))
    for _ in range(2):
        f = tmp_path / "x.mp3"
        f.write_bytes(b"1")
        trash(str(f))
    assert sorted(os.listdir(folder)) == ["x (2).mp3", "x.mp3"]


# --- redact options --------------------------------------------------------------------------------------


def test_build_recipe_applies_enable_disable_threshold_and_rejects_unknown_steps():
    steps = [SimpleNamespace(key="a", hidden=False, label="A"), SimpleNamespace(key="h", hidden=True, label="H")]

    def parse(**kw):
        base = dict(enable=[], disable=[], threshold=None)
        base.update(kw)
        return argparse.Namespace(**base)

    recipe = commands.build_recipe(parse(enable=["a"], threshold="90"), Recipe(), steps)
    assert recipe.enabled["a"] is True and recipe.confidence_threshold == 0.9
    assert commands.build_recipe(parse(disable=["a"], threshold=0.5), recipe, steps).enabled["a"] is False
    with pytest.raises(cli.CliError, match="unknown step"):
        commands.build_recipe(parse(enable=["h"]), Recipe(), steps)  # hidden steps are not the user's
    with pytest.raises(cli.CliError, match="between 0 and 1"):
        commands.build_recipe(parse(threshold="250"), Recipe(), steps)


@pytest.mark.parametrize("raw, expected", [
    ("0.9", 0.9), ("0", 0.0), ("1", 1.0), ("90", 0.9), ("5", 0.05), ("100", 1.0), ("90%", 0.9), ("1.5%", 0.015),
    ("0.5%", 0.005), (0.75, 0.75),
])
def test_parse_threshold_accepts_a_fraction_or_a_percentage(raw, expected):
    assert commands.parse_threshold(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw, message", [
    ("1.5", "ambiguous"), ("2", "ambiguous"), ("150", "between 0 and 1"), ("-0.1", "between 0 and 1"),
    ("abc", "must be a number"), ("101%", "between 0 and 1"),
])
def test_parse_threshold_refuses_the_ambiguous_and_the_impossible(raw, message):
    with pytest.raises(cli.CliError, match=message):
        commands.parse_threshold(raw)


def test_redact_items_calls_after_run_and_adds_its_notes():
    calls = []
    out, buffer = output()
    code = commands.redact_items(
        [], Recipe(), [], make_context=lambda i: None, describe=str, finalize=lambda c, r: None, finalize_label="Save",
        path_of=lambda i: "", out=out, after_run=lambda: calls.append("done") or ["Google Books: refused, not asked again"],
    )
    document = json.loads(buffer.getvalue())
    assert code == 0 and calls == ["done"] and document["run_notes"] == ["Google Books: refused, not asked again"]


def test_redact_items_reports_each_file_and_a_failure_is_exit_1(tmp_path):
    from redactor_common.core.pipeline import Step, StepResult

    class Boom(Step):
        key = "boom"
        label = "Boom"

        def run(self, ctx):
            return StepResult.failed("it broke")

    boom = Boom()
    out, buffer = output()
    a = item(tmp_path, "a.mp3")
    code = commands.redact_items(
        [a], Recipe.default_for([boom]), [boom], make_context=lambda i: SimpleNamespace(step_options={}), describe=lambda i: i.path,
        finalize=lambda c, r: None, finalize_label="Save", path_of=lambda i: i.path, out=out,
    )
    document = json.loads(buffer.getvalue())
    row = document["results"][0]
    assert code == 1 and document["failed"] == 1 and row["file"] == a.path and row["path"] == a.path
    assert row["status"] == "failed" and "it broke" in " ".join(row["failures"])


def test_finish_run_prints_the_summary_and_picks_the_exit_code():
    out, buffer = output()
    assert commands.finish_run(out, 0, files=3, dry_run=False) == 0
    document = json.loads(buffer.getvalue())
    assert document["files"] == 3 and document["failed"] == 0 and document["dry_run"] is False
    out, buffer = output()
    assert commands.finish_run(out, 2, files=3) == 1


def test_say_and_pattern_options():
    out = cli.Output(stdout=io.StringIO(), stderr=io.StringIO())
    commands.say(out, {"path": "a", "status": "renamed", "message": "m", "new_path": "b"})
    assert out.out.getvalue() == "renamed   a  ->  b  (m)\n"
    parser = argparse.ArgumentParser()
    commands.add_pattern_options(parser, pattern_required=True)
    args = parser.parse_args(["-p", "%a%", "--zero-pad", "3", "--ascii", "-n"])
    assert (args.pattern, args.zero_pad, args.ascii, args.dry_run) == ("%a%", 3, True, True)
    with pytest.raises(SystemExit):
        parser.parse_args([])


# --- second review: patterns, trash, recipe files, values ---------------------------------------------------


def test_move_skips_a_file_when_the_pattern_gives_nothing_at_all(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    a, b = item(tmp_path, "a.mp3", artist="", t=""), item(tmp_path, "b.mp3", artist="", t="")
    # "//" is the pattern a batch file hands over when cmd has expanded %series% inside the quotes
    failed, rows = move([a, b], root, "%artist%//%t%")
    assert failed == 0 and [r["status"] for r in rows] == ["skipped", "skipped"]
    assert not any(root.iterdir()) and os.path.exists(a.path) and os.path.exists(b.path)


def test_a_planned_move_knows_it_was_nameless(tmp_path):
    from redactor_common.core.move_plan import plan_moves

    root = tmp_path / "lib"
    root.mkdir()
    a, b = item(tmp_path, "a.mp3"), item(tmp_path, "b.mp3", t="Real")
    plans = plan_moves([a, b], str(root), "%t%", lambda i: i.fields, lambda i: i.path)
    assert [p.nameless for p in plans] == [True, False]
    plans = plan_moves([b], str(root), "%x%", lambda i: {"x": ""}, lambda i: i.path)
    assert plans[0].nameless  # the "produced nothing" case too


def test_a_real_title_called_untitled_is_not_mistaken_for_the_fallback(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    a = item(tmp_path, "a.mp3", t="Untitled")
    failed, rows = move([a], root, "%t%")
    assert rows[0]["status"] == "moved" and (root / "Untitled.mp3").exists()


def test_an_unknown_token_is_a_usage_error_for_rename_and_move(tmp_path):
    a = item(tmp_path, "a.mp3", t="Song")
    with pytest.raises(cli.CliError, match="unknown token %tittle%"):
        rename([a], tmp_path, pattern="%t% %tittle%")
    root = tmp_path / "lib"
    root.mkdir()
    with pytest.raises(cli.CliError, match="unknown token"):
        move([a], root, "%x%/%t%")
    assert os.path.exists(a.path)
    rename([a], tmp_path, pattern="[%t%]", dry_run=True)  # a token inside an optional group is still checked, and known


def test_a_rename_pattern_with_a_slash_is_refused(tmp_path):
    a = item(tmp_path, "a.mp3", t="Song")
    for bad in ("%t%/%t%", "%t%\\%t%"):
        with pytest.raises(cli.CliError, match="cannot contain"):
            rename([a], tmp_path, pattern=bad)


def test_trash_to_makes_its_folder_only_when_a_file_arrives(tmp_path):
    folder = tmp_path / "bin"
    trash = commands.trash_to(str(folder))
    assert not folder.exists()  # nothing yet: a dry run or --list-steps leaves no trace
    f = tmp_path / "x.mp3"
    f.write_bytes(b"1")
    trash(str(f))
    assert (folder / "x.mp3").exists()


def test_trash_to_failures_are_trash_errors(tmp_path):
    from redactor_common.core.trash import TrashError

    blocker = tmp_path / "bin"
    blocker.write_bytes(b"i am a file")  # the folder cannot be made
    f = tmp_path / "x.mp3"
    f.write_bytes(b"1")
    with pytest.raises(TrashError, match="couldn't move it"):
        commands.trash_to(str(blocker))(str(f))
    assert f.exists()


def test_trash_with_retries_tries_again_and_accepts_a_file_that_is_gone(tmp_path, monkeypatch):
    from redactor_common.core.trash import TrashError

    monkeypatch.setattr(commands.time, "sleep", lambda s: None)
    f = tmp_path / "x.mp3"
    f.write_bytes(b"1")
    calls = []

    def flaky(path):
        calls.append(path)
        if len(calls) < 3:
            raise TrashError("locked")
        os.remove(path)

    commands.trash_with_retries(flaky)(str(f))
    assert len(calls) == 3 and not f.exists()

    g = tmp_path / "y.mp3"
    g.write_bytes(b"1")

    def moved_then_error(path):
        os.remove(path)  # the shell moved it, then reported an error
        raise TrashError("odd")

    commands.trash_with_retries(moved_then_error)(str(g))  # does not raise: it is gone, which is the goal
    h = tmp_path / "z.mp3"
    h.write_bytes(b"1")
    with pytest.raises(TrashError):
        commands.trash_with_retries(lambda path: (_ for _ in ()).throw(TrashError("locked")), attempts=3)(str(h))


def test_read_recipe_file_refuses_what_is_not_a_recipe(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"order": ["a"], "enabled": {"a": True}, "options": {}}), encoding="utf-8")
    assert json.loads(commands.read_recipe_file(str(good)))["order"] == ["a"]
    for name, content, message in (
        ("bad.json", "{oops", "not valid JSON"),
        ("list.json", "[1, 2]", "not a recipe"),
        ("empty.json", "{}", "not a recipe"),
        ("other.json", json.dumps({"hello": "world"}), "not a recipe"),
    ):
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        with pytest.raises(cli.CliError, match=message):
            commands.read_recipe_file(str(path))
    with pytest.raises(cli.CliError, match="cannot read"):
        commands.read_recipe_file(str(tmp_path / "missing.json"))


def test_values_refuse_what_no_file_format_can_store():
    from redactor_common.cli import values

    assert values.check_text("title", "  Dune  ") == "Dune"
    assert values.check_text("description", "line one\nline two\ttab", multiline=True) == "line one\nline two\ttab"
    for bad, multiline in (("bad\x01char", False), ("bad\x01char", True), ("a\ufffeb", True), ("two\nlines", False), ("tab\there", False)):
        with pytest.raises(cli.CliError, match="control character"):
            values.check_text("title", bad, multiline=multiline)
    assert values.is_ascii_number("007") and not values.is_ascii_number("\u00b2") and not values.is_ascii_number("\u0663")
    assert not values.is_ascii_number("") and not values.is_ascii_number("1.5")

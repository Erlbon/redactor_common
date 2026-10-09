"""redactor_common.cli.commands: rename/move/redact helpers on plain stand-in items."""
import argparse
import io
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from redactor_common import cli  # noqa: E402
from redactor_common.cli import commands  # noqa: E402
from redactor_common.core.pipeline import Recipe  # noqa: E402
from redactor_common.core.rename_log import RenameLog  # noqa: E402


def output():
    out, err = io.StringIO(), io.StringIO()
    return cli.Output(json_mode=True, stdout=out, stderr=err, quiet=True), out


def item(tmp_path, name, **fields):
    path = tmp_path / name
    path.write_bytes(b"x")
    return SimpleNamespace(path=str(path), fields=fields, bad="")


KW = dict(values_for=lambda i: i.fields, path_of=lambda i: i.path, skip_reason=lambda i: i.bad)


def test_rename_numbers_collisions_skips_empty_names_and_logs(tmp_path):
    a, b, empty = item(tmp_path, "a.mp3", t="Song"), item(tmp_path, "b.mp3", t="Song"), item(tmp_path, "c.mp3")
    log = RenameLog(str(tmp_path / "log.json"))
    out, _ = output()
    failed = commands.rename_items(
        [a, b, empty], pattern="%t%", out=out, dry_run=False, ascii_only=False, log=log, log_label="Rename", **KW
    )
    assert failed == 0
    assert [r["status"] for r in out.records] == ["renamed", "renamed", "skipped"]
    assert sorted(n for n in os.listdir(tmp_path) if n.endswith(".mp3")) == ["Song (2).mp3", "Song.mp3", "c.mp3"]
    assert len(log.last_batch().renames) == 2


def test_rename_dry_run_unchanged_and_item_skip_reasons(tmp_path):
    same = item(tmp_path, "Song.mp3", t="Song")
    other = item(tmp_path, "o.mp3", t="Other")
    broken = item(tmp_path, "bad.mp3", t="X")
    broken.bad = "could not be read"
    out, _ = output()
    commands.rename_items(
        [same, other, broken], pattern="%t%", out=out, dry_run=True, ascii_only=False,
        log=RenameLog(str(tmp_path / "l.json")), log_label="x", **KW,
    )
    assert [r["status"] for r in out.records] == ["unchanged", "planned", "skipped"]
    assert out.records[2]["message"] == "could not be read" and os.path.exists(other.path)


def test_rename_reports_the_app_callback_and_failures(tmp_path):
    a = item(tmp_path, "a.mp3", t="Same")
    seen = []
    out, _ = output()
    commands.rename_items(
        [a], pattern="%t%", out=out, dry_run=False, ascii_only=False, log=RenameLog(str(tmp_path / "l.json")),
        log_label="x", on_renamed=lambda it, new: seen.append(new), **KW,
    )
    assert seen == [str(tmp_path / "Same.mp3")]


def test_move_plans_creates_folders_and_skips_nameless(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    a, nameless = item(tmp_path, "a.mp3", artist="Band", t="Song"), item(tmp_path, "n.mp3")
    out, _ = output()
    log = RenameLog(str(tmp_path / "l.json"))
    failed = commands.move_items(
        [a, nameless], root=str(root), pattern="%artist%/%t%", out=out, dry_run=False, copy=False, ascii_only=False,
        log=log, log_label="Move", **KW,
    )
    assert failed == 0 and [r["status"] for r in out.records] == ["moved", "skipped"]
    assert (root / "Band" / "Song.mp3").exists() and not os.path.exists(a.path) and os.path.exists(nameless.path)
    assert log.last_batch() is not None


def test_move_copy_dry_run_and_the_library_folder_checks(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    a = item(tmp_path, "a.mp3", t="Song")
    out, _ = output()
    log = RenameLog(str(tmp_path / "l.json"))
    commands.move_items([a], root=str(root), pattern="%t%", out=out, dry_run=True, copy=False, ascii_only=False, log=log, log_label="m", **KW)
    assert out.records[0]["status"] == "planned" and os.path.exists(a.path)
    out2, _ = output()
    commands.move_items([a], root=str(root), pattern="%t%", out=out2, dry_run=False, copy=True, ascii_only=False, log=log, log_label="m", **KW)
    assert out2.records[0]["status"] == "copied" and os.path.exists(a.path) and (root / "Song.mp3").exists()
    assert log.last_batch() is None  # a copy leaves nothing to undo
    with pytest.raises(cli.CliError, match="--root"):
        commands.move_items([a], root="", pattern="%t%", out=out, dry_run=True, copy=False, ascii_only=False, log=log, log_label="m", **KW)
    with pytest.raises(cli.CliError, match="does not exist"):
        commands.move_items([a], root=str(tmp_path / "no"), pattern="%t%", out=out, dry_run=True, copy=False, ascii_only=False, log=log, log_label="m", **KW)


def test_trash_to_moves_and_numbers(tmp_path):
    folder = tmp_path / "bin"
    trash = commands.trash_to(str(folder))
    for _ in range(2):
        f = tmp_path / "x.mp3"
        f.write_bytes(b"1")
        trash(str(f))
    assert sorted(os.listdir(folder)) == ["x (2).mp3", "x.mp3"]


def test_build_recipe_applies_enable_disable_threshold_and_rejects_unknown_steps():
    steps = [SimpleNamespace(key="a", hidden=False, label="A"), SimpleNamespace(key="h", hidden=True, label="H")]

    def parse(**kw):
        base = dict(enable=[], disable=[], threshold=None)
        base.update(kw)
        return argparse.Namespace(**base)

    recipe = commands.build_recipe(parse(enable=["a"], threshold=90), Recipe(), steps)
    assert recipe.enabled["a"] is True and recipe.confidence_threshold == 0.9
    assert commands.build_recipe(parse(disable=["a"], threshold=0.5), recipe, steps).enabled["a"] is False
    with pytest.raises(cli.CliError, match="unknown step"):
        commands.build_recipe(parse(enable=["h"]), Recipe(), steps)  # hidden steps are not the user's
    with pytest.raises(cli.CliError, match="threshold"):
        commands.build_recipe(parse(threshold=250), Recipe(), steps)


def test_say_and_pattern_options():
    out, buffer = cli.Output(stdout=io.StringIO(), stderr=io.StringIO()), None
    commands.say(out, {"path": "a", "status": "renamed", "message": "m", "new_path": "b"})
    assert out.out.getvalue() == "renamed   a  ->  b  (m)\n"
    parser = argparse.ArgumentParser()
    commands.add_pattern_options(parser, pattern_required=True)
    args = parser.parse_args(["-p", "%a%", "--zero-pad", "3", "--ascii", "-n"])
    assert (args.pattern, args.zero_pad, args.ascii, args.dry_run) == ("%a%", 3, True, True)
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_redact_items_calls_after_run_and_adds_its_notes(tmp_path):
    calls = []
    out, buffer = output()
    code = commands.redact_items(
        [], Recipe(), [], make_context=lambda i: None, describe=str, finalize=lambda c, r: None, finalize_label="Save",
        path_of=lambda i: "", out=out, after_run=lambda: calls.append("done") or ["Google Books: refused, not asked again"],
    )
    import json

    document = json.loads(buffer.getvalue())
    assert code == 0 and calls == ["done"] and document["run_notes"] == ["Google Books: refused, not asked again"]


def _sidecar(tmp_path, name):
    f = tmp_path / name
    f.write_bytes(b"s")
    return f


def test_rename_carries_companion_files_and_logs_them(tmp_path):
    a = item(tmp_path, "movie.mp3", t="New Name")
    poster = _sidecar(tmp_path, "movie-poster.jpg")
    log = RenameLog(str(tmp_path / "l.json"))

    def companions(old, new):
        stem_old, stem_new = os.path.splitext(old)[0], os.path.splitext(new)[0]
        return [(stem_old + "-poster.jpg", stem_new + "-poster.jpg")]

    out, _ = output()
    commands.rename_items(
        [a], pattern="%t%", out=out, dry_run=True, ascii_only=False, log=log, log_label="r", companions=companions, **KW
    )
    assert out.records[0]["status"] == "planned" and "+1 companion" in out.records[0]["message"] and poster.exists()
    out2, _ = output()
    commands.rename_items(
        [a], pattern="%t%", out=out2, dry_run=False, ascii_only=False, log=log, log_label="r", companions=companions, **KW
    )
    assert (tmp_path / "New Name.mp3").exists() and (tmp_path / "New Name-poster.jpg").exists() and not poster.exists()
    assert len(log.last_batch().renames) == 2


def test_move_carries_companion_files_into_the_same_folders(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    a = item(tmp_path, "m.mp3", artist="Band", t="Song")
    poster = _sidecar(tmp_path, "m-poster.jpg")

    def companions(plan):
        base = os.path.splitext(plan.new_path)[0]
        return [SimpleNamespace(old_path=str(poster), new_path=base + "-poster.jpg")]

    out, _ = output()
    log = RenameLog(str(tmp_path / "l.json"))
    commands.move_items(
        [a], root=str(root), pattern="%artist%/%t%", out=out, dry_run=False, copy=False, ascii_only=False, log=log,
        log_label="m", companions=companions, **KW,
    )
    assert (root / "Band" / "Song.mp3").exists() and (root / "Band" / "Song-poster.jpg").exists() and not poster.exists()
    assert len(log.last_batch().renames) == 2 and "+1 companion" in out.records[0]["message"]

"""cli.commands: the on_moved(old, new) hook rename_items() and move_items() share (wave 1, 2026-10-14).
It fires after each REAL move only: not for copies, dry runs, skips, failures, a cross-volume move whose
original could not be binned, or companions; and before the item's row is recorded."""
import errno
import io
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from redactor_common import cli  # noqa: E402
from redactor_common.cli import commands  # noqa: E402
from redactor_common.core import move_plan  # noqa: E402
from redactor_common.core.move_plan import PlannedMove  # noqa: E402
from redactor_common.core.trash import TrashError  # noqa: E402


class RecordingOutput(cli.Output):
    """Notes, for each recorded row, how many on_moved calls had happened by then."""

    def __init__(self, moves):
        super().__init__(json_mode=True, stdout=io.StringIO(), stderr=io.StringIO(), quiet=True)
        self._moves = moves
        self.calls_at_record = []

    def record(self, row):
        self.calls_at_record.append(len(self._moves))
        super().record(row)


def item(tmp_path, name, **fields):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 10)
    return SimpleNamespace(path=str(path), fields=fields, bad="")


KW = dict(values_for=lambda i: i.fields, path_of=lambda i: i.path, skip_reason=lambda i: i.bad)


def move(items, root, pattern="%t%", dry_run=False, copy=False, **extra):
    moves = []
    out = RecordingOutput(moves)
    failed = commands.move_items(
        items, root=str(root), pattern=pattern, out=out, dry_run=dry_run, copy=copy, ascii_only=False,
        on_moved=lambda old, new: moves.append((old, new)), **KW, **extra,
    )
    return failed, out, moves


def rename(items, pattern="%t%", dry_run=False, **extra):
    moves = []
    out = RecordingOutput(moves)
    failed = commands.rename_items(
        items, pattern=pattern, out=out, dry_run=dry_run, ascii_only=False,
        on_moved=lambda old, new: moves.append((old, new)), **KW, **extra,
    )
    return failed, out, moves


def lib(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    return root


# --- move_items -------------------------------------------------------------------------------------------


def test_move_reports_each_real_move_before_its_row(tmp_path):
    root = lib(tmp_path)
    a, b = item(tmp_path, "a.mp3", t="One"), item(tmp_path, "b.mp3", t="Two")
    failed, out, moves = move([a, b], root)
    assert failed == 0
    assert moves == [(a.path, str(root / "One.mp3")), (b.path, str(root / "Two.mp3"))]
    assert out.calls_at_record == [1, 2]  # the hook ran before each row was recorded


def test_move_copy_and_dry_run_are_not_moves(tmp_path):
    root = lib(tmp_path)
    a = item(tmp_path, "a.mp3", t="One")
    _, _, moves = move([a], root, copy=True)
    assert moves == [] and os.path.exists(a.path) and os.path.exists(root / "One.mp3")
    b = item(tmp_path, "b.mp3", t="Two")
    _, out, moves = move([b], root, dry_run=True)
    assert moves == [] and out.records[0]["status"] == "planned"


def test_move_skips_failures_and_noops_are_not_moves(tmp_path):
    root = lib(tmp_path)
    skipped = item(tmp_path, "s.mp3", t="S")
    skipped.bad = "unreadable"
    nameless = item(tmp_path, "n.mp3")
    gone = item(tmp_path, "g.mp3", t="Gone")
    already = item(root, "Here.mp3", t="Here")
    os.remove(gone.path)
    failed, out, moves = move([skipped, nameless, gone, already], root)
    assert moves == []
    assert [r["status"] for r in out.records] == ["skipped", "skipped", "failed", "unchanged"]
    assert failed == 1


def _force_cross_volume(monkeypatch):
    from redactor_common.core import os_utils
    real = os_utils.rename_no_clobber

    def fake(src, dst):
        if not os.path.basename(src).endswith(".part"):
            raise OSError(errno.EXDEV, "cross-device link")
        real(src, dst)

    monkeypatch.setattr(move_plan, "rename_no_clobber", fake)


def test_cross_volume_move_counts_only_when_the_original_was_binned(tmp_path, monkeypatch):
    root = lib(tmp_path)
    _force_cross_volume(monkeypatch)
    binned = item(tmp_path, "a.mp3", t="One")
    _, out, moves = move([binned], root, trash=lambda p: os.remove(p))
    assert moves == [(binned.path, str(root / "One.mp3"))] and out.records[0]["status"] == "moved"

    kept = item(tmp_path, "b.mp3", t="Two")

    def refuse(path):
        raise TrashError("no bin here")

    _, out, moves = move([kept], root, trash=refuse)
    # Both files exist: the row still says moved (with a warning), but nothing follows the file yet.
    assert out.records[0]["status"] == "moved" and "kept" in out.records[0]["message"]
    assert moves == [] and os.path.exists(kept.path)


def test_companions_travel_but_are_not_reported(tmp_path):
    root = lib(tmp_path)
    a = item(tmp_path, "a.mkv", t="Film")
    poster = tmp_path / "a-poster.jpg"
    poster.write_bytes(b"p")

    def companions(plan):
        return [PlannedMove(item=None, old_path=str(poster),new_path=os.path.join(os.path.dirname(plan.new_path), "Film-poster.jpg"))]

    _, _, moves = move([a], root, companions=companions)
    assert moves == [(a.path, str(root / "Film.mkv"))]
    assert (root / "Film-poster.jpg").exists()


def test_move_without_the_hook_still_works(tmp_path):
    root = lib(tmp_path)
    a = item(tmp_path, "a.mp3", t="One")
    out = cli.Output(json_mode=True, stdout=io.StringIO(), stderr=io.StringIO(), quiet=True)
    assert commands.move_items([a], root=str(root), pattern="%t%", out=out, dry_run=False, copy=False,
                               ascii_only=False, **KW) == 0
    assert (root / "One.mp3").exists()


# --- rename_items: the same hook --------------------------------------------------------------------------


def test_rename_reports_old_and_new_path_with_on_renamed_first(tmp_path):
    a = item(tmp_path, "a.mp3", t="Song")
    order = []
    moves = []
    out = RecordingOutput(moves)
    commands.rename_items(
        [a], pattern="%t%", out=out, dry_run=False, ascii_only=False,
        on_renamed=lambda it, new: order.append(("renamed", it.path, new)),
        on_moved=lambda old, new: (order.append(("moved", old, new)), moves.append((old, new))),
        **KW,
    )
    new = str(tmp_path / "Song.mp3")
    assert order == [("renamed", a.path, new), ("moved", a.path, new)]
    assert out.calls_at_record == [1]


def test_rename_case_only_change_is_a_move(tmp_path):
    a = item(tmp_path, "song.mp3", t="Song")
    _, out, moves = rename([a])
    assert out.records[0]["status"] == "renamed"
    assert moves == [(a.path, str(tmp_path / "Song.mp3"))]


def test_rename_dry_run_skip_unchanged_and_failure_are_not_moves(tmp_path, monkeypatch):
    planned = item(tmp_path, "a.mp3", t="New")
    _, _, moves = rename([planned], dry_run=True)
    assert moves == []

    same = item(tmp_path, "Same.mp3", t="Same")
    skipped = item(tmp_path, "s.mp3", t="X")
    skipped.bad = "unreadable"
    nameless = item(tmp_path, "n.mp3")
    _, out, moves = rename([same, skipped, nameless])
    assert moves == [] and [r["status"] for r in out.records] == ["unchanged", "skipped", "skipped"]

    def refuse(src, dst):
        raise PermissionError("locked")

    monkeypatch.setattr(commands, "rename_no_clobber", refuse)
    failing = item(tmp_path, "f.mp3", t="Other")
    failed, out, moves = rename([failing])
    assert failed == 1 and moves == [] and out.records[0]["status"] == "failed"


def test_rename_companions_are_not_reported(tmp_path):
    a = item(tmp_path, "a.mkv", t="Film")
    sub = tmp_path / "a.en.srt"
    sub.write_bytes(b"s")
    _, _, moves = rename([a], companions=lambda old, new: [(str(sub), str(tmp_path / "Film.en.srt"))])
    assert moves == [(a.path, str(tmp_path / "Film.mkv"))]
    assert (tmp_path / "Film.en.srt").exists()

"""redactor_common.cli: exit codes, output modes, path expansion, run()."""
import io
import json
import os
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from redactor_common import cli  # noqa: E402


class Color(Enum):
    RED = "red"


@dataclass
class Row:
    name: str
    size: int


def make_output(**kw):
    out, err = io.StringIO(), io.StringIO()
    return cli.Output(stdout=out, stderr=err, **kw), out, err


def test_json_mode_prints_one_document_and_nothing_else_on_stdout():
    output, out, err = make_output(json_mode=True)
    output.line("human text is suppressed")
    output.record({"file": "a.cbz", "path": Path("x/a.cbz"), "color": Color.RED, "row": Row("r", 3)})
    output.warn("careful")
    output.finish({"files": 1})
    document = json.loads(out.getvalue())
    assert document["results"][0] == {
        "file": "a.cbz", "path": str(Path("x/a.cbz")), "color": "red", "row": {"name": "r", "size": 3},
    }
    assert document["files"] == 1 and document["warnings"] == ["careful"]
    assert "careful" in err.getvalue()


def test_text_mode_prints_lines_and_finish_prints_nothing():
    output, out, err = make_output()
    output.line("hello")
    output.finish({"files": 1})
    assert out.getvalue() == "hello\n"


def test_quiet_silences_progress_and_warnings_but_not_errors():
    output, out, err = make_output(quiet=True)
    output.progress(1, 3, "a")
    output.warn("w")
    output.error("e")
    assert err.getvalue() == "error: e\n"
    loud, _out, loud_err = make_output()
    loud.progress(2, 3, "b")
    loud.progress(1, 1, "single items need no progress line")
    assert loud_err.getvalue() == "[2/3] b\n"


def test_expand_paths_takes_files_folders_and_wildcards(tmp_path):
    (tmp_path / "sub").mkdir()
    for name in ("b10.cbz", "b2.cbz", "note.txt", "sub/c.CBZ", "sub/d.cbr"):
        (tmp_path / name).write_bytes(b"x")
    files, missing = cli.expand_paths([str(tmp_path)], [".cbz", "cbr"])
    assert [os.path.relpath(f, tmp_path) for f in files] == ["b2.cbz", "b10.cbz", os.path.join("sub", "c.CBZ"), os.path.join("sub", "d.cbr")]
    assert missing == []
    shallow, _ = cli.expand_paths([str(tmp_path)], [".cbz"], recursive=False)
    assert [os.path.basename(f) for f in shallow] == ["b2.cbz", "b10.cbz"]
    named, _ = cli.expand_paths([str(tmp_path / "note.txt")], [".cbz"])
    assert [os.path.basename(f) for f in named] == ["note.txt"]  # a file named on purpose is kept
    globbed, _ = cli.expand_paths([str(tmp_path / "b*.cbz")], [".cbz"])
    assert [os.path.basename(f) for f in globbed] == ["b2.cbz", "b10.cbz"]
    both, _ = cli.expand_paths([str(tmp_path / "b2.cbz"), str(tmp_path)], [".cbz"])
    assert len(both) == len(set(both))  # no duplicates


def test_expand_paths_reports_what_matched_nothing(tmp_path):
    files, missing = cli.expand_paths([str(tmp_path / "nope.cbz"), str(tmp_path / "*.xyz")], [".cbz"])
    assert files == [] and len(missing) == 2


def test_run_maps_endings_to_exit_codes(capsys):
    assert cli.run(lambda argv: None) == 0
    assert cli.run(lambda argv: 1) == 1

    def usage(argv):
        raise cli.CliError("bad thing")

    assert cli.run(usage) == cli.EXIT_USAGE
    assert "error: bad thing" in capsys.readouterr().err

    def partial(argv):
        raise cli.CliError("partly", cli.EXIT_PARTIAL)

    assert cli.run(partial) == 1

    def interrupted(argv):
        raise KeyboardInterrupt

    assert cli.run(interrupted) == cli.EXIT_INTERRUPTED


def test_a_closed_pipe_ends_quietly(monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())  # run() closes stdout, as `| head` expects

    def pipe(argv):
        raise BrokenPipeError

    assert cli.run(pipe) == 0


def test_add_common_options_defines_json_and_quiet():
    import argparse

    parser = argparse.ArgumentParser()
    cli.add_common_options(parser)
    args = parser.parse_args(["--json", "-q"])
    assert args.json and args.quiet


# --- one exe: --output, is_cli_invocation, internal errors -------------------------------------


def parse(argv):
    import argparse

    parser = argparse.ArgumentParser()
    cli.add_common_options(parser)
    return parser.parse_args(argv)


def test_output_writes_the_json_document_to_a_file_not_stdout(tmp_path, capsys):
    target = tmp_path / "result.json"
    out = cli.make_output(parse(["--json", "--output", str(target)]))
    out.line("ignored in json mode")
    out.record({"file": "a.cbz"})
    out.finish({"files": 1})
    out.close()
    assert capsys.readouterr().out == ""
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["results"] == [{"file": "a.cbz"}] and document["files"] == 1


def test_output_file_also_takes_the_text_lines(tmp_path):
    target = tmp_path / "result.txt"
    out = cli.make_output(parse(["-o", str(target)]))
    out.line("hello")
    out.close()
    assert target.read_text(encoding="utf-8") == "hello\n"


def test_output_to_an_unwritable_place_is_a_usage_error(tmp_path):
    with pytest.raises(cli.CliError, match="cannot write the output file"):
        cli.make_output(parse(["--output", str(tmp_path / "no-such-folder" / "x.json")]))


def test_without_output_the_streams_are_the_normal_ones():
    out = cli.make_output(parse([]))
    assert out.out is sys.stdout and not out._owns_out
    out.close()  # nothing to close


def test_a_command_name_means_cli_and_anything_else_means_the_window():
    commands = ["info", "set"]
    assert cli.is_cli_invocation(["app.exe", "info", "x.cbz"], commands)
    assert cli.is_cli_invocation(["app.exe", "--help"], commands) and cli.is_cli_invocation(["app.exe", "--version"], commands)
    assert not cli.is_cli_invocation(["app.exe"], commands)
    assert not cli.is_cli_invocation(["app.exe", "C:/comics/book.cbz"], commands)  # a path to open
    assert not cli.is_cli_invocation(["app.exe", "inform"], commands)


def test_an_unexpected_error_prints_a_traceback_and_exits_70(capsys):
    def boom(argv):
        raise ValueError("kaboom")

    assert cli.run(boom) == cli.EXIT_INTERNAL == 70
    assert "kaboom" in capsys.readouterr().err


def test_bind_standard_streams_leaves_working_streams_alone():
    before = (sys.stdout, sys.stderr)
    cli.bind_standard_streams()
    assert (sys.stdout, sys.stderr) == before


# --- review fixes: paths, JSON on failure, encodings ---------------------------------------------------------


def test_brackets_in_a_real_name_are_not_a_wildcard(tmp_path):
    (tmp_path / "Book [2019].cbz").write_bytes(b"x")
    (tmp_path / "Book 0.cbz").write_bytes(b"x")  # what the class [2019] would match
    files, missing = cli.expand_paths([str(tmp_path / "Book [2019].cbz")], [".cbz"])
    assert [os.path.basename(f) for f in files] == ["Book [2019].cbz"] and missing == []


def test_a_wildcard_under_a_folder_with_brackets_still_matches(tmp_path):
    folder = tmp_path / "Series [Complete]"
    folder.mkdir()
    (folder / "a.cbz").write_bytes(b"x")
    (folder / "b.cbz").write_bytes(b"x")
    files, missing = cli.expand_paths([str(folder / "*.cbz")], [".cbz"])
    assert [os.path.basename(f) for f in files] == ["a.cbz", "b.cbz"] and missing == []
    files, _ = cli.expand_paths([str(folder / "[ab].cbz")], [".cbz"])  # a real pattern in the last part
    assert len(files) == 2


def test_a_wildcard_in_a_folder_name_still_works(tmp_path):
    for name in ("x1", "x2"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "f.cbz").write_bytes(b"x")
    files, _ = cli.expand_paths([str(tmp_path / "x*" / "f.cbz")], [".cbz"])
    assert len(files) == 2


def test_wildcard_matches_are_filtered_by_extension_like_a_folder(tmp_path):
    for name in ("a.cbz", "note.txt", "cover.jpg"):
        (tmp_path / name).write_bytes(b"x")
    files, _ = cli.expand_paths([str(tmp_path / "*")], [".cbz"])
    assert [os.path.basename(f) for f in files] == ["a.cbz"]
    named, _ = cli.expand_paths([str(tmp_path / "note.txt")], [".cbz"])  # named on purpose: kept
    assert [os.path.basename(f) for f in named] == ["note.txt"]


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows thing")
def test_junctions_and_symlinks_are_not_followed_in_a_folder_walk(tmp_path):
    import subprocess

    real = tmp_path / "Marvel"
    real.mkdir()
    (real / "a.cbz").write_bytes(b"x")
    link = tmp_path / "Favourites"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)], capture_output=True)
    if made.returncode != 0:
        pytest.skip("could not create a junction here")
    loop = real / "again"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(loop), str(tmp_path)], capture_output=True)
    files, _ = cli.expand_paths([str(tmp_path)], [".cbz"])
    assert [os.path.relpath(f, tmp_path) for f in files] == [os.path.join("Marvel", "a.cbz")]  # once, no loop


def test_natural_order_survives_digit_lookalikes():
    assert cli.natural_key("1\u00b23.cbz")  # a superscript two is not a number
    assert sorted(["b10", "b2", "b\u00b2"], key=cli.natural_key)


def test_json_is_ascii_safe_whatever_the_console_code_page():
    out = io.StringIO()
    output = cli.Output(json_mode=True, stdout=out)
    output.record({"path": "C:/Music/\u01c6 \u4e2d\u6587 \U0001f3b5.mp3"})
    output.finish()
    text = out.getvalue()
    assert text.isascii() and json.loads(text)["results"][0]["path"] == "C:/Music/\u01c6 \u4e2d\u6587 \U0001f3b5.mp3"


def test_a_summary_cannot_overwrite_the_documents_own_keys():
    out = io.StringIO()
    output = cli.Output(json_mode=True, stdout=out)
    output.record({"a": 1})
    output.finish({"results": "oops", "warnings": "oops", "files": 1})
    document = json.loads(out.getvalue())
    assert document["results"] == [{"a": 1}] and document["warnings"] == [] and document["files"] == 1


def test_a_command_that_fails_still_leaves_a_valid_json_document(tmp_path):
    target = tmp_path / "result.json"
    output = cli.make_output(__import__("argparse").Namespace(json=True, quiet=True, output=str(target)))
    try:
        try:
            output.record({"path": "a.cbz", "status": "renamed"})
            output.error("nothing found for b.cbz")
            raise RuntimeError("disk on fire")
        finally:
            output.close()
    except RuntimeError:
        pass
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["results"] == [{"path": "a.cbz", "status": "renamed"}]
    assert document["error"] == "disk on fire" and document["errors"] == ["nothing found for b.cbz"]


def test_an_interrupted_command_is_marked_in_the_document(tmp_path):
    target = tmp_path / "r.json"
    output = cli.make_output(__import__("argparse").Namespace(json=True, quiet=True, output=str(target)))
    with pytest.raises(KeyboardInterrupt):
        try:
            raise KeyboardInterrupt
        finally:
            output.close()
    assert json.loads(target.read_text(encoding="utf-8"))["error"] == "interrupted"


def test_a_finished_command_writes_exactly_one_document(tmp_path):
    target = tmp_path / "r.json"
    output = cli.make_output(__import__("argparse").Namespace(json=True, quiet=True, output=str(target)))
    output.record({"x": 1})
    output.finish({"files": 1})
    output.close()
    document = json.loads(target.read_text(encoding="utf-8"))  # a second document would not parse
    assert "error" not in document and document["files"] == 1


def test_text_mode_close_writes_nothing_extra(tmp_path):
    target = tmp_path / "r.txt"
    output = cli.make_output(__import__("argparse").Namespace(json=False, quiet=True, output=str(target)))
    output.line("hello")
    output.close()
    assert target.read_text(encoding="utf-8") == "hello\n"


def test_a_closed_pipe_with_no_stdout_does_not_escape_run(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)

    def pipe(argv):
        raise BrokenPipeError

    assert cli.run(pipe) == 0

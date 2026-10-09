"""
redactor_common/cli

The shared skeleton of the Redactor apps' command-line interfaces, so every app behaves the same way
in a terminal or a script. No Qt, no app code: an app builds its own argparse parser and commands
and uses these pieces.

  - Exit codes: EXIT_OK (0), EXIT_PARTIAL (1: some files failed), EXIT_USAGE (2: bad arguments or
    a file that does not exist), EXIT_INTERRUPTED (130: Ctrl+C).
  - Output: human lines on stdout, progress and warnings on stderr (silenced by --quiet), or with
    --json ONE JSON document on stdout and nothing else there -- so a script can pipe it to a parser.
  - expand_paths(): files, folders (recursively, filtered by extension) and wildcards (cmd.exe does
    not expand them), de-duplicated and in natural order.
  - run(): wraps an app's main so Ctrl+C, a closed pipe, an unencodable character in a file name
    and a CliError all end in the right exit code instead of a traceback; an unexpected error prints
    its traceback and ends with EXIT_INTERNAL.
  - One exe for the app AND its command line. is_cli_invocation() tells the app's entry point whether
    the arguments are a command (then it runs the CLI and never starts the window), and
    bind_standard_streams() (called by run()) makes print() work in a windowed Windows exe: it uses the
    handles the caller redirected (> file, | pipe) or attaches to the parent terminal. A windowed exe
    cannot be waited for by an interactive shell, so for scripts there is --output FILE, a result file
    that is complete when the process exits (use `start /wait`, `Start-Process -Wait` or a scheduled
    task to wait, and read the exit code).

Typical use:

    def main(argv=None):
        parser = argparse.ArgumentParser(prog="cbzredactor")
        add_common_options(parser)
        ...
        args = parser.parse_args(argv)
        out = Output(json_mode=args.json, quiet=args.quiet)
        ...
        out.finish({"files": n})
        return EXIT_OK if not failed else EXIT_PARTIAL

    if __name__ == "__main__":
        sys.exit(run(main))
"""

from __future__ import annotations

import argparse
import dataclasses
import enum
import glob
import json
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence, TextIO

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2
EXIT_INTERNAL = 70  # an unexpected error (a bug); the traceback is on stderr
EXIT_INTERRUPTED = 130

_NATURAL = re.compile(r"(\d+)")


class CliError(Exception):
    """A problem to report to the user in one line (no traceback) and end with `code`."""

    def __init__(self, message: str, code: int = EXIT_USAGE):
        super().__init__(message)
        self.code = code


def add_common_options(parser: argparse.ArgumentParser) -> None:
    """--json and --quiet, on the main parser and on every subcommand that wants them."""
    parser.add_argument("--json", action="store_true", help="print one JSON document instead of text")
    parser.add_argument("-q", "--quiet", action="store_true", help="no progress or warnings on stderr")
    parser.add_argument(
        "-o", "--output", metavar="FILE", help="write the result (text, or the JSON document) to FILE instead of stdout"
    )


def json_default(value: Any) -> Any:
    """json.dumps(default=...) for what results hold: paths, enums, dataclasses, sets."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, enum.Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    raise TypeError(f"cannot serialize {type(value).__name__}")


class Output:
    """Where a command's results go. In JSON mode `record()` collects rows and `finish()` prints the
    one document; in text mode `line()` prints as it goes. Progress and warnings always use stderr."""

    def __init__(
        self, json_mode: bool = False, quiet: bool = False, stdout: TextIO | None = None, stderr: TextIO | None = None
    ):
        self.json_mode = json_mode
        self.quiet = quiet
        self._out = stdout
        self._err = stderr
        self._owns_out = False
        self._finished = False
        self.records: list[Any] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []

    @property
    def out(self) -> TextIO:
        return self._out or sys.stdout

    @property
    def err(self) -> TextIO:
        return self._err or sys.stderr

    def line(self, text: str = "") -> None:
        """A line of the human-readable result (nothing in JSON mode)."""
        if not self.json_mode:
            print(text, file=self.out)

    def record(self, row: Any) -> None:
        """A result row: collected for the JSON document; in text mode the caller prints its own line."""
        self.records.append(row)

    def info(self, text: str) -> None:
        """Progress-style news on stderr, unless --quiet."""
        if not self.quiet:
            print(text, file=self.err)

    def warn(self, text: str) -> None:
        self.warnings.append(text)
        if not self.quiet:
            print(f"warning: {text}", file=self.err)

    def error(self, text: str) -> None:
        self.errors.append(text)
        print(f"error: {text}", file=self.err)

    def progress(self, done: int, total: int, label: str) -> None:
        """"[3/10] name" on stderr, unless --quiet or there is only one item."""
        if total > 1:
            self.info(f"[{done}/{total}] {label}")

    def _document(self, summary: dict | None, key: str, extra: dict | None = None) -> str:
        document: dict[str, Any] = dict(summary or {})
        if extra:
            document.update(extra)
        # The document's own keys come last so a summary can never replace them. ASCII-only JSON: a script
        # reading a console or pipe with another code page must never see "?" where a path had a letter.
        document[key] = self.records
        document["warnings"] = self.warnings
        document["errors"] = self.errors
        return json.dumps(document, indent=2, ensure_ascii=True, default=json_default)

    def close(self) -> None:
        """Ends the output. In JSON mode a command that never reached finish() (it was interrupted, or
        failed) still leaves ONE valid document: what was collected so far plus an "error" entry, so a
        script reading --output FILE never finds an empty or half-written result. A result file opened by
        make_output() is flushed and closed; stdout is left open."""
        if self.json_mode and not self._finished:
            failure = sys.exc_info()[1]
            if failure is not None:
                message = str(failure) or type(failure).__name__
                extra = {"error": "interrupted" if isinstance(failure, KeyboardInterrupt) else message}
            else:
                extra = {"error": "the command ended without a result"}
            try:
                print(self._document(None, "results", extra), file=self.out)
            except (OSError, ValueError):
                pass
            self._finished = True
        if self._owns_out and self._out is not None:
            try:
                self._out.close()
            except OSError:
                pass
            self._owns_out = False

    def finish(self, summary: dict | None = None, key: str = "results") -> None:
        """In JSON mode: prints {**summary, "results": [...], "warnings": [...], "errors": [...]} once."""
        self._finished = True
        if self.json_mode:
            print(self._document(summary, key), file=self.out)


def make_output(args: argparse.Namespace) -> Output:
    """The Output for parsed common options (--json, --quiet, --output FILE). Close it when the command ends."""
    target = getattr(args, "output", None)
    handle = None
    if target:
        try:
            handle = open(target, "w", encoding="utf-8", newline="\n")
        except OSError as exc:
            raise CliError(f"cannot write the output file {target}: {exc}") from exc
    output = Output(json_mode=getattr(args, "json", False), quiet=getattr(args, "quiet", False), stdout=handle)
    output._owns_out = handle is not None
    return output


def is_cli_invocation(argv: Sequence[str], commands: Iterable[str]) -> bool:
    """Whether the program was started as a command line: its first argument is one of the app's command
    names (or --help / --version). A path or anything else means "start the window"."""
    return len(argv) > 1 and argv[1] in set(commands) | {"-h", "--help", "--version"}


def natural_key(text: str) -> list:
    return [int(part) if part.isdecimal() else part.lower() for part in _NATURAL.split(text)]


def _is_link(path: str) -> bool:
    """A symlink or (Windows) junction: a folder that is really another folder."""
    junction = getattr(os.path, "isjunction", None)
    return os.path.islink(path) or bool(junction and junction(path))


def _wildcard_matches(argument: str) -> list[str]:
    """What a wildcard argument matches. Only the last part of the path is a pattern, so a folder whose name
    contains "[" or "]" ("Series [Complete]") still works; if that finds nothing the whole argument is tried
    as a pattern (a wildcard in a folder name)."""
    head, tail = os.path.split(argument)
    matches = glob.glob(os.path.join(glob.escape(head), tail)) if head else glob.glob(tail)
    return matches or glob.glob(argument)


def expand_paths(
    paths: Iterable[str], extensions: Iterable[str], recursive: bool = True
) -> tuple[list[str], list[str]]:
    """(files, missing). Each argument may be a file (kept whatever its extension: the user named it), a
    folder (every file with one of `extensions` inside it, subfolders too when `recursive`; symlinked and
    junctioned folders inside it are not followed) or a wildcard pattern (its matches are filtered by
    `extensions` like a folder's files; matched folders are searched). An argument that names an existing
    file or folder is always taken literally, so "[" in a real name is not a wildcard. Absolute paths,
    de-duplicated, in natural order. `missing` lists arguments that matched nothing."""
    wanted = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions}
    files: dict[str, str] = {}
    missing: list[str] = []

    def add(path: str) -> None:
        absolute = os.path.abspath(path)
        files.setdefault(os.path.normcase(absolute), absolute)

    def wanted_extension(name: str) -> bool:
        return not wanted or os.path.splitext(name)[1].lower() in wanted

    for argument in paths:
        literal = os.path.lexists(argument)
        pattern = not literal and any(c in argument for c in "*?[")
        candidates = _wildcard_matches(argument) if pattern else [argument]
        found = False
        for candidate in candidates:
            if os.path.isfile(candidate):
                if not pattern or wanted_extension(candidate):  # a file named on purpose is kept
                    add(candidate)
                    found = True
            elif os.path.isdir(candidate):
                found = True
                if recursive:
                    for root, dirs, names in os.walk(candidate):
                        dirs[:] = sorted((d for d in dirs if not _is_link(os.path.join(root, d))), key=natural_key)
                        for name in names:
                            if wanted_extension(name):
                                add(os.path.join(root, name))
                else:
                    for name in os.listdir(candidate):
                        full = os.path.join(candidate, name)
                        if os.path.isfile(full) and wanted_extension(name):
                            add(full)
        if not found:
            missing.append(argument)
    ordered = sorted(files.values(), key=lambda p: natural_key(os.path.normcase(p)))
    return ordered, missing


def bind_standard_streams() -> None:
    """Gives a windowed Windows exe working stdout/stderr (it has no console, so sys.stdout is None):
    the handles the caller redirected if there are any, else the parent terminal's console. Does nothing
    elsewhere, or when the streams already work."""
    if sys.platform != "win32":
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.GetStdHandle.restype = ctypes.c_void_p
    attached = False
    for name, std_id in (("stdout", -11), ("stderr", -12)):
        if getattr(sys, name, None) is not None:
            continue
        stream = None
        handle = kernel32.GetStdHandle(std_id)
        if handle and kernel32.GetFileType(ctypes.c_void_p(handle)) != 0:  # redirected: a file or a pipe
            import msvcrt

            try:
                fd = msvcrt.open_osfhandle(handle, os.O_WRONLY)
                stream = os.fdopen(fd, "w", encoding="utf-8", errors="replace", buffering=1, closefd=False)
            except OSError:
                stream = None
        if stream is None:
            if not attached:
                attached = bool(kernel32.AttachConsole(ctypes.c_uint32(0xFFFFFFFF)))  # the parent's terminal
            if attached:
                code_page = f"cp{kernel32.GetConsoleOutputCP()}"
                try:
                    stream = open("CONOUT$", "w", encoding=code_page, errors="replace", buffering=1)
                except (OSError, LookupError):
                    stream = None
        if stream is not None:
            setattr(sys, name, stream)


def run(main: Callable[[Sequence[str] | None], int], argv: Sequence[str] | None = None) -> int:
    """Runs `main(argv)` and returns the exit code, turning the usual ways a command line ends badly
    into one-line messages: Ctrl+C (130), a closed pipe (like `| head`), a CliError, and file names the
    console cannot encode (printed with replacement characters rather than crashing)."""
    bind_standard_streams()
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (OSError, ValueError):
                pass
    try:
        return int(main(argv) or 0)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except BrokenPipeError:
        try:
            sys.stdout.close()  # what `| head` expects; stdout may be None (windowed exe) or already closed
        except (AttributeError, OSError, ValueError):
            pass
        return EXIT_OK
    except Exception:  # noqa: BLE001 -- a bug: say so with the traceback and a distinct exit code
        traceback.print_exc()
        return EXIT_INTERNAL

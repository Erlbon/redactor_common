"""
redactor_common/cli/main_factory.py

The top of every app's command line, written once: the four *cli/main.py
modules were the same 30 lines apart from the program name, its one-line
description and which subcommand parsers to add.

    build_parser, main = make_main(
        "mp3redactor", APP_VERSION,
        "The MP3 Redactor on the command line: ...",
        [cmd_tags.add_info_parser, cmd_tags.add_set_parser, cmd_files.add_rename_parser, ...],
        commands=COMMANDS,
    )

Each `adder(subparsers)` adds one subcommand and sets its `handler`
default to a callable `handler(args, out) -> exit code` (argparse's
set_defaults(handler=...)), exactly as the apps' cmd_*.add_*_parser
functions already do. main(argv) parses, makes the Output (--json/--quiet/
--output, see make_output) and always closes it. Wrap it in
redactor_common.cli.run() for the exit-code handling.

Pure logic, no Qt.
"""

from __future__ import annotations

import argparse
from typing import Callable, Iterable, Optional, Sequence

from redactor_common.cli import make_output

ParserAdder = Callable[[argparse._SubParsersAction], object]


def build_app_parser(
    prog: str,
    version: str,
    description: str,
    adders: Iterable[ParserAdder],
    commands: Optional[Sequence[str]] = None,
) -> argparse.ArgumentParser:
    """The app's argparse parser: --version ("prog version"), a required
    COMMAND, and one subcommand per adder, in order. When `commands` is
    given (the app's COMMANDS tuple, which also decides whether the exe
    starts the CLI or the window) it must list exactly the subcommands, in
    the same order -- a forgotten entry would make a command unreachable
    from the exe, so it is a ValueError here rather than a silent gap."""
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument("--version", action="version", version=f"{prog} {version}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True
    for adder in adders:
        adder(sub)
    if commands is not None and tuple(sub.choices) != tuple(commands):
        raise ValueError(
            f"{prog}: COMMANDS {tuple(commands)!r} must list the subcommands {tuple(sub.choices)!r}"
        )
    return parser


def make_main(
    prog: str,
    version: str,
    description: str,
    adders: Sequence[ParserAdder],
    commands: Optional[Sequence[str]] = None,
) -> tuple[Callable[[], argparse.ArgumentParser], Callable[[Optional[Sequence[str]]], int]]:
    """(build_parser, main) for an app's *cli/main.py. build_parser() makes
    a fresh parser each call (tests inspect it); main(argv) parses `argv`
    (None = sys.argv[1:]), runs the chosen subcommand's handler with an
    Output and returns its exit code."""
    adders = list(adders)

    def build_parser() -> argparse.ArgumentParser:
        return build_app_parser(prog, version, description, adders, commands)

    def main(argv: Optional[Sequence[str]] = None) -> int:
        args = build_parser().parse_args(argv)
        out = make_output(args)
        try:
            return args.handler(args, out)
        finally:
            out.close()

    return build_parser, main

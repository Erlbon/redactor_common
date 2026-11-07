"""
redactor_common/core/update_cli.py

The update framework's command line (docs/update-framework.md, section 5), answered by every app before it opens a
window, so an old copy can be asked, by the release gate or by hand, what it would do with a release:

    App.exe --version                                   prints APP_VERSION
    App.exe --check-update [--release-tag vX]           prints the Decision as JSON; downloads and changes nothing
    App.exe --update [--release-tag vX]                 does the update with no window at all
    ... --result-file PATH                              also writes the JSON to PATH (a windowed exe has no console)

`--update` on windows-installer downloads and checks the installer, starts it fully silent WITHOUT relaunch and
reports its process id ("started"; the caller waits for it); on windows-exe it swaps the exe ("installed").
Exit codes: 0 done (or decided), 2 GitHub could not be asked or the arguments are wrong, 3 this copy cannot install
the release (the JSON says why), 4 the download or install failed.

Qt-free: an app calls handle() first thing in main() and returns its code when it is not None.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Callable, TextIO

from redactor_common.core import lookup_client, selfupdate
from redactor_common.core.selfupdate import INSTALL, UpdateError
from redactor_common.core.update_manifest import WINDOWS_EXE, WINDOWS_INSTALLER

FLAGS = ("--version", "--check-update", "--update")


def _value(argv: list[str], flag: str) -> str | None:
    """The value after `flag`; "" when the flag is there without one; None when it is absent."""
    if flag not in argv:
        return None
    at = argv.index(flag)
    return argv[at + 1] if at + 1 < len(argv) and not argv[at + 1].startswith("--") else ""


def _start_detached(argv: list[str]) -> subprocess.Popen:
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(argv, creationflags=flags, close_fds=True, cwd=str(Path(argv[0]).parent))


def handle(argv: list[str], app_name: str, app_version: str, repo_url: str, *, exe: Path | None = None,
           frozen: bool | None = None, fetch: lookup_client.FetchFn | None = None, out: TextIO | None = None,
           opener=None, launch: Callable[[list[str]], object] | None = None) -> int | None:
    """Answers --version / --check-update / --update; None when `argv` holds none of them (the app starts as usual)."""
    if not any(flag in argv for flag in FLAGS):
        return None
    out = out if out is not None else sys.stdout
    result_file = _value(argv, "--result-file")

    def say(data: dict | str, code: int) -> int:
        text = data if isinstance(data, str) else json.dumps(data, indent=1)
        if result_file:
            path = Path(result_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text + "\n", encoding="utf-8")
        if out is not None:
            try:
                out.write(text + "\n")
                out.flush()
            except (OSError, ValueError):
                pass
        return code

    if "--version" in argv:
        return say(app_version, 0)
    tag = _value(argv, "--release-tag") or ""
    if _value(argv, "--release-tag") == "" or result_file == "":
        return say({"action": "error", "reason": "--release-tag and --result-file need a value"}, 2)
    exe = Path(exe) if exe else selfupdate.running_exe()
    frozen = selfupdate.is_frozen() if frozen is None else frozen
    channel = selfupdate.channel_of(exe, frozen)
    repo = selfupdate.repo_from_url(repo_url)
    try:
        decision = selfupdate.decide(repo, repo.split("/", 1)[1].lower(), app_version, channel, fetch, tag,
                                     writable=lambda: selfupdate.can_self_replace(exe))
    except UpdateError as exc:
        return say({"action": "error", "reason": str(exc), "channel": channel, "current": app_version,
                    "release": tag}, 2)
    data = decision.as_json()
    if "--check-update" in argv:
        return say(data, 0)
    if decision.action != INSTALL:
        return say(data, 3 if decision.action != selfupdate.NONE else 0)
    release = decision.release
    try:
        if channel == WINDOWS_INSTALLER:
            setup = selfupdate.download(release, selfupdate.installer_download_path(release), opener=opener)
            log = setup.with_name(setup.name + ".log")          # the release gate reads it (forced closes)
            command = selfupdate.installer_command(setup, visible=False, relaunch=False) + [f"/LOG={log}"]
            process = (launch or _start_detached)(command)
            return say(dict(data, action="started", pid=int(getattr(process, "pid", 0) or 0), installer=str(setup),
                            log=str(log)), 0)
        if channel == WINDOWS_EXE:
            new_file = exe.with_name(exe.name + selfupdate.NEW_SUFFIX)
            selfupdate.download(release, new_file, opener=opener)
            selfupdate.install(new_file, exe)
            return say(dict(data, action="installed"), 0)
    except (UpdateError, OSError) as exc:
        return say(dict(data, action="failed", reason=str(exc)), 4)
    return say(dict(data, action="failed", reason=f"no headless update for channel {channel!r}"), 4)

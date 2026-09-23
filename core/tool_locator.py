"""
redactor_common/core/tool_locator.py

Locates an external CLI tool a project shells out to (mp3val,
keyfinder-cli, ffmpeg, mkvpropedit, ...), in this priority order:

  1. An explicit override the user set (e.g. via a Settings > Locate
     External Tools dialog). If given and it doesn't actually exist,
     this is treated as NOT FOUND rather than silently falling through
     to auto-detect -- an explicit path the user pointed at is either
     right or wrong, and silently ignoring a broken one would make a
     "found"/"not found" indicator in the GUI meaningless.
  2. A copy bundled in `tools_dir` next to a frozen build (no separate
     install required by the user) -- e.g. tools/mp3val.exe. Skipped
     entirely if the caller doesn't have a bundled-tools concept
     (tools_dir=None).
  3. Whatever's on PATH, for dev-mode runs or users who already have
     the tool installed system-wide.
  4. Well-known install folders (`install_dirs`, see
     windows_program_dirs()), for installers that don't add themselves
     to PATH -- MKVToolNix, Calibre, Sigil (2026-09-23, from epub).

Returns None (never raises) when nothing is found -- callers are
expected to surface a clear "tool missing" status rather than crash,
since a missing sidecar tool is a deployment/config issue, not a bug
in a specific file.

Promoted from the mp3 project's original core/tool_locator.py, which
had this exact three-tier logic but only mp3 had it -- other projects
that shell out to external tools (the video project's ffmpeg/
MKVToolNix) only supported override-or-PATH, with no way to offer a
fully portable, no-install-needed distribution the way mp3 could.

`exe_name` should be the bare command name (e.g. "ffmpeg", not
"ffmpeg.exe") when the caller wants Windows .exe resolution handled
automatically in the bundled-dir check; passing a name that already
ends in ".exe" (mp3's existing convention, e.g. "mp3val.exe") works
identically -- only one candidate filename is tried in that case,
since the name is already fully qualified.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Callable


def find_tool(
    exe_name: str,
    tools_dir: str | Path | None = None,
    override: str | Path | None = None,
    install_dirs: list[str | Path] | tuple[str | Path, ...] = (),
    which: Callable[[str], str | None] | None = None,
) -> Path | None:
    """See the module docstring for tiers 1-3. `install_dirs` adds a
    fourth: well-known install folders checked after PATH, for tools
    whose installer doesn't add itself to PATH (Calibre, Sigil,
    MKVToolNix on Windows) -- promoted from epub's Calibre/Sigil lookup.
    Missing folders are skipped. `which` is injectable for tests; it
    defaults to shutil.which looked up at call time, so patching
    shutil.which still works too."""
    if override:
        overridden = Path(override)
        return overridden if overridden.exists() else None

    candidates = [exe_name]
    if not exe_name.lower().endswith(".exe"):
        candidates.append(exe_name + ".exe")

    if tools_dir is not None:
        found = _first_existing(Path(tools_dir), candidates)
        if found is not None:
            return found

    on_path = (which or shutil.which)(exe_name)
    if on_path:
        return Path(on_path)

    for install_dir in install_dirs:
        if not install_dir:
            continue
        found = _first_existing(Path(install_dir), candidates)
        if found is not None:
            return found

    return None


def _first_existing(directory: Path, candidates: list[str]) -> Path | None:
    for candidate in candidates:
        path = directory / candidate
        if path.exists():
            return path
    return None


def windows_program_dirs(*relative: str) -> list[Path]:
    """Candidate install folders under Program Files, Program Files
    (x86) and %LOCALAPPDATA%/Programs, e.g.
    windows_program_dirs("Calibre2"). Empty on other platforms, so a
    caller can pass the result unconditionally."""
    if sys.platform != "win32":
        return []
    roots = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ]
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if local_appdata:
        roots.append(os.path.join(local_appdata, "Programs"))
    dirs: list[Path] = []
    for root in roots:
        for rel in relative:
            path = Path(root) / rel
            if path not in dirs:
                dirs.append(path)
    return dirs

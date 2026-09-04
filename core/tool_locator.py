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

import shutil
from pathlib import Path


def find_tool(
    exe_name: str,
    tools_dir: str | Path | None = None,
    override: str | Path | None = None,
) -> Path | None:
    if override:
        overridden = Path(override)
        return overridden if overridden.exists() else None

    if tools_dir is not None:
        tools_dir = Path(tools_dir)
        candidates = [exe_name]
        if not exe_name.lower().endswith(".exe"):
            candidates.append(exe_name + ".exe")
        for candidate in candidates:
            bundled = tools_dir / candidate
            if bundled.exists():
                return bundled

    on_path = shutil.which(exe_name)
    if on_path:
        return Path(on_path)

    return None

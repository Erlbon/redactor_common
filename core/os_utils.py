"""
redactor_common/core/os_utils.py

Small shared OS-integration helper. Promoted from the epub project's
gui/os_utils.py -- no PyQt6 dependency despite living in a GUI-adjacent
role, so it belongs in core/ alongside the rest of the pure-logic
modules, not gui/.
"""

from __future__ import annotations

import os
import subprocess
import sys


def rename_no_clobber(src: str, dst: str) -> None:
    """os.rename() that never replaces an existing `dst`: raises
    FileExistsError instead. Windows' os.rename already behaves this way;
    POSIX's silently overwrites, so there a hard link (which fails
    atomically if `dst` exists) followed by removing `src` is used,
    falling back to a plain exists-check when the filesystem has no
    hard links."""
    if sys.platform == "win32":
        os.rename(src, dst)
        return
    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except OSError:
        if os.path.exists(dst):
            raise FileExistsError(dst) from None
        os.rename(src, dst)
        return
    os.unlink(src)


def reveal_in_file_manager(path: str) -> None:
    """Opens the system file manager showing (ideally selecting) path.
    Best-effort -- silently does nothing if the platform call fails,
    since this is always a convenience, never a core function in
    whatever's calling it."""
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except OSError:
        pass

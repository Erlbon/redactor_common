"""
redactor_common/core/trash.py

Removing a file the user may want back -- a converted or repaired
original: sent to the Recycle Bin (Trash on Linux/Mac) via send2trash,
never permanently deleted. Promoted from cbzredactor's core/trash.py
(2026-09-28) when videoredactor's repair needed the same thing.

send2trash is imported lazily and isn't a dependency of this package:
each app that uses this lists send2trash in its own requirements.txt,
and a missing install becomes a clear TrashError ("the original was
kept") instead of an ImportError at startup.
"""

from __future__ import annotations

import os


class TrashError(Exception):
    pass


def shell_path(path: str) -> str:
    """The plain form of a path the Windows shell accepts: no extended-length
    prefix and one kind of slash. send2trash fails a path like
    \\\\?\\D:/Download\\x.cbz with "[Errno 3] path not found" although the file
    exists. Harmless elsewhere (normpath only)."""
    path = str(path)
    ext = chr(92) * 2 + "?" + chr(92)
    if path.startswith(ext):
        rest = path[len(ext):]
        unc = "UNC" + chr(92)
        path = chr(92) * 2 + rest[len(unc):] if rest.startswith(unc) else rest
    return os.path.normpath(path)


def move_to_trash(path: str) -> None:
    path = shell_path(path)
    try:
        from send2trash import send2trash
    except ImportError as exc:
        raise TrashError(
            "the 'send2trash' package isn't installed (pip install send2trash), "
            "so the original was kept"
        ) from exc
    try:
        send2trash(str(path))
    except OSError as exc:
        raise TrashError(f"couldn't move it to the Recycle Bin: {exc}") from exc

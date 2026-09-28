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


class TrashError(Exception):
    pass


def move_to_trash(path: str) -> None:
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

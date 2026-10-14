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
import shutil
import sys
import time
from typing import Callable


class TrashError(Exception):
    pass


class TrashUnavailable(TrashError):
    """The Recycle Bin cannot be used for this file at all (a drive where it is not reliable, send2trash not
    installed): trying again would only fail the same way, so trash_with_retries() does not."""


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


# Shared settings key (QSettings, General group) for the folder an app
# moves an original into when the Recycle Bin is not reliable for its
# drive. Empty/unset = no fallback: move_to_trash() then refuses with a
# TrashError and the caller keeps the original where it is. Only
# videoredactor wires a UI for it (its Library safety "quarantine folder");
# the other apps show the TrashError message and keep the file.
RECYCLE_FALLBACK_FOLDER_KEY = "recycle_fallback_folder"

_DRIVE_FIXED = 3  # winapi DRIVE_FIXED


def _is_unc(path: str) -> bool:
    p = str(path).replace("/", chr(92))
    return p.startswith(chr(92) * 2)


def _windows_drive_type(path: str) -> int | None:
    """GetDriveTypeW for the path's root (Windows only; None elsewhere or
    when the call fails)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        root = os.path.splitdrive(os.path.abspath(path))[0]
        if not root:
            return None
        return int(ctypes.windll.kernel32.GetDriveTypeW(root + chr(92)))  # type: ignore[attr-defined]
    except Exception:
        return None


def trash_is_reliable(path: str, drive_type: Callable[[str], int | None] | None = None) -> bool:
    """Whether send2trash can be trusted to put `path` in a Recycle Bin the
    user can get it back from. False for UNC paths and, on Windows, for
    any drive that is not a fixed local disk (mapped network drives,
    removable media: the shell deletes permanently there with no
    warning). On other platforms a file under a remote mount point
    (/mnt, /media, /Volumes, /net, /run/user/.../gvfs) is treated as
    unreliable; everything else is trusted. `drive_type` can be injected
    for tests (it returns the winapi drive-type number, or None when
    unknown; unknown counts as unreliable on Windows)."""
    path = shell_path(path)
    if _is_unc(path):
        return False
    if sys.platform == "win32":
        kind = (drive_type or _windows_drive_type)(path)
        return kind == _DRIVE_FIXED
    if drive_type is not None:
        kind = drive_type(path)
        if kind is not None:
            return kind == _DRIVE_FIXED
    top = os.path.abspath(path).split("/")
    first = top[1] if len(top) > 1 else ""
    if first in ("mnt", "media", "Volumes", "net"):
        return False
    if "gvfs" in top or "smb-share:" in path:
        return False
    return True


def move_to_fallback_folder(path: str, folder: str) -> str:
    """Move `path` into `folder` under a dated subfolder that mirrors the
    file's drive-relative path (so D:/Video/a.mkv on 2026-10-13 becomes
    <folder>/2026-10-13/Video/a.mkv). Never overwrites: a name that is
    taken gets a " (2)", " (3)"... suffix. Returns the new path. Raises
    TrashError (file untouched) when the folder is not set or the move
    fails."""
    path = shell_path(path)
    folder = str(folder or "").strip()
    if not folder:
        raise TrashError("no fallback folder is set, so the original was kept")
    drive, rel = os.path.splitdrive(os.path.abspath(path))
    rel = rel.lstrip(chr(92) + "/")
    if _is_unc(path):
        # //server/share/dir/file -> share/dir/file
        parts = [p for p in rel.replace(chr(92), "/").split("/") if p]
        rel = "/".join(parts[1:]) if len(parts) > 1 else rel
    dated = os.path.join(folder, time.strftime("%Y-%m-%d"))
    target = os.path.normpath(os.path.join(dated, rel))
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        stem, ext = os.path.splitext(target)
        n = 2
        while os.path.lexists(target):
            target = f"{stem} ({n}){ext}"
            n += 1
        if os.path.lexists(target):
            raise FileExistsError(target)
        shutil.move(path, target)
    except OSError as exc:
        raise TrashError(f"couldn't move it to the fallback folder {folder}: {exc}") from exc
    return target


def move_to_trash(path: str, fallback_folder: str | None = None) -> None:
    """Send `path` to the Recycle Bin. On a drive where the bin is not
    reliable (see trash_is_reliable) the file is NOT touched and a
    TrashError is raised, unless `fallback_folder` is given: then it is
    moved there (move_to_fallback_folder) instead."""
    path = shell_path(path)
    if not trash_is_reliable(path):
        if fallback_folder:
            move_to_fallback_folder(path, fallback_folder)
            return
        raise TrashUnavailable(
            "the Recycle Bin is not available on this drive (network share or removable "
            "disk: the shell would delete permanently), so the original was kept"
        )
    try:
        from send2trash import send2trash
    except ImportError as exc:
        raise TrashUnavailable(
            "the 'send2trash' package isn't installed (pip install send2trash), "
            "so the original was kept"
        ) from exc
    try:
        send2trash(str(path))
    except OSError as exc:
        raise TrashError(f"couldn't move it to the Recycle Bin: {exc}") from exc


# --- retrying -----------------------------------------------------------------------------------------------
#
# On Windows an antivirus scan, the search indexer or the app's own background cover/thumbnail scan can hold a
# file open for an instant, which fails the move to the Recycle Bin. Promoted 2026-10-14 from
# cli/commands.trash_with_retries (still importable from there) and cbzredactor's _trash_retrying.
#
# How a caller uses it:
#   - instead of move_to_trash(path):      move_to_trash_retrying(path)
#   - where a trash callable is passed on:  execute_move(..., trash=trash_with_retries())
#                                           commit_in_place(..., trash=trash_with_retries())
#   - around another trash function:        trash_with_retries(trash_to(folder))

DEFAULT_TRASH_ATTEMPTS = 8
DEFAULT_TRASH_RETRY_DELAY = 0.25  # seconds; 8 attempts = under two seconds of waiting


def trash_with_retries(
    trash: Callable[[str], None] | None = None,
    attempts: int = DEFAULT_TRASH_ATTEMPTS,
    delay: float = DEFAULT_TRASH_RETRY_DELAY,
    sleep: Callable[[float], None] | None = None,
) -> Callable[[str], None]:
    """`trash` (default: move_to_trash, looked up when called, so a test's monkeypatch of this module's
    move_to_trash applies) wrapped to be tried again for a moment when it fails with a TrashError. The path is
    given to it in its plain shell form (shell_path). A file that is gone after a failed attempt was already
    taken (the shell can report an error after doing the move): that counts as done. TrashUnavailable (the
    bin can't be used here at all) is raised at once; any other lasting failure raises the last TrashError.
    `sleep` is injectable for tests (default: time.sleep, looked up when called)."""
    attempts = max(1, int(attempts))

    def wrapper(path: str) -> None:
        plain = shell_path(path)
        for attempt in range(attempts):
            try:
                (trash or move_to_trash)(plain)
                return
            except TrashUnavailable:
                raise
            except TrashError:
                if not os.path.lexists(plain):
                    return
                if attempt == attempts - 1:
                    raise
                (sleep or time.sleep)(delay)

    return wrapper


def move_to_trash_retrying(
    path: str,
    fallback_folder: str | None = None,
    attempts: int = DEFAULT_TRASH_ATTEMPTS,
    delay: float = DEFAULT_TRASH_RETRY_DELAY,
) -> None:
    """move_to_trash(path, fallback_folder) with trash_with_retries()'s retries: the drop-in for a direct
    move_to_trash() call on a file the app itself may still be reading."""
    trash_with_retries(lambda p: move_to_trash(p, fallback_folder), attempts=attempts, delay=delay)(path)

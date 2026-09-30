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
import time
from typing import Any, Callable


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
        if os.path.lexists(dst):
            raise FileExistsError(dst) from None
        os.rename(src, dst)
        return
    os.unlink(src)

# Windows error codes for "access denied", "sharing violation" and "lock
# violation": what an antivirus scan, the search indexer, a preview
# handler or a cloud-sync client raises while it briefly holds a file.
_LOCK_WINERRORS = (5, 32, 33)
LOCK_HINT = "the file may be locked by another program (antivirus, sync client, preview)"


def is_lock_error(exc: BaseException) -> bool:
    """True for the transient "someone else has the file open" errors:
    any PermissionError, or (Windows) an OSError whose winerror is 5, 32
    or 33. FileExistsError and FileNotFoundError are never lock errors."""
    if isinstance(exc, (FileExistsError, FileNotFoundError)):
        return False
    if isinstance(exc, PermissionError):
        return True
    return sys.platform == "win32" and isinstance(exc, OSError) and getattr(exc, "winerror", None) in _LOCK_WINERRORS


def retry_on_lock(
    func: Callable[..., Any], *args: Any,
    attempts: int = 6, delay: float = 0.15, sleep: Callable[[float], None] | None = None, **kwargs: Any,
) -> Any:
    """Call `func(*args, **kwargs)`, retrying when it raises a lock error
    (see is_lock_error) -- on Windows a just-written file is often held
    for a few hundred ms by a scanner or indexer. Waits delay, delay*1.4,
    delay*1.4^2 ... between attempts (about 1.6 s in total with the
    defaults), then re-raises the last error. Any other exception
    propagates at once. `sleep` is injectable for tests."""
    pause = sleep if sleep is not None else time.sleep
    for attempt in range(max(1, attempts)):
        try:
            return func(*args, **kwargs)
        except OSError as exc:
            if not is_lock_error(exc) or attempt >= attempts - 1:
                raise
            pause(delay * 1.4 ** attempt)


def replace_with_retry(src: str, dst: str, **retry: Any) -> None:
    """os.replace() that rides out a transient lock (see retry_on_lock)."""
    retry_on_lock(os.replace, src, dst, **retry)


def rename_with_retry(src: str, dst: str, **retry: Any) -> None:
    """rename_no_clobber() that rides out a transient lock: still refuses
    (FileExistsError, never retried) to overwrite an existing `dst`."""
    retry_on_lock(rename_no_clobber, src, dst, **retry)


def open_with_default_app(path: str) -> bool:
    """Opens `path` in whatever the OS has registered for it (the user's
    own comic reader, e-reader app, music or video player). Returns
    False if nothing could be launched, so the caller can tell the user;
    never raises for a missing file or a missing handler."""
    if not path or not os.path.exists(path):
        return False
    try:
        if sys.platform == "win32":
            os.startfile(os.path.normpath(path))  # type: ignore[attr-defined]  # win32 only
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError:
        return False
    return True


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

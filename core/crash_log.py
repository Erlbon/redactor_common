"""
redactor_common/core/crash_log.py

Installs a global exception hook that appends a full timestamped
traceback for any otherwise-unhandled exception to a log file, and
enables faulthandler on the same file for native-level crashes (a
segfault inside Qt or a C extension such as aubio never raises a
Python exception, so sys.excepthook can't see it).

This matters more for a PyQt app than a plain script: an exception
raised inside a signal/slot doesn't propagate back through Qt's C++
event loop the normal way, and a frozen --windowed build has no
console to print a traceback to.

Promoted from epubredactor's core/crash_log.py (cbz had an identical
port, mp3 a weaker rewrite that cut entries mid-way when trimming, and
video had none at all).

Pure logic, no Qt dependency -- install it before QApplication exists.
"""

from __future__ import annotations

import datetime
import faulthandler
import os
import sys
import traceback
from pathlib import Path
from typing import Callable

# A single log file, appended to (not overwritten), so a pattern across
# multiple crashes stays visible. Capped so a machine that crashes
# repeatedly doesn't grow it without bound.
MAX_LOG_BYTES = 2_000_000
ENTRY_SEPARATOR = "=" * 70

ExceptionCallback = Callable[[type, BaseException, object], None]

# Kept open for the life of the process -- faulthandler writes to the
# file descriptor directly at crash time, so it must not be closed.
_faulthandler_file = None


def format_crash_entry(exc_type, exc_value, exc_tb) -> str:
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    return f"\n{ENTRY_SEPARATOR}\n{timestamp}\n{tb_text}"


def trim_if_oversized(path: str | Path, max_bytes: int = MAX_LOG_BYTES) -> None:
    """Drops the oldest entries once the file exceeds `max_bytes`,
    cutting at an entry separator so the file still starts cleanly."""
    try:
        if os.path.getsize(path) <= max_bytes:
            return
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        trimmed = content[-max_bytes:]
        idx = trimmed.find(ENTRY_SEPARATOR)
        if idx > 0:
            trimmed = trimmed[idx:]
        with open(path, "w", encoding="utf-8") as f:
            f.write(trimmed)
    except OSError:
        pass  # trimming is a nice-to-have; never let it block logging itself


def write_crash_entry(
    exc_type, exc_value, exc_tb, path: str | Path, max_bytes: int = MAX_LOG_BYTES,
) -> None:
    """Appends one crash entry. Never raises -- a failure here (e.g. a
    read-only install folder) must not stop the caller from still
    showing the exception."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(format_crash_entry(exc_type, exc_value, exc_tb))
        trim_if_oversized(path, max_bytes)
    except OSError:
        pass


def install(
    log_path: str | Path,
    also_call: ExceptionCallback | None = None,
    enable_faulthandler: bool = True,
    write_entry: ExceptionCallback | None = None,
) -> None:
    """Installs the global hook writing to `log_path`.

    `also_call(exc_type, exc_value, exc_tb)`, if given, runs after
    logging (e.g. to show a dialog); it is never allowed to stop the
    previous hook from still running afterward. `enable_faulthandler`
    is best-effort: an unwritable log location still lets the app
    launch normally. `write_entry(exc_type, exc_value, exc_tb)` replaces
    the default write_crash_entry(..., log_path) -- for a project wrapper
    whose own tests patch its module-level writer.
    """
    global _faulthandler_file
    if enable_faulthandler:
        try:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            _faulthandler_file = open(log_path, "a", encoding="utf-8")
            faulthandler.enable(file=_faulthandler_file)
        except OSError:
            pass

    previous_hook = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        if write_entry is not None:
            try:
                write_entry(exc_type, exc_value, exc_tb)
            except Exception:  # noqa: BLE001 - logging must never block the rest
                pass
        else:
            write_crash_entry(exc_type, exc_value, exc_tb, log_path)
        if also_call is not None:
            try:
                also_call(exc_type, exc_value, exc_tb)
            except Exception:  # noqa: BLE001 - the crash handler itself must never crash
                pass
        previous_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook

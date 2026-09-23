"""
redactor_common/core/subprocess_utils.py

One way to shell out to an external CLI tool (ffmpeg, ffprobe,
mkvmerge, mp3val, keyfinder-cli, Calibre's ebook-convert, ...), with
the three things every project had to learn separately:

1. No console window. A --windowed PyInstaller build has no console of
   its own, but a child console program still allocates one when
   launched, which flashes on screen or steals focus. CREATE_NO_WINDOW
   only exists on Windows, so it's gated on sys.platform (epub v35,
   then independently re-fixed in mp3 and video).
2. stdin=DEVNULL. ffmpeg reads stdin for interactive commands and can
   hang forever waiting on it when launched from a GUI with an
   inherited handle (found on mp3; video's ffmpeg/MKVToolNix calls
   still lacked it).
3. UTF-8 output decoding. `text=True` decodes with the locale encoding
   (cp1252 on most Windows installs), but ffprobe's and mkvmerge's
   JSON output is UTF-8 -- a non-ASCII title ("Amélie") came back as
   mojibake ("AmÃ©lie") and got written back to the file on save.
   Decoding the raw bytes as UTF-8 with errors="replace" (epub's
   approach for Calibre) is always safe.

Pure logic, no Qt dependency.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Sequence

DEFAULT_TIMEOUT_SECONDS = 120


def no_window_kwargs() -> dict:
    """Extra kwargs to splat into subprocess.run()/Popen() so a launched
    console program doesn't pop up its own window. Empty dict on
    non-Windows platforms."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def decode_output(data: bytes | str | None) -> str:
    """Decodes captured tool output as UTF-8, never raising."""
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return data.decode("utf-8", errors="replace")


def run_tool(
    args: Sequence[str],
    timeout: float | None = DEFAULT_TIMEOUT_SECONDS,
    **kwargs,
) -> subprocess.CompletedProcess:
    """subprocess.run() with no console window, stdin=DEVNULL, captured
    output decoded as UTF-8 (stdout/stderr are always str, never None),
    and a timeout (pass timeout=None for a genuinely long-running job).

    Raises FileNotFoundError if the executable doesn't exist and
    subprocess.TimeoutExpired on timeout -- same as subprocess.run(),
    so callers keep their existing handling for both. Extra kwargs
    (cwd, env, ...) pass straight through.
    """
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    for key, value in no_window_kwargs().items():
        kwargs.setdefault(key, value)
    result = subprocess.run(
        [str(a) for a in args],
        capture_output=True,
        timeout=timeout,
        **kwargs,
    )
    result.stdout = decode_output(result.stdout)
    result.stderr = decode_output(result.stderr)
    return result


def popen_tool(args: Sequence[str], **kwargs) -> subprocess.Popen:
    """subprocess.Popen() with no console window and stdin=DEVNULL, for
    a long job the caller polls itself (e.g. an ffmpeg encode that must
    stay cancellable). Pipes are left to the caller; pass
    encoding="utf-8", errors="replace" alongside text=True rather than
    relying on the locale default."""
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    for key, value in no_window_kwargs().items():
        kwargs.setdefault(key, value)
    if kwargs.get("text") or kwargs.get("universal_newlines"):
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    return subprocess.Popen([str(a) for a in args], **kwargs)

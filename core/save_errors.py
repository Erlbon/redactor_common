"""
redactor_common/core/save_errors.py

Turns a raw exception from a failed save into a clear, actionable
message -- most importantly, recognizes Windows' classic MAX_PATH
(260-character) path-length limit specifically, since that's a
persistent property of WHERE the file lives, not a transient problem.
Retrying the exact same save immediately after will fail again in
exactly the same way; the file needs to move somewhere with a shorter
path first. A generic "file could not be written" message doesn't make
that distinction, and easily reads as "try again" when trying again
can't possibly help.

Note that a project's atomic-write save()'s atomic-write temp file (path + ".tmp_write")
is itself 10 characters longer than the book's own path, so this can
trip even when the original filename alone would have just barely
fit -- worth knowing when explaining why a file that looks like it's
close to the limit fails anyway.
"""

from __future__ import annotations

# Windows' own error code for "the filename or extension is too long"
# (ERROR_FILENAME_EXCED_RANGE) -- available as OSError.winerror on
# Windows, but that attribute doesn't exist at all on other platforms,
# so every access here goes through getattr() with a default.
WINERROR_FILENAME_TOO_LONG = 206

PATH_TOO_LONG_MESSAGE = (
    "This file's path is too long for Windows to write to (it's over the "
    "260-character limit most Windows systems still enforce by default). "
    "Saving again won't help without moving the book -- shorten the folder "
    "path it lives in, or move it somewhere with a shorter path, then try "
    "again."
)


def is_path_too_long_error(exc: BaseException) -> bool:
    """True if `exc` looks like a Windows path/filename-too-long error.
    Checks the Windows-specific winerror code first (most reliable when
    available), falling back to a message-based heuristic for
    Python/OS combinations that surface this as a plainer OSError
    instead."""
    if getattr(exc, "winerror", None) == WINERROR_FILENAME_TOO_LONG:
        return True
    message = str(exc).lower()
    return "too long" in message and ("path" in message or "filename" in message)


def describe_save_error(exc: BaseException) -> str:
    """The message to show/store for a failed save. Recognizes a
    path-too-long error specifically and explains it clearly; anything
    else falls back to the exception's own message, unchanged."""
    if is_path_too_long_error(exc):
        return PATH_TOO_LONG_MESSAGE
    return str(exc)

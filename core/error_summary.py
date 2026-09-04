"""
redactor_common/core/error_summary.py

Turns a list of per-item error messages into a short, bounded preview
string, safe to show in a status label, QMessageBox, or similar
single-line-ish UI element.

Some error sources can be extremely verbose -- Calibre's own
fetch-ebook-metadata is a known one: its stderr on a "nothing found"
search logs every plugin's search attempts, URLs queried, and
intermediate results, sometimes hundreds of lines. Without a cap, a
single unusually chatty error can balloon a plain QLabel (which has no
built-in truncation or scroll) to an unusable size, dragging the whole
dialog down with it. This bounds both how many individual errors are
shown and how long each one's text can run, regardless of what the
underlying error source happens to produce.
"""

from __future__ import annotations

MAX_ERRORS_SHOWN = 3
MAX_MESSAGE_CHARS = 300  # per individual error message


def summarize_errors(
    errors: list[str], max_shown: int = MAX_ERRORS_SHOWN, max_chars: int = MAX_MESSAGE_CHARS
) -> str:
    """`errors` is a list of already-formatted strings (typically
    "book: reason"). Returns at most `max_shown` of them, each truncated
    to `max_chars`, joined with "; ", with a trailing ", ..." appended
    if there were more than `max_shown` to begin with."""
    shown = []
    for message in errors[:max_shown]:
        if len(message) > max_chars:
            message = message[:max_chars].rstrip() + "\u2026"
        shown.append(message)
    preview = "; ".join(shown)
    if len(errors) > max_shown:
        preview += ", ..."
    return preview

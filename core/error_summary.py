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


def wrapped_errors(
    errors: list[str], max_shown: int = 5, max_chars: int = 600, width: int = 70
) -> str:
    """Like summarize_errors(), for a QMessageBox: one error per paragraph,
    hard-wrapped at `width` characters so the whole text fits the dialog
    whatever the widget does with a long line (a long "file: reason" used to
    run off the dialog's right edge and hide the reason). Continuation lines
    are indented; a path or word longer than `width` is broken. `max_chars`
    still bounds one runaway message."""
    import textwrap

    paragraphs = []
    for message in errors[:max_shown]:
        if len(message) > max_chars:
            message = message[:max_chars].rstrip() + "…"
        paragraphs.append(
            textwrap.fill(
                message, width=width, subsequent_indent="    ",
                break_long_words=True, break_on_hyphens=False,
            )
        )
    text = "\n\n".join(paragraphs)
    if len(errors) > max_shown:
        text += f"\n\n... and {len(errors) - max_shown} more"
    return text

"""
core/long_paths.py

Folder and file names that end in a dot or a space ('R.E.M.', 'Tolkien, J.R.R.'). Windows' own tools will not make them,
but they reach a share from elsewhere (Linux, Samba, copy tools), and an ordinary Windows path to one is cut short: the
dot is dropped and nothing is found. literal(path) gives the path Windows opens as written. Promoted from the Folder
Redactor (2026-11-21), which met it in music, book and comic trees; comic-checker met the same.

Use it for the OS call only (os.scandir, open, os.link...) and keep the plain path for everything shown or compared. The
Windows shell (send2trash, the Recycle Bin) does not take these paths at all; see core/trash.py shell_path().

  needs_literal(path)   a name in it ends in a dot or a space (Windows only)
  literal(path)         the path as a string, with the extended-length prefix only when needs_literal()
"""

from __future__ import annotations

import os
from pathlib import Path

BACKSLASH = chr(92)
LONG_PREFIX = BACKSLASH * 2 + "?" + BACKSLASH  # Windows' literal-path prefix
UNC_LONG_PREFIX = LONG_PREFIX + "UNC" + BACKSLASH


def needs_literal(path: str | os.PathLike) -> bool:
    return os.name == "nt" and any(part.endswith((".", " ")) and part not in (".", "..")
                                   for part in Path(os.fspath(path)).parts[1:])


def literal(path: str | os.PathLike) -> str:
    text = os.fspath(path)
    if text.startswith(LONG_PREFIX) or not needs_literal(text):
        return text
    if not os.path.isabs(text):
        text = os.path.join(os.getcwd(), text)  # (os.path.abspath would cut the trailing dot)
    text = text.replace("/", BACKSLASH)
    if text.startswith(BACKSLASH * 2):
        return UNC_LONG_PREFIX + text[2:]
    return LONG_PREFIX + text

"""
redactor_common/core/rename_pattern.py

mp3tag-style "Tag -> Filename" engine: turn a set of metadata field
values into a filename using a pattern with %placeholder% tokens, e.g.

    %series% %series_index% - %title%
    %artist% - %title%

No GUI dependencies -- pure string logic, so it's fully unit-testable.

Generalized from the epub project's original version: that version
took an EpubMetadata object directly, which meant the whole engine was
epub-specific. This version takes a plain dict[str, str] of field
values instead -- each project supplies its own placeholder list and
its own function for turning its metadata object into a values dict
(see placeholder_values below), and everything downstream of that is
shared.
"""

from __future__ import annotations

import os
import re
from typing import Callable

# Characters Windows forbids in filenames, plus control characters.
_ILLEGAL_CHARS_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MULTI_SPACE_RE = re.compile(r"[ \t]+")
_REPEATED_SEPARATOR_RE = re.compile(r"(?:\s*-\s*){2,}")
_TRIM_SEPARATORS_RE = re.compile(r"^[\s\-\u2013\u2014]+|[\s\-\u2013\u2014]+$")

# Windows reserved device names -- a file literally named "CON.epub" fails.
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

MAX_FILENAME_LENGTH = 150  # stem only, conservative vs. Windows' ~255 path limit


def zero_pad_numeric_value(value: str) -> str:
    """Zero-pad a numeric field (e.g. a series index or episode number)
    to at least 2 digits, correctly handling decimal sub-indices like
    "5.5" -> "05.5" (only the integer part gets padded; the fractional
    part is left exactly as typed)."""
    value = (value or "").strip()
    if not value:
        return value
    if "." in value:
        int_part, sep, frac_part = value.partition(".")
        if int_part.isdigit():
            return f"{int_part.zfill(2)}{sep}{frac_part}"
        return value
    if value.isdigit():
        return value.zfill(2)
    return value


def sanitize_filename(name: str) -> str:
    """Strip characters Windows forbids in filenames and tidy whitespace."""
    name = _ILLEGAL_CHARS_RE.sub("", name)
    name = _MULTI_SPACE_RE.sub(" ", name)
    # Empty %field% tokens commonly leave behind dangling " - " runs
    # (e.g. no series -> " - Title"); collapse those down.
    name = _REPEATED_SEPARATOR_RE.sub(" - ", name)
    name = _TRIM_SEPARATORS_RE.sub("", name)
    name = name.strip().strip(".")  # trailing dots/spaces are invalid on Windows
    return name


_TOKEN_RE = re.compile(r"%(\w+)%")


def render_filename(
    values: dict[str, str],
    pattern: str,
    fallback: str = "untitled",
) -> str:
    """Render a filename stem (no extension) from `pattern`, substituting
    each %field% token with values.get(field, ""). A token for a field
    the caller's values dict doesn't mention at all (as opposed to
    mentioning with an empty value) is also substituted as "" -- it
    should never survive into the rendered name as literal "%field%"
    text just because the caller's dict happened to omit that key.

    Falls back to `fallback` if the pattern produces nothing usable (e.g.
    every referenced field was empty).
    """
    result = _TOKEN_RE.sub(lambda m: values.get(m.group(1), "") or "", pattern)

    result = sanitize_filename(result)

    if not result:
        result = fallback

    if result.upper() in _RESERVED_NAMES:
        result = f"_{result}"

    if len(result) > MAX_FILENAME_LENGTH:
        result = result[:MAX_FILENAME_LENGTH].rstrip()

    return result


def unique_path(directory: str, stem: str, ext: str, taken: set[str]) -> str:
    """Return a filesystem path for `stem+ext` inside `directory` that
    doesn't collide with anything already on disk or already claimed in
    this batch (`taken`, a set of absolute paths already assigned during
    the current rename/export run -- normalized case-insensitively since
    Windows filesystems are case-insensitive by default).
    """
    def norm(p: str) -> str:
        return os.path.normcase(os.path.abspath(p))

    candidate = os.path.join(directory, f"{stem}{ext}")
    if not os.path.exists(candidate) and norm(candidate) not in taken:
        return candidate

    n = 2
    while True:
        candidate = os.path.join(directory, f"{stem} ({n}){ext}")
        if not os.path.exists(candidate) and norm(candidate) not in taken:
            return candidate
        n += 1


def validate_filename_stem(name: str) -> str:
    """Checks whether `name` (without extension) is a valid Windows
    filename on its own merits -- returns "" if it's fine, or a clear,
    specific reason why not. Used when someone types a filename
    directly (see rename_file() below), as opposed to render_filename()'s
    own pattern output, which is sanitized automatically rather than
    rejected -- nobody's hand-typing pattern output character by
    character, but a single, deliberate rename deserves a clear
    "here's what's wrong" instead of silently changing what was
    actually typed."""
    if not name.strip():
        return "The filename can't be empty."
    illegal = sorted(set(_ILLEGAL_CHARS_RE.findall(name)))
    if illegal:
        return f"These characters aren't allowed in a filename: {' '.join(illegal)}"
    if name != name.rstrip():
        return "A filename can't end with a space."
    if name.rstrip(".") != name:
        return "A filename can't end with a dot."
    if name.upper() in _RESERVED_NAMES:
        return f'"{name}" is a reserved name on Windows and can\'t be used.'
    if len(name) > MAX_FILENAME_LENGTH:
        return f"That name is too long (max {MAX_FILENAME_LENGTH} characters)."
    return ""


def rename_file_on_disk(path: str, new_stem: str) -> str:
    """Renames the file at `path` (same folder, same extension) to
    `new_stem`. Returns the new path. For fixing a typo or small
    mistake in one file's name directly, without going through the
    pattern-based Rename/Export tool.

    Raises ValueError if new_stem isn't a valid Windows filename (see
    validate_filename_stem()), or FileExistsError if a file with that
    name already exists in the same folder -- deliberately NOT
    auto-numbered here, unlike Rename/Export's batch mode: a single,
    deliberate rename should end up with exactly the name given, or
    fail clearly, not silently end up numbered to something else
    without the person necessarily noticing.

    A no-op (doesn't touch the filesystem, returns `path` unchanged) if
    new_stem is already the file's current name."""
    error = validate_filename_stem(new_stem)
    if error:
        raise ValueError(error)

    directory = os.path.dirname(path)
    ext = os.path.splitext(path)[1]
    new_path = os.path.join(directory, new_stem + ext)

    if os.path.normcase(os.path.abspath(new_path)) == os.path.normcase(os.path.abspath(path)):
        return path

    if os.path.exists(new_path):
        raise FileExistsError(
            f'A file named "{os.path.basename(new_path)}" already exists in this folder.'
        )

    os.rename(path, new_path)
    return new_path

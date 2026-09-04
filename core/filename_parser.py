"""
redactor_common/core/filename_parser.py

The reverse of core/rename_pattern.py: instead of turning metadata into a
filename, this turns a filename BACK into metadata field values, using
the same %placeholder% pattern syntax.

How it works: the pattern is compiled into a regex, where each %field%
token becomes a named capture group and everything else (spaces, dashes,
punctuation) is treated as literal text that must match exactly. This
works well for patterns with clear separators between fields (which is
the normal case -- e.g. "%series% %series_index% - %title%"), but is
inherently ambiguous for adjacent fields with no separator between them,
or when a field's own value happens to contain the literal text used as
a separator elsewhere in the pattern. There's no way around that with a
plain pattern-matching approach; it's a limitation worth knowing about
rather than something to silently paper over.

Generalized from the epub project's version: field validity and which
fields get a stricter numeric regex are now passed in per-project
rather than imported from a fixed epub placeholder list.
"""

from __future__ import annotations

import re

_ISBN_FIELD_PATTERN = r"[\dXx\-]+"
_NUMERIC_FIELD_PATTERN = r"\d+(?:\.\d+)?"

_TOKEN_RE = re.compile(r"%(\w+)%")


def _flexible_literal_regex(literal: str) -> str:
    """Converts a literal (non-placeholder) pattern-text segment into a
    regex fragment where any run of whitespace matches any run of
    whitespace in the filename -- one space in the pattern still
    matches one space, but also two, three, or a stray tab, rather than
    requiring the exact same character-for-character spacing. Real
    filenames often pick up an extra or missing space somewhere (a
    double space from a rename tool, inconsistent spacing around a
    dash), and that shouldn't break matching altogether when the
    surrounding text is otherwise a clean match. Non-whitespace
    characters are still escaped and matched exactly -- this doesn't
    loosen anything about the literal punctuation/text itself, only
    how much whitespace is required where the pattern already has some."""
    pieces = []
    for chunk in re.split(r"(\s+)", literal):
        if not chunk:
            continue
        pieces.append(r"\s+" if chunk.isspace() else re.escape(chunk))
    return "".join(pieces)


def build_parser_regex(
    pattern: str,
    valid_field_keys: set[str],
    numeric_fields: set[str] = frozenset(),
    isbn_like_fields: set[str] = frozenset(),
) -> re.Pattern:
    """Compile a %field% pattern into a regex with one named group per
    (first occurrence of a) valid field token. A field used a second time
    in the same pattern, or an unrecognized %something%, is treated as
    literal text to match rather than causing a crash.

    `numeric_fields` get a stricter digit-shaped regex fragment instead
    of the generic ".+?" -- this matters in practice: a pattern like
    "%series% %series_index% - %title%" only has a single space
    separating a (possibly multi-word) series name from the index,
    which is ambiguous with a plain ".+?" match. Requiring the index to
    actually look like a number resolves that ambiguity in the common
    case. `isbn_like_fields` get a digit/X/hyphen fragment for the same
    reason.
    """
    parts: list[str] = []
    seen_fields: set[str] = set()
    last_end = 0

    for m in _TOKEN_RE.finditer(pattern):
        literal = pattern[last_end:m.start()]
        if literal:
            parts.append(_flexible_literal_regex(literal))

        field = m.group(1)
        if field in valid_field_keys and field not in seen_fields:
            if field in numeric_fields:
                parts.append(f"(?P<{field}>{_NUMERIC_FIELD_PATTERN})")
            elif field in isbn_like_fields:
                parts.append(f"(?P<{field}>{_ISBN_FIELD_PATTERN})")
            else:
                parts.append(f"(?P<{field}>.+?)")
            seen_fields.add(field)
        else:
            parts.append(re.escape(m.group(0)))

        last_end = m.end()

    trailing = pattern[last_end:]
    if trailing:
        parts.append(_flexible_literal_regex(trailing))

    return re.compile("^" + "".join(parts) + "$")


def strip_leading_zeros(value: str) -> str:
    """Strips leading zeros from the integer part of a numeric field,
    preserving any decimal part exactly ("03.5" -> "3.5", "007" -> "7",
    "0" -> "0"). Filenames often zero-pad a number purely for correct
    sort order ("Book 03", "Episode 03"), but that padding isn't
    meaningful metadata -- it's a filename-ordering artifact, not part
    of the actual value, so it shouldn't carry over into the field
    value itself."""
    if not value:
        return value
    if "." in value:
        int_part, sep, frac_part = value.partition(".")
        stripped = int_part.lstrip("0") or "0"
        return f"{stripped}{sep}{frac_part}"
    return value.lstrip("0") or "0"


def parse_filename(
    filename_stem: str,
    pattern: str,
    valid_field_keys: set[str],
    numeric_fields: set[str] = frozenset(),
    isbn_like_fields: set[str] = frozenset(),
    strip_leading_zeros_fields: set[str] = frozenset(),
) -> dict[str, str] | None:
    """Extract field values from a filename (without extension) using
    `pattern`. Returns None if the filename doesn't match the pattern's
    shape at all; returns {} if the pattern has no recognized fields.

    `strip_leading_zeros_fields` names which extracted numeric fields
    (typically a subset of numeric_fields, e.g. a series/episode index
    but not a 4-digit year) get strip_leading_zeros() applied.
    """
    regex = build_parser_regex(pattern, valid_field_keys, numeric_fields, isbn_like_fields)
    match = regex.match((filename_stem or "").strip())
    if match is None:
        return None
    result = {key: (value or "").strip() for key, value in match.groupdict().items()}
    for field in strip_leading_zeros_fields:
        if field in result:
            result[field] = strip_leading_zeros(result[field])
    return result


def count_matching_filenames(
    filenames: list[str],
    pattern: str,
    valid_field_keys: set[str],
    numeric_fields: set[str] = frozenset(),
    isbn_like_fields: set[str] = frozenset(),
) -> int:
    """How many of these filename stems does `pattern` successfully
    parse, extracting at least one field? Used to detect which of a set
    of candidate patterns (e.g. previously-used ones from history)
    actually fits a given batch of loaded files, rather than making the
    person try each one by hand to find out."""
    count = 0
    for stem in filenames:
        parsed = parse_filename(stem, pattern, valid_field_keys, numeric_fields, isbn_like_fields)
        if parsed:  # None or {} both count as no match
            count += 1
    return count


def best_matching_pattern(
    filenames: list[str],
    patterns: list[str],
    valid_field_keys: set[str],
    numeric_fields: set[str] = frozenset(),
    isbn_like_fields: set[str] = frozenset(),
) -> tuple[str, int] | None:
    """Given a list of candidate patterns (e.g. pattern history, newest
    first), returns the (pattern, match_count) pair that matches the
    most of these filename stems -- or None if none of them match
    anything at all. A tie is broken toward whichever pattern comes
    first in `patterns`, so passing history in its natural newest-first
    order naturally prefers the more recently-used pattern when two tie
    on match count."""
    best: str | None = None
    best_count = 0
    for pattern in patterns:
        count = count_matching_filenames(filenames, pattern, valid_field_keys, numeric_fields, isbn_like_fields)
        if count > best_count:
            best = pattern
            best_count = count
    return (best, best_count) if best is not None else None

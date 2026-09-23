"""
redactor_common/core/filename_parser.py

The reverse of core/rename_pattern.py: instead of turning metadata into a
filename, this turns a filename BACK into metadata field values, using
the same %placeholder% pattern syntax.

How it works: the pattern is compiled into a regex, where each %field%
token becomes a named capture group and everything else (spaces, dashes,
punctuation) is treated as literal text that must match. This works
well for patterns with clear separators between fields (the normal case
-- e.g. "%series% %series_index% - %title%"), but is inherently
ambiguous for adjacent fields with no separator between them, or when a
field's own value contains the literal text used as a separator
elsewhere. There's no way around that with plain pattern matching; it's
a limitation worth knowing about rather than something to paper over.

Generalized from the epub project's version: field validity and each
field's regex shape are passed in per project rather than imported from
a fixed epub placeholder list. Epub's later additions are all here too
(2026-09-23): optional (...)/[...]/{...} groups, per-field regex shapes
(`field_patterns` -- series index ranges, month names, 2-or-4-digit
years), per-field value normalizers, and tolerance of missing spaces.

Whitespace: literal spaces in the pattern first have to match at least
one space in the filename. Only if that finds no match at all is the
pattern retried with spaces optional (epub's looser rule, for names
with a missing space around a separator). Trying strict first matters:
the loose rule alone parsed "Jean-Paul Sartre - Nausea" with pattern
"%authors% - %title%" as author "Jean", because " - " could then match
the bare hyphen inside the name.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Callable, Mapping

# -- ready-made field shapes a project can put in `field_patterns` -------------

NUMERIC_FIELD_PATTERN = r"\d+(?:\.\d+)?"
ISBN_FIELD_PATTERN = r"[\dXx\-]+"

# A series/volume/episode index: 1-3 digits (so it can never be confused
# with a 4-digit year), optionally a decimal sub-index ("5.5") OR a range
# for an omnibus ("1-6"), optionally a trailing ordinal "." either way.
SERIES_INDEX_FIELD_PATTERN = r"\d{1,3}(?:\.\d+|-\d{1,3})?\.?"

# 4 digits, occasionally 2 -- tried longest-first so "2020" isn't read as "20".
YEAR_FIELD_PATTERN = r"\d{4}|\d{2}"
DAY_FIELD_PATTERN = r"\d{1,2}"

MONTH_NAMES = {
    "jan": "1", "january": "1",
    "feb": "2", "february": "2",
    "mar": "3", "march": "3",
    "apr": "4", "april": "4",
    "may": "5",
    "jun": "6", "june": "6",
    "jul": "7", "july": "7",
    "aug": "8", "august": "8",
    "sep": "9", "sept": "9", "september": "9",
    "oct": "10", "october": "10",
    "nov": "11", "november": "11",
    "dec": "12", "december": "12",
}
_MONTH_NAME_ALTERNATION = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
MONTH_FIELD_PATTERN = rf"(?i:(?:\d{{1,2}}|{_MONTH_NAME_ALTERNATION})\.?)"

_TOKEN_RE = re.compile(r"%(\w+)%")
_OPTIONAL_GROUP_RE = re.compile(r"\[([^\[\]]*)\]|\(([^()]*)\)|\{([^{}]*)\}")
# The separator run right after an optional group's closing character,
# folded into the same optional group (see build_parser_regex()).
_TRAILING_SEPARATOR_RE = re.compile(r"[^%\[({\w]*")

Normalizer = Callable[[str], str]


# -- value normalizers ------------------------------------------------------------

def strip_leading_zeros(value: str) -> str:
    """Strips filename-ordering zero padding from a numeric value,
    handling all three index shapes: "007" -> "7", "03.5" -> "3.5"
    (decimal part kept exactly), "01-06" -> "1-6" (each side of an
    omnibus range). A trailing ordinal "." is dropped too ("5." -> "5")."""
    if not value:
        return value
    value = value.rstrip(".")
    if not value:
        return value
    if "-" in value:
        left, sep, right = value.partition("-")
        return f"{left.lstrip('0') or '0'}{sep}{right.lstrip('0') or '0'}"
    if "." in value:
        int_part, sep, frac_part = value.partition(".")
        return f"{int_part.lstrip('0') or '0'}{sep}{frac_part}"
    return value.lstrip("0") or "0"


def normalize_month(value: str) -> str:
    """A month captured as a name ("Jan", "January.") becomes its plain,
    unpadded digit string ("1"); a digit month is returned unchanged."""
    v = value.strip().rstrip(".")
    if not v:
        return v
    return MONTH_NAMES.get(v.lower(), v)


def normalize_field_value(value: str) -> str:
    """Case- and whitespace-insensitive comparison key for a captured
    value ("Terry  Pratchett" == "TERRY PRATCHETT"). Comparison only --
    callers still display/store the originally captured value."""
    return " ".join((value or "").split()).casefold()


# -- regex building ---------------------------------------------------------------

def _flexible_literal_regex(literal: str, whitespace_optional: bool = False) -> str:
    """Escapes a literal pattern segment, letting any run of whitespace
    in it match any run of whitespace in the filename (a stray double
    space or tab shouldn't break matching) -- or, with
    `whitespace_optional`, no whitespace at all."""
    ws = r"\s*" if whitespace_optional else r"\s+"
    pieces = []
    for chunk in re.split(r"(\s+)", literal):
        if not chunk:
            continue
        pieces.append(ws if chunk.isspace() else re.escape(chunk))
    return "".join(pieces)


def _field_regex(
    field: str,
    numeric_fields: set[str],
    isbn_like_fields: set[str],
    field_patterns: Mapping[str, str],
) -> str:
    if field in field_patterns:
        return field_patterns[field]
    if field in numeric_fields:
        return NUMERIC_FIELD_PATTERN
    if field in isbn_like_fields:
        return ISBN_FIELD_PATTERN
    return ".+?"


def build_parser_regex(
    pattern: str,
    valid_field_keys: set[str],
    numeric_fields: set[str] = frozenset(),
    isbn_like_fields: set[str] = frozenset(),
    *,
    field_patterns: Mapping[str, str] | None = None,
    optional_groups: bool = True,
    whitespace_optional: bool = False,
) -> re.Pattern:
    """Compile a %field% pattern into a regex with one named group per
    (first occurrence of a) valid field token. A field used a second time
    in the same pattern, or an unrecognized %something%, is treated as
    literal text to match rather than causing a crash.

    Field shapes: `field_patterns` (field -> regex fragment, no capture
    groups) wins, then `numeric_fields` get a digit-shaped fragment and
    `isbn_like_fields` a digit/X/hyphen one, else a lazy ".+?". A shape
    hint matters in practice: "%series% %series_index% - %title%" only
    has one space between a multi-word series and its index, which a
    plain ".+?" can't disambiguate.

    `optional_groups`: a (...)/[...]/{...} segment containing a %field%
    compiles to an OPTIONAL group (mirroring render_filename()), so a
    file with no series still matches "%authors% - [%series%
    %series_index%] - %title%". The separator text right after the
    group's closing character is folded into the same optional group:
    render_filename() collapses the doubled separator an empty group
    leaves behind, so matching has to accept both shapes. A BARE field is
    always required -- making one optional on its own lets a greedy
    neighbour silently swallow it; wrap it to make it optional.
    """
    field_patterns = field_patterns or {}
    seen_fields: set[str] = set()

    def compile_tokens(segment: str) -> str:
        parts: list[str] = []
        last_end = 0
        for m in _TOKEN_RE.finditer(segment):
            literal = segment[last_end:m.start()]
            if literal:
                parts.append(_flexible_literal_regex(literal, whitespace_optional))
            field = m.group(1)
            if field in valid_field_keys and field not in seen_fields:
                fragment = _field_regex(field, numeric_fields, isbn_like_fields, field_patterns)
                parts.append(f"(?P<{field}>{fragment})")
                seen_fields.add(field)
            else:
                parts.append(re.escape(m.group(0)))
            last_end = m.end()
        trailing = segment[last_end:]
        if trailing:
            parts.append(_flexible_literal_regex(trailing, whitespace_optional))
        return "".join(parts)

    if not optional_groups:
        return re.compile("^" + compile_tokens(pattern) + "$")

    parts: list[str] = []
    last_end = 0
    for m in _OPTIONAL_GROUP_RE.finditer(pattern):
        literal_before = pattern[last_end:m.start()]
        if literal_before:
            parts.append(compile_tokens(literal_before))
        open_ch, close_ch = m.group(0)[0], m.group(0)[-1]
        inner = next(g for g in m.groups() if g is not None)
        if _TOKEN_RE.search(inner):
            sep_match = _TRAILING_SEPARATOR_RE.match(pattern, m.end())
            after_end = sep_match.end() if sep_match else m.end()
            group = re.escape(open_ch) + compile_tokens(inner) + re.escape(close_ch)
            trailing_literal = pattern[m.end():after_end]
            if trailing_literal:
                group += compile_tokens(trailing_literal)
            parts.append(f"(?:{group})?")
            last_end = after_end
        else:
            parts.append(re.escape(m.group(0)))  # plain literal wrapper, required as typed
            last_end = m.end()
    trailing = pattern[last_end:]
    if trailing:
        parts.append(compile_tokens(trailing))
    return re.compile("^" + "".join(parts) + "$")


def parse_filename(
    filename_stem: str,
    pattern: str,
    valid_field_keys: set[str],
    numeric_fields: set[str] = frozenset(),
    isbn_like_fields: set[str] = frozenset(),
    strip_leading_zeros_fields: set[str] = frozenset(),
    *,
    field_patterns: Mapping[str, str] | None = None,
    normalizers: Mapping[str, Normalizer] | None = None,
    optional_groups: bool = True,
    loose_whitespace_fallback: bool = True,
) -> dict[str, str] | None:
    """Extract field values from a filename (without extension) using
    `pattern`. Returns None if the filename doesn't match the pattern's
    shape at all; returns {} if the pattern has no recognized fields.

    `strip_leading_zeros_fields` get strip_leading_zeros() applied (an
    index, but not a 4-digit year); `normalizers` maps a field to any
    other clean-up function (e.g. normalize_month). A field absent from
    the match (inside an optional group that wasn't there) comes back
    as "".
    """
    stem = (filename_stem or "").strip()
    kwargs = dict(field_patterns=field_patterns, optional_groups=optional_groups)
    match = build_parser_regex(
        pattern, valid_field_keys, numeric_fields, isbn_like_fields, **kwargs
    ).match(stem)
    if match is None and loose_whitespace_fallback:
        match = build_parser_regex(
            pattern, valid_field_keys, numeric_fields, isbn_like_fields,
            whitespace_optional=True, **kwargs,
        ).match(stem)
    if match is None:
        return None
    result = {key: (value or "").strip() for key, value in match.groupdict().items()}
    for field in strip_leading_zeros_fields:
        if result.get(field):
            result[field] = strip_leading_zeros(result[field])
    for field, normalize in (normalizers or {}).items():
        if result.get(field):
            result[field] = normalize(result[field])
    return result


def count_matching_filenames(
    filenames: list[str],
    pattern: str,
    valid_field_keys: set[str],
    numeric_fields: set[str] = frozenset(),
    isbn_like_fields: set[str] = frozenset(),
    **parse_kwargs,
) -> int:
    """How many of these filename stems does `pattern` successfully
    parse, extracting at least one field? Used to detect which of a set
    of candidate patterns (e.g. pattern history) actually fits a batch
    of loaded files. Extra keyword arguments pass to parse_filename()."""
    count = 0
    for stem in filenames:
        parsed = parse_filename(
            stem, pattern, valid_field_keys, numeric_fields, isbn_like_fields, **parse_kwargs
        )
        if parsed:  # None or {} both count as no match
            count += 1
    return count


def best_matching_pattern(
    filenames: list[str],
    patterns: list[str],
    valid_field_keys: set[str],
    numeric_fields: set[str] = frozenset(),
    isbn_like_fields: set[str] = frozenset(),
    **parse_kwargs,
) -> tuple[str, int] | None:
    """Returns the (pattern, match_count) that matches the most of these
    filename stems, or None if none match anything. Ties go to whichever
    pattern comes first, so newest-first history prefers the more
    recently used pattern."""
    best: str | None = None
    best_count = 0
    for pattern in patterns:
        count = count_matching_filenames(
            filenames, pattern, valid_field_keys, numeric_fields, isbn_like_fields, **parse_kwargs
        )
        if count > best_count:
            best = pattern
            best_count = count
    return (best, best_count) if best is not None else None


def field_value_counts(
    filenames: list[str],
    pattern: str,
    field: str,
    valid_field_keys: set[str],
    numeric_fields: set[str] = frozenset(),
    isbn_like_fields: set[str] = frozenset(),
    **parse_kwargs,
) -> dict[str, int]:
    """normalized value -> how many of these filename stems produced that
    (non-empty) `field` value -- see normalize_field_value(). A value
    shared by several files (an author, a series) is corroborating
    evidence that the pattern assigns that field correctly; a title is
    normally unique per file."""
    counts: Counter[str] = Counter()
    for stem in filenames:
        parsed = parse_filename(
            stem, pattern, valid_field_keys, numeric_fields, isbn_like_fields, **parse_kwargs
        )
        if parsed and parsed.get(field):
            counts[normalize_field_value(parsed[field])] += 1
    return dict(counts)

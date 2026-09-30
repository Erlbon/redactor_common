"""
redactor_common/core/path_parser.py

Parse Filename, extended to the folder PATH: the mirror of "Move into
folders" (core/move_plan.py). A pattern may contain "/" (or "\\"), e.g.
"%genre%/%author%/%series%/%title%"; the LAST segment matches the file
stem and each earlier segment matches one parent folder, in order.

Every segment is compiled and matched by core/filename_parser.py's own
machinery, so a segment supports exactly what a filename pattern does:
%field% tokens, optional (...)/[...]/{...} groups, per-field shapes
(`field_patterns`), several fields in one segment ("%series%
%series_index% - %title%"), and the strict-then-loose whitespace retry.

Matching runs RIGHT TO LEFT, so the pattern stays anchored to the file:
  - a pattern with FEWER segments than the path ignores the outer folders;
  - a pattern with MORE segments than the path matches what exists from
    the right, leaves the leftover (outermost) pattern segments' fields
    empty and reports them in `missing_segments`.

Values stay raw by default -- a folder often holds "Tolkien, J.R.R."
(author sort form) or "Fantasy". Apps can plug per-field `normalizers`
exactly as for filenames (e.g. an author-sort -> display-name function
for "author"); this module deliberately knows no app-specific field.

Confidence (0..1) tells a caller how far to trust a result -- a Redact
step can auto-apply only results at or above HIGH_CONFIDENCE:
  - each pattern segment scores 1.0 when matched, 0.0 when missing or not
    matching; the result is the mean over the pattern's segments;
  - a FOLDER segment that matched only as a bare whole-segment capture
    (the segment is just "%field%" with no shape) scores LOOSE_SEGMENT_WEIGHT
    -- almost any folder name matches that, so it proves little. (The stem
    segment is exempt: a bare %title% is the normal way to write it.)
  - a segment matched only after the optional-whitespace retry is scaled
    by LOOSE_WHITESPACE_FACTOR;
  - `corroborate(field, value) -> int` (optional; how many items in the
    batch share that folder value, the item itself included) lifts a
    folder segment by CORROBORATION_BONUS when any of its fields is shared
    by at least two items -- sibling books in one author/series folder
    agreeing is strong evidence the pattern assigns the field correctly.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from typing import Callable, Mapping

from redactor_common.core.filename_parser import (
    Normalizer, build_parser_regex, normalize_field_value, strip_leading_zeros,
)

HIGH_CONFIDENCE = 0.9
LOOSE_SEGMENT_WEIGHT = 0.75
LOOSE_WHITESPACE_FACTOR = 0.9
CORROBORATION_BONUS = 0.2
DEFAULT_MAX_SEGMENTS = 6  # cap when the path is outside the root and no count is given

_SEPARATORS_RE = re.compile(r"[\\/]")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_UNC_RE = re.compile(r"^[\\/]{2}[^\\/]+[\\/][^\\/]+")
_BARE_FIELD_RE = re.compile(r"^\s*%\w+%\s*$")

Corroborator = Callable[[str, str], int]


def is_path_pattern(pattern: str) -> bool:
    """True when `pattern` contains a folder separator (/ or \\), i.e. it
    is a path pattern rather than a plain filename pattern. Apps use this
    to tell the two kinds apart in a shared pattern history."""
    return bool(_SEPARATORS_RE.search(pattern or ""))


def split_pattern_history(history: list[str]) -> tuple[list[str], list[str]]:
    """(filename_patterns, path_patterns), each in the original order."""
    names = [p for p in history if not is_path_pattern(p)]
    paths = [p for p in history if is_path_pattern(p)]
    return names, paths


def split_path_pattern(pattern: str) -> list[str]:
    """The pattern's segments, split on / and \\ with blank segments
    (doubled, leading or trailing separators) dropped. The LAST segment
    matches the file stem; earlier ones match parent folders in order."""
    return [part for part in _SEPARATORS_RE.split(pattern or "") if part.strip()]


def _strip_anchor(path: str) -> str:
    """Drops a drive ("C:") or UNC share prefix so it never becomes a segment."""
    path = path or ""
    m = _UNC_RE.match(path)
    if m:
        return path[m.end():]
    if _DRIVE_RE.match(path):
        return path[2:]
    return path


def _folder_parts(path: str) -> list[str]:
    return [p for p in _SEPARATORS_RE.split(_strip_anchor(path)) if p and p != "."]


def _key(part: str) -> str:
    return part.casefold() if sys.platform == "win32" else part


def relative_segments(path: str, root: str = "", max_segments: int | None = None) -> list[str]:
    """The path's segments for matching: the folders below `root`, then
    the file stem (extension dropped).

    If the path is not under `root` (or `root` is empty), falls back to
    the last `max_segments` segments (parent folders plus the stem;
    DEFAULT_MAX_SEGMENTS when not given) -- pass the pattern's segment
    count. The drive letter / UNC share is never a segment, nor is an
    outermost folder beyond the cap."""
    parts = _folder_parts(path)
    if not parts:
        return []
    parts[-1] = os.path.splitext(parts[-1])[0]
    root_parts = _folder_parts(root) if root else []
    n = len(root_parts)
    if root_parts and len(parts) > n and [_key(p) for p in parts[:n]] == [_key(p) for p in root_parts]:
        return parts[n:]
    cap = max_segments if max_segments and max_segments > 0 else DEFAULT_MAX_SEGMENTS
    return parts[-cap:]


@dataclass
class PathParseResult:
    """`values`: every recognized field of the pattern -> value ("" when
    not captured). `confidence`: 0..1 (see module doc), 0.0 when the file
    itself did not match. `matched_segments`: (pattern segment, path
    segment) pairs, outermost first. `missing_segments`: pattern segments
    that found no folder, or whose folder did not match. `notes`:
    human-readable reasons for anything short of a full match."""
    values: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.0
    matched_segments: list[tuple[str, str]] = field(default_factory=list)
    missing_segments: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    file_matched: bool = False

    @property
    def matched(self) -> bool:
        """True when the file-stem segment matched (values are usable)."""
        return self.file_matched


def _match_segment(
    segment: str,
    text: str,
    valid_field_keys: set[str],
    numeric_fields: set[str],
    isbn_like_fields: set[str],
    field_patterns: Mapping[str, str] | None,
    optional_groups: bool,
    loose_whitespace_fallback: bool,
) -> tuple[dict[str, str], bool] | None:
    """Match one pattern segment against one path segment. Returns
    (captures, used_loose_whitespace) or None."""
    text = (text or "").strip()
    kwargs = dict(field_patterns=field_patterns, optional_groups=optional_groups)
    for loose in (False, True):
        if loose and not loose_whitespace_fallback:
            break
        rx = build_parser_regex(
            segment, valid_field_keys, numeric_fields, isbn_like_fields,
            whitespace_optional=loose, **kwargs,
        )
        if not rx.groupindex:
            # A segment with no fields is a fixed folder name ("Books"):
            # folder case is not significant on the platforms we run on.
            rx = re.compile(rx.pattern, rx.flags | re.IGNORECASE)
        m = rx.match(text)
        if m is not None:
            return {k: (v or "").strip() for k, v in m.groupdict().items()}, loose
    return None


def _is_loose_capture(segment: str, valid_field_keys, numeric_fields, isbn_like_fields, field_patterns) -> bool:
    """A segment that is just one unshaped %field% -- matches any name."""
    if not _BARE_FIELD_RE.match(segment):
        return False
    name = segment.strip().strip("%")
    if name not in valid_field_keys:
        return False
    return not (name in (field_patterns or {}) or name in numeric_fields or name in isbn_like_fields)


def parse_path_detailed(
    path: str,
    pattern: str,
    root: str,
    valid_field_keys: set[str],
    numeric_fields: set[str] = frozenset(),
    isbn_like_fields: set[str] = frozenset(),
    strip_leading_zeros_fields: set[str] = frozenset(),
    *,
    field_patterns: Mapping[str, str] | None = None,
    normalizers: Mapping[str, Normalizer] | None = None,
    optional_groups: bool = True,
    loose_whitespace_fallback: bool = True,
    corroborate: Corroborator | None = None,
) -> PathParseResult:
    """Parse `path` against a (path) `pattern`. Never returns None: a
    failure is a result with `matched` False, confidence 0.0 and the
    reasons in `notes`. See the module doc for matching and confidence."""
    result = PathParseResult()
    pattern_segments = split_path_pattern(pattern)
    if not pattern_segments:
        result.notes.append("The pattern is empty.")
        return result
    path_segments = relative_segments(path, root, len(pattern_segments))
    if not path_segments:
        result.notes.append("The path has no file name.")
        return result

    # Every field the pattern mentions starts out empty, as parse_filename
    # reports an absent optional field.
    for segment in pattern_segments:
        for name in re.findall(r"%(\w+)%", segment):
            if name in valid_field_keys:
                result.values.setdefault(name, "")

    n, m = len(pattern_segments), len(path_segments)
    weights = [0.0] * n
    matched_pairs: dict[int, tuple[str, str]] = {}
    for offset in range(min(n, m)):
        p_index, s_index = n - 1 - offset, m - 1 - offset
        segment, text = pattern_segments[p_index], path_segments[s_index]
        is_file = offset == 0
        hit = _match_segment(
            segment, text, valid_field_keys, numeric_fields, isbn_like_fields,
            field_patterns, optional_groups, loose_whitespace_fallback,
        )
        if hit is None:
            where = "file name" if is_file else "folder"
            result.notes.append(f"The {where} \"{text}\" does not match \"{segment}\".")
            if is_file:
                result.missing_segments = pattern_segments[:]  # nothing usable
                return result
            continue
        captures, used_loose_ws = hit
        weight = 1.0
        if not is_file and _is_loose_capture(
            segment, valid_field_keys, numeric_fields, isbn_like_fields, field_patterns
        ):
            weight = LOOSE_SEGMENT_WEIGHT
            result.notes.append(f"\"{text}\" matched \"{segment}\" only as a whole-folder capture.")
        if used_loose_ws:
            weight *= LOOSE_WHITESPACE_FACTOR
        if corroborate is not None and not is_file:
            if any(v and corroborate(k, v) >= 2 for k, v in captures.items()):
                weight = min(1.0, weight + CORROBORATION_BONUS)
        weights[p_index] = weight
        matched_pairs[p_index] = (segment, text)
        for key, value in captures.items():
            # The segment nearer the file wins when a field appears twice.
            if value and not result.values.get(key):
                result.values[key] = value

    result.file_matched = True
    result.matched_segments = [matched_pairs[i] for i in sorted(matched_pairs)]
    result.missing_segments = [pattern_segments[i] for i in range(n) if i not in matched_pairs]
    if m < n:
        result.notes.append(
            f"The path has only {m} segment(s) for a {n}-segment pattern; "
            f"{n - m} outer segment(s) left empty."
        )
    result.confidence = round(sum(weights) / n, 4)

    for key in strip_leading_zeros_fields:
        if result.values.get(key):
            result.values[key] = strip_leading_zeros(result.values[key])
    for key, normalize in (normalizers or {}).items():
        if result.values.get(key):
            result.values[key] = normalize(result.values[key])
    return result


def parse_path(
    path: str,
    pattern: str,
    root: str,
    valid_field_keys: set[str],
    numeric_fields: set[str] = frozenset(),
    isbn_like_fields: set[str] = frozenset(),
    strip_leading_zeros_fields: set[str] = frozenset(),
    **kwargs,
) -> dict[str, str] | None:
    """The field values only (see parse_path_detailed for confidence and
    the matched segments): None when the file-stem segment does not match,
    otherwise a dict as parse_filename() returns. Extra keyword arguments
    pass to parse_path_detailed()."""
    result = parse_path_detailed(
        path, pattern, root, valid_field_keys, numeric_fields, isbn_like_fields,
        strip_leading_zeros_fields, **kwargs,
    )
    return dict(result.values) if result.matched else None


def folder_value_counts(results: list[PathParseResult]) -> dict[tuple[str, str], int]:
    """(field, normalized value) -> how many results captured it; build a
    `corroborate` callable from a first parsing pass over the batch:
    `lambda f, v: counts.get((f, normalize_field_value(v)), 0)`."""
    counts: dict[tuple[str, str], int] = {}
    for r in results:
        for key, value in r.values.items():
            if value:
                k = (key, normalize_field_value(value))
                counts[k] = counts.get(k, 0) + 1
    return counts

"""
redactor_common/core/search_replace.py

Pure search/replace logic, no GUI dependencies. Supports plain-text or
regex search, case sensitivity toggle, and (for regex mode) backreferences
in the replacement text (\\1, \\2, ...).
"""

from __future__ import annotations

import re


class SearchReplaceError(Exception):
    """Raised for an invalid search pattern (e.g. malformed regex)."""


def compile_pattern(search: str, use_regex: bool, case_sensitive: bool) -> re.Pattern:
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern_text = search if use_regex else re.escape(search)
    try:
        return re.compile(pattern_text, flags)
    except re.error as exc:
        raise SearchReplaceError(f"Invalid search pattern: {exc}") from exc


def apply_replace(
    value: str,
    search: str,
    replace: str,
    use_regex: bool = False,
    case_sensitive: bool = False,
) -> str:
    """Return `value` with every match of `search` replaced by `replace`.
    Returns `value` unchanged if `search` is empty or doesn't match.
    Raises SearchReplaceError for an invalid regex."""
    if not search:
        return value
    pattern = compile_pattern(search, use_regex, case_sensitive)
    replacement = replace if use_regex else replace.replace("\\", "\\\\")
    try:
        return pattern.sub(replacement, value)
    except re.error as exc:
        raise SearchReplaceError(f"Invalid replacement: {exc}") from exc


def would_change(
    value: str,
    search: str,
    replace: str,
    use_regex: bool = False,
    case_sensitive: bool = False,
) -> bool:
    """True if applying the replace would actually change this value."""
    try:
        return apply_replace(value, search, replace, use_regex, case_sensitive) != value
    except SearchReplaceError:
        return False

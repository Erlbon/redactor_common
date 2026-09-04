"""
redactor_common/core/case_conversion.py

Case-conversion transforms (mp3tag's "Case Conversion" feature), applied
to whichever fields/books the user selects in the dialog. Pure string
logic, no GUI dependencies.
"""

from __future__ import annotations

# Small English "connector" words conventionally left lowercase in title
# case, except when they're the first or last word.
_MINOR_WORDS = {
    "a", "an", "and", "as", "at", "but", "by", "for", "in", "nor", "of",
    "on", "or", "so", "the", "to", "up", "yet",
}


def to_upper(text: str) -> str:
    return text.upper()


def to_lower(text: str) -> str:
    return text.lower()


def to_title_case(text: str) -> str:
    """Capitalizes each word, leaving minor connector words lowercase
    unless they're the first or last word -- matches how book titles are
    conventionally capitalized. Uses word.capitalize() rather than
    str.title(), since str.title() mangles apostrophes (turns "don't"
    into "Don'T")."""
    words = text.split(" ")
    last_index = len(words) - 1
    result = []
    for i, word in enumerate(words):
        if not word:
            result.append(word)
            continue
        if 0 < i < last_index and word.lower() in _MINOR_WORDS:
            result.append(word.lower())
        else:
            result.append(word[:1].upper() + word[1:].lower())
    return " ".join(result)


def to_sentence_case(text: str) -> str:
    """Capitalizes only the first letter of the first word, lowercases
    the rest. Preserves leading whitespace exactly."""
    stripped = text.lstrip()
    if not stripped:
        return text
    prefix_len = len(text) - len(stripped)
    return text[:prefix_len] + stripped[0].upper() + stripped[1:].lower()


CASE_CONVERSIONS: dict[str, callable] = {
    "UPPERCASE": to_upper,
    "lowercase": to_lower,
    "Title Case": to_title_case,
    "Sentence case": to_sentence_case,
}


def apply_case_conversion(text: str, mode: str) -> str:
    """Apply a named conversion (a key of CASE_CONVERSIONS). Returns the
    text unchanged if the mode isn't recognized, rather than raising --
    this is always driven by a fixed dropdown in the GUI, so an unknown
    mode should never actually happen, but a silent no-op is a safer
    failure than a crash."""
    fn = CASE_CONVERSIONS.get(mode)
    return fn(text) if fn else text

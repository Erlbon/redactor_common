"""
redactor_common/core/case_conversion.py

Case-conversion transforms (mp3tag's "Case Conversion" feature), applied
to whichever fields/books the user selects in the dialog. Pure string
logic, no GUI dependencies.
"""

from __future__ import annotations

# Small English "connector" words conventionally left lowercase in title
# case, except as the first or last word, or right after a colon/dash
# (the union of epub's and video's original lists).
_MINOR_WORDS = {
    "a", "an", "and", "as", "at", "but", "by", "for", "if", "in", "into",
    "nor", "of", "off", "on", "onto", "or", "so", "the", "to", "up",
    "via", "vs", "yet",
}

# A word ending in one of these starts a new clause, so the next word
# capitalizes even if it's a minor word: "Star Wars: A New Hope".
_CLAUSE_ENDINGS = (":", "-", "–", "—")


def to_upper(text: str) -> str:
    return text.upper()


def to_lower(text: str) -> str:
    return text.lower()


def _capitalize_first_letter(word: str) -> str:
    """Capitalizes the first ALPHABETIC character and lowercases the
    rest: "(the" -> "(The", "don't" -> "Don't" (not str.title()'s
    "Don'T"). "O'Brien" -> "O'brien" is an accepted tradeoff, same as
    mp3tag's own case conversion."""
    for i, ch in enumerate(word):
        if ch.isalpha():
            return word[:i] + ch.upper() + word[i + 1:].lower()
    return word  # no letters at all ("123", "--")


def to_title_case(text: str) -> str:
    """Capitalizes each word, leaving minor connector words lowercase
    unless they're the first or last word, or follow a colon/dash --
    "the lord of the rings" -> "The Lord of the Rings", "star wars: a
    new hope" -> "Star Wars: A New Hope". Consecutive spaces are kept.
    Merged from video's text_transforms (clause boundaries, first-letter
    capitalization) into epub's original."""
    words = text.split(" ")
    last_index = len(words) - 1
    result = []
    force_capitalize_next = False
    for i, word in enumerate(words):
        if not word:
            result.append(word)
            continue
        is_boundary = i == 0 or i == last_index or force_capitalize_next
        if not is_boundary and word.lower() in _MINOR_WORDS:
            result.append(word.lower())
        else:
            result.append(_capitalize_first_letter(word))
        force_capitalize_next = word.endswith(_CLAUSE_ENDINGS)
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
    fn = CASE_CONVERSIONS.get(mode) or CASE_CONVERSIONS.get(_MODE_ALIASES.get(mode, ""))
    return fn(text) if fn else text


# video's original short mode keys, accepted too.
_MODE_ALIASES = {
    "upper": "UPPERCASE",
    "lower": "lowercase",
    "title": "Title Case",
    "sentence": "Sentence case",
}

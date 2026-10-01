"""
redactor_common/core/isbn_norm.py

Generic ISBN handling for any app or dump recipe: strip the punctuation
people and catalogues put in ISBNs, validate the ISBN-10 / ISBN-13
checksums (including the "X" check digit), and convert between the two.
No app imports (epubredactor has its own core/isbn.py; this one is the
shared, tested version).

    normalize_isbn("0-306-40615-2")  -> "9780306406157"   (canonical ISBN-13)
    normalize_isbn("080442957x")     -> "9780804429573"
    normalize_isbn("1234567890")     -> None              (bad checksum)
    isbn_variants("9780306406157")   -> ["9780306406157", "0306406152"]
"""

from __future__ import annotations

import re
from typing import Optional

_STRIP = re.compile(r"[\s\-‐-―]")
_PREFIX = re.compile(r"^(?:isbn(?:-?1[03])?\s*:?)\s*", re.IGNORECASE)


def clean_isbn(text: Optional[str]) -> str:
    """Upper-case, with a leading "ISBN"/"ISBN-13:" label, hyphens and
    whitespace removed. Doesn't validate."""
    value = _PREFIX.sub("", (text or "").strip())
    return _STRIP.sub("", value).upper()


def _digits(value: str) -> bool:
    return value.isascii() and value.isdigit()


def isbn10_check_digit(first9: str) -> str:
    total = sum((10 - i) * int(c) for i, c in enumerate(first9))
    check = (11 - total % 11) % 11
    return "X" if check == 10 else str(check)


def isbn13_check_digit(first12: str) -> str:
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(first12))
    return str((10 - total % 10) % 10)


def is_valid_isbn10(text: Optional[str]) -> bool:
    value = clean_isbn(text)
    return len(value) == 10 and _digits(value[:9]) and isbn10_check_digit(value[:9]) == value[9]


def is_valid_isbn13(text: Optional[str]) -> bool:
    value = clean_isbn(text)
    return len(value) == 13 and _digits(value) and isbn13_check_digit(value[:12]) == value[12]


def isbn10_to_13(text: Optional[str]) -> Optional[str]:
    """The 978-prefixed ISBN-13 for a valid ISBN-10, else None."""
    if not is_valid_isbn10(text):
        return None
    base = "978" + clean_isbn(text)[:9]
    return base + isbn13_check_digit(base)


def isbn13_to_10(text: Optional[str]) -> Optional[str]:
    """The ISBN-10 for a valid 978-prefixed ISBN-13; None for anything else
    (979 numbers have no ISBN-10 form)."""
    value = clean_isbn(text)
    if not is_valid_isbn13(value) or not value.startswith("978"):
        return None
    base = value[3:12]
    return base + isbn10_check_digit(base)


def normalize_isbn(text: Optional[str], strict: bool = True) -> Optional[str]:
    """The canonical form -- 13 digits, no punctuation -- or None.
    `strict` (default): the checksum must be right. `strict=False` accepts
    a well-formed ISBN-10/13 with a wrong checksum (real catalogues have
    typos), returning it cleaned but unconverted."""
    value = clean_isbn(text)
    if is_valid_isbn13(value):
        return value
    if is_valid_isbn10(value):
        return isbn10_to_13(value)
    if not strict:
        if len(value) == 13 and _digits(value):
            return value
        if len(value) == 10 and _digits(value[:9]) and (_digits(value[9]) or value[9] == "X"):
            return value
    return None


def isbn_variants(text: Optional[str]) -> list[str]:
    """Every valid form of one ISBN -- [isbn13, isbn10] (the second only
    when it exists); [] when invalid. For looking a book up in a source
    that stores either form."""
    isbn13 = normalize_isbn(text)
    if not isbn13:
        return []
    isbn10 = isbn13_to_10(isbn13)
    return [isbn13] + ([isbn10] if isbn10 else [])

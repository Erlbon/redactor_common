"""
redactor_common/core/auto_number.py

Pure logic backing Auto-Numbering, promoted verbatim from video's
core/text_transforms.py (already fully generic -- no VideoFile
dependency at all). No PyQt6 dependency, unit-tested.
"""

from __future__ import annotations


def generate_auto_number(index: int, start: int, increment: int, padding: int) -> str:
    """The i-th (0-indexed) number in an auto-numbering sequence,
    zero-padded to `padding` digits (padding=0 means no padding).
    generate_auto_number(0, start=1, increment=1, padding=2) -> "01"
    generate_auto_number(2, start=5, increment=10, padding=0) -> "25"
    """
    value = start + index * increment
    if padding <= 0:
        return str(value)
    # Negative numbers: zfill still pads correctly (Python's str.zfill
    # keeps the sign character and pads the digits after it), e.g.
    # str(-5).zfill(3) == "-05", not "0-5" -- confirmed by test.
    return str(value).zfill(padding)


def apply_auto_number_to_text_field(current_value: str, number_str: str, separator: str) -> str:
    """For a TEXT field (Title, Track Title, etc.): prefix the current
    value with the formatted number and separator -- "Pilot" + "01" +
    " - " -> "01 - Pilot". An empty current_value just gives the
    number alone (no dangling separator with nothing after it).
    """
    if not current_value:
        return number_str
    return f"{number_str}{separator}{current_value}"

"""
redactor_common/cli/values.py

Checks the apps' `set` commands share, so a bad value is refused up front (exit code 2, nothing touched) instead
of crashing a writer half way through a batch.
"""

from __future__ import annotations

import re

from redactor_common.cli import CliError

# C0 controls other than tab, line feed and carriage return are illegal in XML 1.0 (ComicInfo.xml, an EPUB's
# OPF, Matroska tags) and make a writer raise; the two non-characters U+FFFE / U+FFFF and lone surrogates
# are illegal too. Tab/LF/CR are fine in a multi-line text such as a description.
_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff\ud800-\udfff]")
_CONTROL = re.compile("[\x00-\x1f\x7f\ufffe\uffff\ud800-\udfff]")


def check_text(field: str, value: str, multiline: bool = False) -> str:
    """The value stripped, or a CliError naming the field when it holds characters no file format can store. A
    single-line field also refuses line breaks and tabs."""
    pattern = _ILLEGAL if multiline else _CONTROL
    match = pattern.search(value)
    if match:
        shown = f"U+{ord(match.group()):04X}"
        raise CliError(f"{field} cannot contain the control character {shown}")
    return value.strip()


def is_ascii_number(value: str) -> bool:
    """True for a string of ASCII digits only. str.isdigit() also accepts "\u00b2" and Arabic-Indic digits, which int()
    then rejects or silently converts."""
    return value.isascii() and value.isdigit()

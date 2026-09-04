"""
Bumps both of redactor_common's version markers together -- core/version.py's
REDACTOR_COMMON_VERSION ("YYYY-MM-DD#NN", the human-readable form shown
in each consuming project's About dialog) and pyproject.toml's `version`
(the PEP 440 form pip actually reads, "YYYY.M.D.NN" -- dots, no "#",
no leading zeros on month/day/counter).

Same date/counter logic as every consuming project's own
bump_version.py: if today's date already appears in
REDACTOR_COMMON_VERSION, increments the #NN counter; if the date has
changed, resets to #01 for the new date.

Run this, then tag and push -- see README.md's "Releasing a new
version" section for the full steps each consuming project's
requirements.txt pin depends on.
"""

import re
from datetime import date
from pathlib import Path

VERSION_FILE = Path(__file__).parent / "core" / "version.py"
PYPROJECT_FILE = Path(__file__).parent / "pyproject.toml"
VERSION_PATTERN = re.compile(r'REDACTOR_COMMON_VERSION = "(\d{4}-\d{2}-\d{2})#(\d+)"')
PYPROJECT_VERSION_PATTERN = re.compile(r'(?m)^version = "[^"]+"$')


def bump() -> str:
    text = VERSION_FILE.read_text(encoding="utf-8")
    match = VERSION_PATTERN.search(text)
    if not match:
        raise SystemExit(f"Could not find REDACTOR_COMMON_VERSION in {VERSION_FILE}")

    old_date, old_counter = match.group(1), int(match.group(2))
    today = date.today()
    today_str = today.isoformat()

    if old_date == today_str:
        new_counter = old_counter + 1
    else:
        new_counter = 1

    new_version = f'REDACTOR_COMMON_VERSION = "{today_str}#{new_counter:02d}"'
    new_text = VERSION_PATTERN.sub(new_version, text, count=1)
    VERSION_FILE.write_text(new_text, encoding="utf-8")

    pep440 = f"{today.year}.{today.month}.{today.day}.{new_counter}"
    pyproject_text = PYPROJECT_FILE.read_text(encoding="utf-8")
    if not PYPROJECT_VERSION_PATTERN.search(pyproject_text):
        raise SystemExit(f'Could not find a `version = "..."` line in {PYPROJECT_FILE}')
    pyproject_text = PYPROJECT_VERSION_PATTERN.sub(f'version = "{pep440}"', pyproject_text, count=1)
    PYPROJECT_FILE.write_text(pyproject_text, encoding="utf-8")

    return f"{today_str}#{new_counter:02d}"


if __name__ == "__main__":
    print(f"Version bumped to {bump()} (pyproject.toml kept in lockstep)")

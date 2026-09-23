"""
Bumps both of redactor_common's version markers together -- core/version.py's
REDACTOR_COMMON_VERSION ("YYYY-MM-DD#NN", the human-readable form shown
in each consuming project's About dialog) and pyproject.toml's `version`
(the PEP 440 form pip actually reads, "YYYY.M.D.NN" -- dots, no "#",
no leading zeros on month/day/counter).

The date/counter rule itself lives in core/version_bump.py, shared with
every consuming project's own bump_version.py.

Run this, then tag and push -- see README.md's "Releasing a new
version" section for the full steps each consuming project's
requirements.txt pin depends on.
"""

import re
from pathlib import Path

from core.version_bump import bump_version_file, pep440_from

VERSION_FILE = Path(__file__).parent / "core" / "version.py"
PYPROJECT_FILE = Path(__file__).parent / "pyproject.toml"
PYPROJECT_VERSION_PATTERN = re.compile(r'(?m)^version = "[^"]+"$')


def bump() -> str:
    pyproject_text = PYPROJECT_FILE.read_text(encoding="utf-8")
    if not PYPROJECT_VERSION_PATTERN.search(pyproject_text):
        raise SystemExit(f'Could not find a `version = "..."` line in {PYPROJECT_FILE}')

    _old, new_version = bump_version_file(VERSION_FILE, "REDACTOR_COMMON_VERSION")

    pyproject_text = PYPROJECT_VERSION_PATTERN.sub(
        f'version = "{pep440_from(new_version)}"', pyproject_text, count=1
    )
    PYPROJECT_FILE.write_text(pyproject_text, encoding="utf-8")
    return new_version


if __name__ == "__main__":
    print(f"Version bumped to {bump()} (pyproject.toml kept in lockstep)")

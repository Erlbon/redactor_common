"""
redactor_common/core/version_bump.py

The "YYYY-MM-DD#NN" bump every Redactor repo's bump_version.py
implements (five copies before this one, all the same regex and
same-day/new-day counter rule): if the stored date is today, increment
the counter; otherwise reset to #01 under today's date. The date always
comes from the system clock, never from anyone's memory of what day it
is -- the failure mode that made epub build this in the first place.

Each project's bump_version.py stays as a few-line wrapper naming its
own version file (so `python bump_version.py` keeps working unchanged).
Pure logic, no Qt dependency.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path


def next_version(stored_date: str, stored_counter: int, today: datetime.date | None = None) -> str:
    """The version string that follows (stored_date, stored_counter)."""
    today_str = (today or datetime.date.today()).isoformat()
    counter = stored_counter + 1 if stored_date == today_str else 1
    return f"{today_str}#{counter:02d}"


def bump_version_file(
    version_file: str | Path,
    var_name: str = "APP_VERSION",
    today: datetime.date | None = None,
) -> tuple[str, str]:
    """Rewrites `var_name = "YYYY-MM-DD#NN"` in `version_file` to its
    next version. Returns (old_version, new_version). Raises SystemExit
    with a clear message if no such line exists."""
    path = Path(version_file)
    pattern = re.compile(rf'{re.escape(var_name)} = "(\d{{4}}-\d{{2}}-\d{{2}})#(\d+)"')
    text = path.read_text(encoding="utf-8")
    match = pattern.search(text)
    if not match:
        raise SystemExit(
            f'ERROR: no {var_name} line matching the "YYYY-MM-DD#NN" format found in {path}'
        )
    stored_date, stored_counter = match.group(1), int(match.group(2))
    old_version = f"{stored_date}#{stored_counter:02d}"
    new_version = next_version(stored_date, stored_counter, today)
    path.write_text(pattern.sub(f'{var_name} = "{new_version}"', text, count=1), encoding="utf-8")
    return old_version, new_version


def pep440_from(version: str) -> str:
    """"2026-09-04#10" -> "2026.9.4.10" (pyproject.toml's form)."""
    date_part, counter = version.split("#")
    year, month, day = (int(x) for x in date_part.split("-"))
    return f"{year}.{month}.{day}.{int(counter)}"


def main(version_file: str | Path, var_name: str = "APP_VERSION") -> str:
    """Entry point for a project's bump_version.py: bumps and prints."""
    old_version, new_version = bump_version_file(version_file, var_name)
    print(f"{var_name} bumped: {old_version} -> {new_version}")
    return new_version

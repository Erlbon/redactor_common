"""
redactor_common/core/version.py

Version marker for redactor_common itself, distinct from (and tracked
separately from) each consuming project's own APP_VERSION. Bump this
whenever redactor_common's code changes, following the same
"YYYY-MM-DD#NN" convention each project's own bump_version.py already
uses. Keep in lockstep with pyproject.toml's `version` field (that one
has to be PEP 440 -- dots, not "#" -- so it's a same-day dotted twin of
this string, e.g. "2026-09-04#10" here <-> "2026.9.4.10" there) -- see
bump_version.py, which updates both together.

Each project now installs this package via pip (see README.md) rather
than vendoring a copy -- this is the one place that version lives.
"""

from __future__ import annotations

REDACTOR_COMMON_VERSION = "2026-09-04#10"
REDACTOR_COMMON_REPO_URL = "https://github.com/Erlbon/redactor_common"

"""
redactor_common/core/version.py

Version marker for redactor_common itself, distinct from (and tracked
separately from) each consuming project's own APP_VERSION. Bump this
whenever redactor_common's code changes, following the same
"YYYY-MM-DD#NN" convention each project's own bump_version.py already
uses.

Each project can surface this in its own About/Changelog dialog (e.g.
"redactor_common 2026-09-04#04") -- useful for spotting at a glance
that one project is running an older vendored copy than another after
a git subtree pull was missed somewhere.
"""

from __future__ import annotations

REDACTOR_COMMON_VERSION = "2026-09-04#04"

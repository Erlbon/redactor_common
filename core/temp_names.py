"""
redactor_common/core/temp_names.py

Recognising the temp and backup files the Redactor apps leave beside a
user's files, so a folder scan (core/folder_discovery's `skip`) never
lists them: an app killed mid-repair or mid-Redact would otherwise show
its leftovers as books, songs or videos in the next scan. Promoted
2026-10-14 from videoredactor's core/temp_names.py; mp3redactor's
REDACT_SCRATCH_PREFIX check is covered by the family marker below.

Matching is deliberately narrow (exact marker positions, never a bare
substring), so a user's own file such as "My.Partial.Cut.mkv" is still
listed. The family's markers, and who writes them:

- ``<stem>.redact-orig<ext>`` / ``<stem>.redact-orig<N><ext>``
                              core/pipeline.commit_in_place backup (all four apps)
- ``.<stem>.redact-<8 hex><ext>``
                              videoredactor's Redact scratch copy (hidden)
- ``.<app>redactor-<anything>``
                              hidden scratch files made with that prefix, e.g.
                              mp3redactor's ".mp3redactor-redact-*.mp3",
                              ".mp3redactor-*.tmp", ".videoredactor-*.tmp"
- ``.<8 hex>.part``           core/move_plan's verified cross-volume copy
- ``<stem>.repairing<ext>`` / ``<stem>.partial<ext>``
                              videoredactor's repair and remux/convert output
- ``<stem>.redact-part<ext>`` / ``<stem>.redact-resize<ext>``
                              videoredactor's conversion in progress. The marker is deliberately NOT the bare
                              words "part" / "resize": a real file called "Kill.Bill.Part.mkv" must stay visible.

The marker is the LAST dot-segment before the extension. An app adds its
own with register(); matching is case-insensitive on every platform
(Windows semantics). Qt-free.
"""

from __future__ import annotations

import re
from typing import Callable, Iterable, Pattern, Union

_FLAGS = re.IGNORECASE

FAMILY_PATTERNS: tuple[Pattern[str], ...] = (
    re.compile(r"^.+\.redact-orig\d*\.[^.]+$", _FLAGS),
    re.compile(r"^\..+\.redact-[0-9a-f]{8}\.[^.]+$", _FLAGS),
    re.compile(r"^\.(?:mp3|epub|video|cbz)redactor-.+$", _FLAGS),
    re.compile(r"^\.[0-9a-f]{8}\.part$", _FLAGS),
    re.compile(r"^.+\.(?:repairing|partial)\.[^.]+$", _FLAGS),
    re.compile(r"^.+\.redact-(?:part|resize)\.[^.]+$", _FLAGS),
)

PatternLike = Union[str, Pattern[str], Callable[[str], bool]]

_registered: list[Callable[[str], bool]] = []
_registered_keys: set[object] = set()


def register(patterns: PatternLike | Iterable[PatternLike]) -> None:
    """Adds an app's own temp-name patterns to what is_app_temp_name() recognises, for the life of the
    process. Each is a regular expression string (matched against the whole bare file name, ignoring case),
    a compiled pattern (used as compiled, with fullmatch), or a callable name -> bool. Registering the same
    pattern again does nothing, so an app can call this from module import without guarding it.

        register(r"\\.[^.]+\\.tmp_(?:write|pages|clean|resize)")   # an app's own suffixes"""
    if isinstance(patterns, (str, re.Pattern)) or callable(patterns):
        patterns = [patterns]  # type: ignore[list-item]
    for pattern in patterns:  # type: ignore[union-attr]
        if pattern in _registered_keys:
            continue
        if isinstance(pattern, str):
            compiled = re.compile(pattern, _FLAGS)
            test: Callable[[str], bool] = lambda name, c=compiled: c.fullmatch(name) is not None
        elif isinstance(pattern, re.Pattern):
            test = lambda name, c=pattern: c.fullmatch(name) is not None
        elif callable(pattern):
            test = pattern
        else:
            raise TypeError(f"not a pattern: {pattern!r}")
        _registered_keys.add(pattern)
        _registered.append(test)


def is_app_temp_name(file_name: str) -> bool:
    """True if `file_name` (a bare name, no folder) is one of the family's temp/backup names (see the module
    docstring) or matches a pattern an app has register()ed."""
    name = str(file_name)
    return any(p.match(name) for p in FAMILY_PATTERNS) or any(test(name) for test in _registered)

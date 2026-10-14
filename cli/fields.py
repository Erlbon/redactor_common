"""
redactor_common/cli/fields.py

Field names on the command line: any spelling the user is likely to type
("albumartist", "Album Artist", "album-artist", "Series #") finds the
field, an unknown one is a CliError listing the valid names, and
"FIELD=VALUE" is split and checked before anything is written. Promoted
from the four apps' *cli/fields.py, whose resolve_field()/
parse_assignment() were the same code over different field tables; what
stays in each app is its table and its check_value() (the value rules are
app data: track numbers, ISBNs, ComicInfo enums...).

    RESOLVER = FieldResolver(
        spellings={"artist": "artist", "album artist": "albumartist", ...},
        names=FIELD_KEYS,
        check=check_value,
    )
    RESOLVER.resolve("Album-Artist")         -> "albumartist"
    RESOLVER.parse_assignment("artist=Queen") -> ("artist", "Queen")

Lookup is case-insensitive and tries, in order: the text as typed
(stripped, lower case), then with spaces and hyphens as underscores, then
with underscores removed -- so "Album Artist", "album_artist",
"album-artist" and "albumartist" all reach a field whose table has any one
of those spellings. spelling_variants() builds the usual set from a key
and its label for a table that wants them all spelled out.

Pure logic, no Qt.
"""

from __future__ import annotations

from typing import Callable, Collection, Iterable, Mapping, Optional, Sequence

from redactor_common.cli import CliError


def _variants(text: str) -> list[str]:
    """The forms of what the user typed that are looked up, in order."""
    typed = text.strip().lower()
    underscored = typed.replace(" ", "_").replace("-", "_")
    squeezed = underscored.replace("_", "")
    seen: list[str] = []
    for form in (typed, underscored, squeezed):
        if form and form not in seen:
            seen.append(form)
    return seen


def spelling_variants(*names: str) -> set[str]:
    """Every usual spelling of a key and/or label, lower case: as given,
    without spaces, with spaces as "_" or "-", and with "_" removed or
    turned into "-". ("Album Artist" -> albumartist, album_artist,
    album-artist, "album artist")."""
    result: set[str] = set()
    for name in names:
        if not name:
            continue
        lower = name.strip().lower()
        result.update({
            lower,
            lower.replace(" ", ""),
            lower.replace(" ", "_"),
            lower.replace(" ", "-"),
            lower.replace("_", ""),
            lower.replace("_", "-"),
        })
    return result


class FieldResolver:
    """Resolves a typed field name to the app's field key.

    `spellings`: typed spelling -> field key (case-insensitive; keys are
    lower-cased here). Every key's own name should be in it.
    `names`: the valid names listed in the "unknown field" error, in the
    order to show them.
    `check`: optional check_value(key, value) -> stored value (raises
    CliError for a value the field won't take); parse_assignment() runs it.
    `settable`: optional set of the keys `set` may change (cbz: some
    ComicInfo fields are read-only). resolve(text, settable=True) and
    parse_assignment() refuse the others. `settable_names`: the names to
    list in that error (default `names`; the display names can't be
    inferred from keys, so pass them when `settable` is used).
    """

    def __init__(
        self,
        spellings: Mapping[str, str],
        names: Sequence[str],
        check: Optional[Callable[[str, str], str]] = None,
        settable: Optional[Collection[str]] = None,
        settable_names: Optional[Sequence[str]] = None,
    ):
        self._lookup: dict[str, str] = {}
        for spelling, key in spellings.items():
            if spelling:
                self._lookup[spelling.strip().lower()] = key
        self.names = list(names)
        self._check = check
        self.settable = None if settable is None else frozenset(settable)
        self.settable_names = list(settable_names) if settable_names is not None else self.names

    def __contains__(self, text: object) -> bool:
        return isinstance(text, str) and self.find(text) is not None

    @property
    def keys(self) -> set[str]:
        """Every field key a spelling leads to."""
        return set(self._lookup.values())

    def add(self, spellings: Iterable[str], key: str) -> None:
        """More spellings for `key` (an app's aliases)."""
        for spelling in spellings:
            if spelling:
                self._lookup[spelling.strip().lower()] = key

    def find(self, text: str) -> Optional[str]:
        """The field key for what the user typed, or None."""
        for form in _variants(text or ""):
            key = self._lookup.get(form)
            if key is not None:
                return key
        return None

    def resolve(self, text: str, settable: bool = False) -> str:
        """The field key for what the user typed. Raises CliError listing
        the valid names (only the settable ones when `settable` is True
        and the resolver has a settable set)."""
        key = self.find(text)
        restricted = settable and self.settable is not None
        if key is None or (restricted and key not in self.settable):
            valid = self.settable_names if restricted else self.names
            raise CliError(f"unknown field {text!r}. Fields: {', '.join(valid)}")
        return key

    def check_value(self, key: str, value: str) -> str:
        """`value` as the field will store it: the app's check, or the
        value stripped when there is no check."""
        if self._check is None:
            return value.strip()
        return self._check(key, value)

    def parse_assignment(self, text: str) -> tuple[str, str]:
        """"artist=Queen" -> (key, checked value). Splits on the first "="
        (a value may contain "="); the field must be settable."""
        if "=" not in text:
            raise CliError(f"expected FIELD=VALUE, got {text!r}")
        name, _, value = text.partition("=")
        key = self.resolve(name, settable=True)
        return key, self.check_value(key, value)

"""
redactor_common/core/preferences.py

The Qt-free half of the shared Preferences dialog (gui/preferences_dialog.py):
declarative setting specs, sections, the storage-agnostic backend protocol,
tolerant value coercion, and builders for the STANDARD sections -- the settings
that mean the same thing in more than one app (filename habits, default
language).

Nothing here knows where an app keeps its settings. An app implements
PreferencesBackend over its own storage (QSettings, an ini via configparser, a
dataclass saved as ini...) and the dialog only ever calls `get(key)` and
`set_many({...})`. Raw values may come back as anything the storage produces
(QSettings returns "true"/"2" strings from an ini); coerce() turns them into
the spec's type and falls back to the spec's default when they are unusable, so
a hand-edited settings file never crashes the dialog.

This module is independent of core/pipeline.py on purpose: PrefSpec borrows the
OptionSpec idea (declarative kind/default/range, coerce with fallback) but adds
what a settings dialog needs (help text, dependencies, restart notes).

Shared key names are the KEY_* constants below, so an app's Export/Import
adapter and its backend can map them to its own storage without guessing.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable

from redactor_common.core import languages

KINDS = ("bool", "int", "float", "choice", "str", "path", "language")

# --- shared keys -----------------------------------------------------------
# Used by the standard sections below; an app's backend maps each one it
# includes to its own storage.
KEY_ASCII_FILENAMES = "ascii_filenames"
KEY_ZERO_PAD_NUMBERS = "zero_pad_numbers"
KEY_ZERO_PAD_WIDTH = "zero_pad_width"
KEY_AUTO_NUMBER_PADDING = "auto_number_padding"
KEY_BLANK_LANGUAGE_ENABLED = "blank_language_enabled"
KEY_DEFAULT_LANGUAGE = "default_language"

SECTION_FILENAMES = "filenames"
SECTION_LANGUAGE = "language"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class PrefSpec:
    """One setting, declared generically so the dialog can build a control for it.

    kind: "bool" (checkbox), "int" / "float" (spin box, `minimum`/`maximum`),
    "choice" (combo box; `choices` = plain values or (value, label) pairs),
    "str" (one line of text), "path" (text + Browse; `path_mode` "file" or
    "folder") or "language" (editable combo of (code, name) `choices`; a typed
    code that isn't listed is kept as typed).

    `help` is the plain-language sentence shown under the control.
    `depends_on` is the key of a bool spec in the same dialog; this control is
    greyed while that one is off. `restart_note` is appended to the help when
    the setting only applies after a restart.
    """

    key: str
    label: str
    kind: str = "bool"
    default: Any = False
    help: str = ""
    minimum: float | None = None
    maximum: float | None = None
    choices: Sequence[Any] | None = None
    depends_on: str | None = None
    restart_note: str = ""
    path_mode: str = "file"

    def choice_pairs(self) -> list[tuple[str, str]]:
        """`choices` normalised to (value, label) pairs."""
        pairs = []
        for item in self.choices or ():
            if isinstance(item, (tuple, list)) and len(item) == 2:
                pairs.append((str(item[0]), str(item[1])))
            else:
                pairs.append((str(item), str(item)))
        return pairs

    def choice_values(self) -> list[str]:
        return [value for value, _label in self.choice_pairs()]


@dataclass(frozen=True)
class PrefSection:
    """A titled group of specs: one tab/page of the dialog."""

    key: str
    title: str
    specs: tuple[PrefSpec, ...]
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "specs", tuple(self.specs))


@runtime_checkable
class PreferencesBackend(Protocol):
    """Where an app keeps its settings, as the dialog sees it."""

    def get(self, key: str) -> object:
        """The stored value for `key` (raw is fine: it is coerced), or the
        default / None when nothing is stored."""
        ...

    def set_many(self, values: dict[str, object]) -> None:
        """Store these (already coerced) values. Called with only the keys the
        user changed, once per OK/Apply, so a backend can save in one go."""
        ...


class MemoryBackend:
    """A dict-backed backend: for tests, and as the simplest reference."""

    def __init__(self, initial: Mapping[str, object] | None = None) -> None:
        self.values: dict[str, object] = dict(initial or {})
        self.writes: list[dict[str, object]] = []  # each set_many call, for tests

    def get(self, key: str) -> object:
        return self.values.get(key)

    def set_many(self, values: dict[str, object]) -> None:
        self.writes.append(dict(values))
        self.values.update(values)


class CallbackBackend:
    """Adapts two callables (an app's own load/save functions) to the protocol:
    `getter(key) -> raw`, `setter(values: dict) -> None`."""

    def __init__(self, getter, setter) -> None:
        self._getter = getter
        self._setter = setter

    def get(self, key: str) -> object:
        return self._getter(key)

    def set_many(self, values: dict[str, object]) -> None:
        self._setter(values)


# --- coercion --------------------------------------------------------------


def _number(raw: Any) -> float | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except ValueError:
            return None
    else:
        return None
    return value if math.isfinite(value) else None


def coerce(spec: PrefSpec, raw: Any) -> Any:
    """`raw` as the spec's type, or the spec's default when it is missing,
    the wrong type or out of range. Never raises."""
    default = spec.default
    if raw is None:
        return default
    kind = spec.kind
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, int) and raw in (0, 1):
            return bool(raw)
        if isinstance(raw, str):
            text = raw.strip().lower()
            if text in _TRUE:
                return True
            if text in _FALSE:
                return False
        return default
    if kind in ("int", "float"):
        value = _number(raw)
        if value is None:
            return default
        if kind == "int":
            if value != int(value):
                return default
            value = int(value)
        if spec.minimum is not None and value < spec.minimum:
            return default
        if spec.maximum is not None and value > spec.maximum:
            return default
        return value
    if kind == "choice":
        text = str(raw).strip() if isinstance(raw, (str, int, float)) and not isinstance(raw, bool) else ""
        return text if text in spec.choice_values() else default
    if kind in ("str", "path", "language"):
        if not isinstance(raw, str):
            return default
        text = raw if kind == "str" else raw.strip()
        if "\n" in text or "\r" in text or "\x00" in text:
            return default
        if kind == "language" and not text:
            return default
        return text
    return default


def defaults(sections: Iterable[PrefSection]) -> dict[str, Any]:
    """key -> default for every spec of every section."""
    return {spec.key: spec.default for section in sections for spec in section.specs}


def validate_sections(sections: Iterable[PrefSection]) -> None:
    """Raises ValueError for a coding mistake: duplicate key, unknown kind,
    choice without choices, min > max, or depends_on that isn't a bool spec."""
    seen: dict[str, PrefSpec] = {}
    for section in sections:
        for spec in section.specs:
            if spec.key in seen:
                raise ValueError(f"duplicate preference key {spec.key!r}")
            if spec.kind not in KINDS:
                raise ValueError(f"{spec.key}: unknown kind {spec.kind!r}")
            if spec.kind == "choice" and not spec.choice_pairs():
                raise ValueError(f"{spec.key}: a choice needs choices")
            if (spec.minimum is not None and spec.maximum is not None
                    and spec.minimum > spec.maximum):
                raise ValueError(f"{spec.key}: minimum is above maximum")
            if spec.kind == "path" and spec.path_mode not in ("file", "folder"):
                raise ValueError(f"{spec.key}: path_mode must be 'file' or 'folder'")
            seen[spec.key] = spec
    for spec in seen.values():
        if spec.depends_on is not None:
            target = seen.get(spec.depends_on)
            if target is None or target.kind != "bool":
                raise ValueError(f"{spec.key}: depends_on {spec.depends_on!r} is not a bool preference")
            if spec.depends_on == spec.key:
                raise ValueError(f"{spec.key}: depends on itself")


# --- standard sections ------------------------------------------------------


def _customise(
    specs: list[PrefSpec],
    overrides: Mapping[str, Mapping[str, Any]] | None,
) -> tuple[PrefSpec, ...]:
    """Applies per-key field overrides ({key: {"default": 3, "help": ...}});
    an unknown key is a typo, so it raises instead of being ignored."""
    overrides = dict(overrides or {})
    out = []
    for spec in specs:
        changes = overrides.pop(spec.key, None)
        out.append(dataclasses.replace(spec, **changes) if changes else spec)
    if overrides:
        raise ValueError(f"overrides for preferences not in this section: {sorted(overrides)}")
    return tuple(out)


def filenames_section(
    *,
    ascii_filenames: bool = True,
    zero_pad: bool = True,
    auto_number: bool = True,
    width_min: int = 1,
    width_max: int = 9,
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
    title: str = "Filenames",
    description: str = "Habits for the names the app builds when it renames, exports or moves files.",
) -> PrefSection:
    """ASCII-safe filenames, zero-pad numbers (+ width) and Auto-Numbering
    padding. The three flags choose which items an app has (an app without
    Auto-Numbering passes auto_number=False). `overrides` changes any field of
    any item, e.g. {KEY_ZERO_PAD_WIDTH: {"default": 3}}.

    These are the "remembered dialog choices": the Rename by Pattern and
    Auto-Numbering dialogs start from them."""
    specs: list[PrefSpec] = []
    if ascii_filenames:
        specs.append(PrefSpec(
            KEY_ASCII_FILENAMES, "ASCII-safe filenames", "bool", False,
            help="Replace accented and special characters in new filenames with plain "
                 "letters, so the files open on any system or device.",
        ))
    if zero_pad:
        specs.append(PrefSpec(
            KEY_ZERO_PAD_NUMBERS, "Zero-pad numbers in filenames", "bool", False,
            help="Write 7 as 07 (or 007) when a number is part of a new filename, so "
                 "the files sort in the right order.",
        ))
        specs.append(PrefSpec(
            KEY_ZERO_PAD_WIDTH, "Zero-pad width (digits)", "int", 2,
            help="How many digits a padded number gets.",
            minimum=width_min, maximum=width_max, depends_on=KEY_ZERO_PAD_NUMBERS,
        ))
    if auto_number:
        specs.append(PrefSpec(
            KEY_AUTO_NUMBER_PADDING, "Auto-Numbering padding (digits)", "int", 2,
            help="How many digits Auto-Numbering uses for the numbers it writes.",
            minimum=width_min, maximum=width_max,
        ))
    return PrefSection(SECTION_FILENAMES, title, _customise(specs, overrides), description)


def language_section(
    *,
    style: languages.CodeStyle = "alpha2",
    codes: Sequence[str] | None = None,
    include_enabled: bool = False,
    default_code: str | None = None,
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
    title: str = "Language",
    description: str = "",
) -> PrefSection:
    """The default language used for files whose language is blank or unknown.

    `style` is the code style the app stores ("alpha2" epub/cbz, "alpha3" mp3,
    "alpha3b" Matroska); the list comes from core/languages.py in that style
    (`codes` narrows it, in the order given). `include_enabled` adds the
    on/off switch epub has for its apply-without-review action."""
    if codes is None:
        codes = [lang.alpha2 or lang.alpha3 for lang in languages.LANGUAGES]
    pairs = languages.language_pairs(list(codes), style)
    if default_code is None:
        default_code = languages.convert("en", style)
    specs: list[PrefSpec] = []
    if include_enabled:
        specs.append(PrefSpec(
            KEY_BLANK_LANGUAGE_ENABLED, "Set blank or unknown languages automatically", "bool", True,
            help="Allow the command that fills in a blank or unknown language with the "
                 "default below. Turn it off if a blank language is a deliberate marker "
                 "in your library.",
        ))
    specs.append(PrefSpec(
        KEY_DEFAULT_LANGUAGE, "Default language", "language", default_code,
        help="Used for files whose language is blank or unknown. Pick one from the list "
             "or type a language code.",
        choices=tuple(pairs), depends_on=KEY_BLANK_LANGUAGE_ENABLED if include_enabled else None,
    ))
    return PrefSection(SECTION_LANGUAGE, title, _customise(specs, overrides), description)

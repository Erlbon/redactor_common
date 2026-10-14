"""
redactor_common/gui/qsettings_store.py

The QSettings plumbing epubredactor and cbzredactor each kept in their
own gui/app_settings.py (near byte-level copies): a plain .ini file at an
explicit path (QSettings IniFormat, never the Windows registry, which
did not survive an in-place upgrade), read back through typed accessors
that never raise.

    store = QSettingsStore(lambda: os.path.join(base_dir(), "cbzredactor_settings.ini"))
    store.int_value("resize/max_width", 1440)           # "abc" in the file -> 1440, no crash
    genres = ManagedListStore(store, "genres/custom", "genres/hidden_defaults", COMMON_COMIC_GENRES)
    genres.merged()                               # visible defaults + custom entries
    history = PatternHistoryStore(store, "patterns/history")
    history.record("%series% #%number%")
    store.last_directory()                        # "" when the folder is gone

Why typed accessors: QSettings hands every ini value back as text (or
as a LIST when a hand-typed value has an unquoted comma in it), and
`QSettings.value(key, type=int)` RAISES TypeError on "abc" or "2.5" --
which crashed a dialog on open in cbz before its _int_value(). Every
accessor here goes through core/settings_coerce.py's parse_*() and
falls back to the given default on garbage.

Key names and fallbacks are the caller's: the apps' public
load_x()/save_x() functions become one-line delegations with their
existing keys, so nobody's saved settings move.

The path may be a string or a zero-argument callable, evaluated on each
access (the apps compute it from base_dir(), and tests patch that). A
fresh QSettings is opened per call, like the apps did: QSettings caches
per file within the process, so this costs nothing and every caller
sees the others' writes.

Qt is imported lazily, so the pure last-directory helpers
(resolve_start_directory, directory_for) work without a Qt platform:
mp3redactor keeps its settings in configparser and uses only those.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Iterable, Optional, Sequence, Union

from redactor_common.core import managed_list, pattern_history
from redactor_common.core import settings_coerce as sc

PathSource = Union[str, "os.PathLike[str]", Callable[[], str]]

LAST_DIRECTORY_KEY = "files/last_directory"
INCLUDE_SUBFOLDERS_KEY = "include_subfolders"
ASCII_FILENAMES_KEY = "rename/ascii_only"
ZERO_PAD_ENABLED_KEY = "rename/zero_pad_enabled"
ZERO_PAD_WIDTH_KEY = "rename/zero_pad_width"
REDACT_RECIPE_KEY = "redact/recipe"
HISTORY_ENABLED_KEY = "history/enabled"


# -- last-used directory: pure helpers (no Qt) ----------------------------------------

def resolve_start_directory(last_directory: str) -> str:
    """`last_directory` if it is still a real directory, else "" -- e.g. a
    removable drive since unplugged, or a settings file carried over from
    another machine. "" means "let Qt use its own default" for a file
    dialog's starting folder (mp3 core/settings.py; epub/cbz loaders)."""
    if not isinstance(last_directory, str) or not last_directory:
        return ""
    return last_directory if os.path.isdir(last_directory) else ""


def directory_for(path: str) -> str:
    """The folder to remember after the user picked `path`: the path itself
    when it is a folder (Open Folder), else its parent (Open Files). Does
    not check that the parent exists -- see remember_directory()."""
    return path if os.path.isdir(path) else os.path.dirname(path)


def remembered_directory(path: str) -> str:
    """directory_for(path) when that is an existing folder, else "" (what
    the epub/cbz save_last_directory() would store; "" = store nothing)."""
    if not isinstance(path, str) or not path:
        return ""
    directory = directory_for(path)
    return directory if directory and os.path.isdir(directory) else ""


# -- the store ----------------------------------------------------------------------

class QSettingsStore:
    """Typed, crash-proof access to one ini file. Getters never raise on a
    bad stored value; setters write through QSettings (which writes the
    file atomically on sync / destruction)."""

    def __init__(self, ini_path: PathSource):
        self._path_source = ini_path

    # -- plumbing ------------------------------------------------------------

    @property
    def path(self) -> str:
        source = self._path_source
        return os.fspath(source() if callable(source) else source)

    def settings(self):
        """A QSettings on the ini file (for anything the accessors don't cover)."""
        from PyQt6.QtCore import QSettings

        return QSettings(self.path, QSettings.Format.IniFormat)

    def raw(self, key: str, default: Any = None) -> Any:
        """The stored value as QSettings returns it (text from an ini, or a
        list for an unquoted comma value), `default` when nothing is stored
        (epub's raw_setting())."""
        return self.settings().value(key, default)

    def contains(self, key: str) -> bool:
        return bool(self.settings().contains(key))

    def set(self, key: str, value: Any) -> None:
        self.settings().setValue(key, value)

    def remove(self, key: str, *, sync: bool = False) -> None:
        settings = self.settings()
        settings.remove(key)
        if sync:
            settings.sync()

    def write_many(self, values: dict[str, Any]) -> None:
        """Several keys in one go, then sync (epub's write_settings())."""
        settings = self.settings()
        for key, value in values.items():
            settings.setValue(key, value)
        settings.sync()

    def sync(self) -> None:
        self.settings().sync()

    # -- typed getters / setters ----------------------------------------------

    def text(self, key: str, default: str = "") -> str:
        """The value as text; `default` when missing. A list (QSettings'
        reading of an unquoted "a, b") is joined back with ", "."""
        return sc.raw_text(self.raw(key), default)

    def bool_value(self, key: str, default: bool = False, *, other_is_false: bool = False) -> bool:
        """true/false, 1/0, yes/no, on/off; garbage gives `default` (or False
        with other_is_false -- cbz's history switch). Note: QSettings'
        own type=bool calls any non-empty garbage True."""
        return sc.parse_bool(self.raw(key), default, other_is_false=other_is_false)

    def set_bool(self, key: str, value: Any) -> None:
        self.set(key, bool(value))

    def int_value(self, key: str, default: int, lo: Optional[int] = None, hi: Optional[int] = None) -> int:
        """A whole number; "abc", "2.5", a list, or a value outside lo..hi
        gives `default` (cbz's _int_value; epub's type=int raised)."""
        return sc.parse_int(self.raw(key), default, lo, hi)

    def set_int(self, key: str, value: Any) -> None:
        self.set(key, int(value))

    def float_value(self, key: str, default: float, lo: Optional[float] = None, hi: Optional[float] = None) -> float:
        return sc.parse_float(self.raw(key), default, lo, hi)

    def choice(self, key: str, choices: Iterable[str], default: str) -> str:
        """The stored text when it is one of `choices`, else `default`
        (epub's text overflow mode, cbz's conversion choices)."""
        return sc.parse_choice(self.raw(key), choices, default)

    def set_choice(self, key: str, value: str, choices: Iterable[str]) -> bool:
        """Stores `value` only when it is one of `choices`; True if stored."""
        if value in tuple(choices):
            self.set(key, value)
            return True
        return False

    def json(self, key: str, default: Any = None) -> Any:
        """A JSON value stored as one line of text; `default` when blank/corrupt."""
        return sc.parse_json(self.raw(key), default)

    def set_json(self, key: str, value: Any) -> None:
        self.set(key, json.dumps(value))

    def json_list(self, key: str) -> list:
        """A JSON list; [] when missing, corrupt or not a list (epub adapter's _json_list)."""
        return sc.parse_json_list(self.raw(key))

    def str_list(self, key: str, *, drop_blank: bool = False) -> list[str]:
        """The strings of a JSON list. drop_blank drops whitespace-only ones
        (epub's last session files)."""
        return sc.parse_str_list(self.raw(key), drop_blank=drop_blank)

    def set_str_list(self, key: str, values: Iterable[str]) -> None:
        self.set(key, json.dumps(list(values)))

    def string_set(self, key: str) -> set[str]:
        """A set of non-empty strings from a JSON list (epub's junk cover
        hashes, cbz's hidden columns)."""
        return {v for v in sc.parse_str_list(self.raw(key)) if v}

    def set_string_set(self, key: str, values: Iterable[str]) -> None:
        """Stored sorted, so the file doesn't churn."""
        self.set(key, json.dumps(sorted(values)))

    def int_set(self, key: str, *, minimum: Optional[int] = 0) -> set[int]:
        """A set of ints from a JSON list (epub's index-based hidden columns)."""
        return sc.parse_int_set(self.raw(key), minimum=minimum)

    def set_int_set(self, key: str, values: Iterable[int]) -> None:
        self.set(key, json.dumps(sorted(values)))

    def width_map(
        self,
        key: str,
        *,
        lo: Optional[int] = 1,
        hi: Optional[int] = None,
        int_keys: bool = False,
        numeric_strings: bool = True,
        strict: bool = False,
    ) -> dict:
        """{column: width} from a JSON object; see settings_coerce.parse_width_map.
        epub: defaults (by key) or int_keys=True (old index form); cbz:
        lo=None, strict=True."""
        return sc.parse_width_map(
            self.raw(key), lo=lo, hi=hi, int_keys=int_keys, numeric_strings=numeric_strings, strict=strict
        )

    def set_width_map(self, key: str, widths: dict) -> None:
        """Keys stored as strings (JSON objects need them)."""
        self.set(key, json.dumps({str(k): v for k, v in widths.items()}))

    # -- paths ------------------------------------------------------------------

    def existing_path(self, key: str, kind: str = "dir") -> str:
        """A stored path, or "" when unset or it no longer exists. kind:
        "dir" (epub's Calibre install folder), "file" (epub's Sigil exe),
        "any" (no existence check, the database paths)."""
        path = self.text(key)
        if not path or kind == "any":
            return path
        exists = os.path.isdir(path) if kind == "dir" else os.path.isfile(path)
        return path if exists else ""

    def set_existing_path(self, key: str, path: str, kind: str = "dir") -> bool:
        """Stores `path` only when it exists as `kind`; True if stored
        (epub's save_calibre_install_dir / save_sigil_path)."""
        if not isinstance(path, str) or not path:
            return False
        if kind == "dir" and not os.path.isdir(path):
            return False
        if kind == "file" and not os.path.isfile(path):
            return False
        self.set(key, path)
        return True

    def last_directory(self, key: str = LAST_DIRECTORY_KEY) -> str:
        """The remembered dialog folder, "" when none or it is gone."""
        return resolve_start_directory(self.text(key))

    def remember_directory(self, path: str, key: str = LAST_DIRECTORY_KEY) -> bool:
        """Remembers the folder of `path` (a file or folder just opened) when
        it exists; True if stored."""
        directory = remembered_directory(path)
        if directory:
            self.set(key, directory)
            return True
        return False

    # -- small switches every app has -------------------------------------------

    def include_subfolders(self) -> bool:
        """File > Include Subfolders: ON unless turned off."""
        return self.bool_value(INCLUDE_SUBFOLDERS_KEY, True)

    def set_include_subfolders(self, on: bool) -> None:
        self.set_bool(INCLUDE_SUBFOLDERS_KEY, on)

    def ascii_filenames(self, key: str = ASCII_FILENAMES_KEY) -> bool:
        """Rename by Pattern's ASCII-safe filenames box (off by default)."""
        return self.bool_value(key, False)

    def set_ascii_filenames(self, on: bool, key: str = ASCII_FILENAMES_KEY) -> None:
        self.set_bool(key, on)

    def rename_zero_pad(
        self,
        enabled_key: str = ZERO_PAD_ENABLED_KEY,
        width_key: str = ZERO_PAD_WIDTH_KEY,
        default_width: int = 2,
    ) -> tuple[bool, int]:
        """(zero-pad on?, width) for RenamePatternDialog; off and 2 by default."""
        return self.bool_value(enabled_key, False), self.int_value(width_key, default_width)

    def set_rename_zero_pad(
        self,
        enabled: bool,
        width: int,
        enabled_key: str = ZERO_PAD_ENABLED_KEY,
        width_key: str = ZERO_PAD_WIDTH_KEY,
    ) -> None:
        settings = self.settings()
        settings.setValue(enabled_key, bool(enabled))
        settings.setValue(width_key, int(width))

    def opt_in(self, key: str = HISTORY_ENABLED_KEY) -> bool:
        """An opt-in switch (data logging, history): OFF when missing, and
        OFF for any garbled value -- only a true word turns it on."""
        return self.bool_value(key, False, other_is_false=True)


# -- managed lists (genres, languages) ---------------------------------------------

Defaults = Union[Sequence[Any], Callable[[], Sequence[Any]]]


class ManagedListStore:
    """Built-in defaults (individually hideable, restorable) plus the user's
    custom entries, kept as two JSON lists in the ini. The rules are
    core/managed_list.py; this is the storage both QSettings apps wrote.

    pairs=False: plain names (genres), deduped case-insensitively.
    pairs=True:  (code, name) pairs (languages), deduped by code; hidden
                 defaults are stored as a list of codes.

    `defaults` may be a callable, evaluated on each use (epub imports its
    genre list lazily)."""

    def __init__(self, store: QSettingsStore, custom_key: str, hidden_key: str, defaults: Defaults,
                 *, pairs: bool = False):
        self.store = store
        self.custom_key = custom_key
        self.hidden_key = hidden_key
        self._defaults = defaults
        self.pairs = pairs

    def defaults(self) -> list:
        source = self._defaults
        return list(source() if callable(source) else source)

    # -- hidden defaults -------------------------------------------------------

    def hidden(self) -> list[str]:
        """Hidden default genres, or hidden default language CODES."""
        return managed_list.decode_names(self.store.text(self.hidden_key))

    def save_hidden(self, items: Iterable[str]) -> None:
        self.store.set(self.hidden_key, managed_list.encode_names(list(items)))

    def hide(self, item: str) -> None:
        """Hides a default genre (by name) or language (by code)."""
        add = managed_list.add_code if self.pairs else managed_list.add_name
        self.save_hidden(add(self.hidden(), item))

    def restore_defaults(self) -> None:
        self.save_hidden([])

    def visible_defaults(self) -> list:
        if self.pairs:
            return managed_list.exclude_hidden_codes(self.defaults(), self.hidden())
        return managed_list.exclude_hidden_names(self.defaults(), self.hidden())

    # -- custom entries --------------------------------------------------------

    def custom(self) -> list:
        raw = self.store.text(self.custom_key)
        return managed_list.decode_pairs(raw) if self.pairs else managed_list.decode_names(raw)

    def save_custom(self, items: Iterable) -> None:
        items = list(items)
        encoded = managed_list.encode_pairs(items) if self.pairs else managed_list.encode_names(items)
        self.store.set(self.custom_key, encoded)

    def add_custom(self, item: str, name: str = "") -> None:
        """add_custom(genre) or add_custom(code, name); a pair replaces an
        existing entry with the same code."""
        if self.pairs:
            self.save_custom(managed_list.add_pair(self.custom(), item, name))
        else:
            self.save_custom(managed_list.add_name(self.custom(), item))

    def remove_custom(self, item: str) -> None:
        """By name (genres) or by code (languages)."""
        if self.pairs:
            self.save_custom(managed_list.remove_pair(self.custom(), item))
        else:
            self.save_custom(managed_list.remove_name(self.custom(), item))

    def merged(self) -> list:
        """Visible defaults then custom entries -- what the "+" picker shows."""
        if self.pairs:
            return managed_list.merge_pairs(self.visible_defaults(), self.custom())
        return managed_list.merge_names(self.visible_defaults(), self.custom())


# -- rename / parse pattern history ------------------------------------------------

class PatternHistoryStore:
    """Most-recently-used patterns (core/pattern_history.py) under one ini key.
    Read accepts JSON or the \\x1f-joined form; writes JSON."""

    def __init__(self, store: QSettingsStore, key: str, max_history: int = pattern_history.DEFAULT_MAX_HISTORY):
        self.store = store
        self.key = key
        self.max_history = max_history

    def load(self) -> list[str]:
        return pattern_history.decode_history(self.store.text(self.key))

    def record(self, pattern: str) -> None:
        """The pattern was used: to the front, deduped, trimmed (save_pattern_used())."""
        self.save(pattern_history.dedupe_and_trim(self.load(), pattern, self.max_history))

    def save(self, history: Sequence[str]) -> None:
        """Replaces the whole list, trimmed (Import Settings)."""
        self.store.set(self.key, pattern_history.encode_history(list(history)[:self.max_history]))

    def last(self, default: str = "") -> str:
        history = self.load()
        return history[0] if history else default


# -- table columns by field key ----------------------------------------------------

class ColumnSettingsStore:
    """Column widths / hidden columns / order keyed by FIELD KEY (stable when
    a column is added in code), with epub's one-time migration from its
    old index-based keys: when the new key is missing and a legacy key is
    given, the legacy values are mapped through the caller's column_keys
    and written back in the new form."""

    def __init__(
        self,
        store: QSettingsStore,
        widths_key: str,
        hidden_key: str,
        order_key: Optional[str] = None,
        *,
        legacy_widths_key: Optional[str] = None,
        legacy_hidden_key: Optional[str] = None,
        min_width: Optional[int] = 1,
        strict_widths: bool = False,
    ):
        self.store = store
        self.widths_key = widths_key
        self.hidden_key = hidden_key
        self.order_key = order_key
        self.legacy_widths_key = legacy_widths_key
        self.legacy_hidden_key = legacy_hidden_key
        self.min_width = min_width
        self.strict_widths = strict_widths

    def widths(self, column_keys: Optional[Sequence[str]] = None) -> dict[str, int]:
        """{} when never saved. min_width=1 drops 0/negative (epub);
        cbz used min_width=None, strict_widths=True."""
        if self.store.contains(self.widths_key) or not (self.legacy_widths_key and column_keys):
            return self.store.width_map(self.widths_key, lo=self.min_width, strict=self.strict_widths)
        legacy = self.store.width_map(self.legacy_widths_key, int_keys=True)
        migrated = {column_keys[i]: w for i, w in legacy.items() if 0 <= i < len(column_keys)}
        if migrated:
            self.save_widths(migrated)
        return migrated

    def save_widths(self, widths: dict[str, int]) -> None:
        self.store.set_width_map(self.widths_key, widths)

    def hidden(self, column_keys: Optional[Sequence[str]] = None) -> set[str]:
        if self.store.contains(self.hidden_key) or not (self.legacy_hidden_key and column_keys is not None):
            return self.store.string_set(self.hidden_key)
        migrated = {column_keys[i] for i in self.store.int_set(self.legacy_hidden_key) if 0 <= i < len(column_keys)}
        self.save_hidden(migrated)  # even an empty set: the migration ran (epub)
        return migrated

    def save_hidden(self, hidden: Iterable[str]) -> None:
        self.store.set_string_set(self.hidden_key, hidden)

    def has_hidden_preference(self) -> bool:
        """True once a hidden-columns choice was ever saved, even "show all"
        (cbz: tells "never configured" from a deliberate empty set)."""
        return self.store.contains(self.hidden_key)

    def order(self) -> list[str]:
        return self.store.str_list(self.order_key) if self.order_key else []

    def save_order(self, order: Sequence[str]) -> None:
        if self.order_key:
            self.store.set_str_list(self.order_key, order)

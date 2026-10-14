"""
redactor_common/core/settings_coerce.py

Total validators for settings values, in two families:

* coerce_*  -- a value that came out of an imported settings file (JSON
  types: str, bool, int, list, dict). Written four times before this,
  once per app's settings adapter (epub/cbz gui/settings_adapter.py,
  mp3/video core/settings_adapter.py), each solving the same trap:
  `bool` is a subclass of `int` in Python, so a check like
  `isinstance(value, int)` lets an imported `true` through as 1.
* parse_*   -- a raw value read back from an ini file (QSettings or
  configparser hand back text, or a list for an unquoted value with
  commas in it). These read what the app itself wrote, plus whatever a
  user typed into the file by hand.

Every function is TOTAL: garbage in gives `default` out, never an
exception, so a hand-edited or damaged value can't crash a dialog or the
start-up. The defaults are what the copies returned:

* epub/cbz adapters returned None for "leave the setting alone", which
  is `default`'s default here;
* mp3/video adapters raised ValueError instead -- wrap a call in
  required() for that style:
      width = required(coerce_int(value, 1, 10), "expected a whole number from 1 to 10")

Where the copies differed (blank strings dropped or kept, one bad entry
rejecting the whole list or only itself) the difference is a parameter;
the docstrings name which app used which setting.

Pure logic, no Qt dependency.
"""

from __future__ import annotations

import json
import math
from typing import Any, Callable, Iterable, Optional, TypeVar

T = TypeVar("T")

_TRUE_WORDS = frozenset({"true", "1", "yes", "on"})
_FALSE_WORDS = frozenset({"false", "0", "no", "off"})


def required(value: Optional[T], message: str = "invalid value") -> T:
    """`value`, or ValueError(message) when it is None -- turns a coerce_*()
    result into the raise-on-invalid style of the mp3/video adapters."""
    if value is None:
        raise ValueError(message)
    return value


def is_int(value: Any) -> bool:
    """True for a real int; False for bool (an int subclass) and everything else."""
    return isinstance(value, int) and not isinstance(value, bool)


# -- imported values (JSON types) ---------------------------------------------------

def coerce_bool(value: Any, default: Optional[bool] = None) -> Optional[bool]:
    """`value` when it is a real bool, else `default`. (Every copy: no 1/0,
    no "true" strings -- a JSON file says true or false.)"""
    return value if isinstance(value, bool) else default


def coerce_int(
    value: Any,
    lo: Optional[int] = None,
    hi: Optional[int] = None,
    default: Optional[int] = None,
) -> Optional[int]:
    """`value` when it is an int (never a bool) within lo..hi inclusive
    (None = unbounded), else `default`. cbz: _int(low, high); video:
    _int_codec; mp3: kind "int" with 0..99; epub: zero-pad width 1..10."""
    if not is_int(value):
        return default
    if lo is not None and value < lo:
        return default
    if hi is not None and value > hi:
        return default
    return value


def coerce_float(
    value: Any,
    lo: Optional[float] = None,
    hi: Optional[float] = None,
    default: Optional[float] = None,
) -> Optional[float]:
    """An int or float (never a bool, never NaN/inf) within lo..hi, as float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        return default
    if (lo is not None and number < lo) or (hi is not None and number > hi):
        return default
    return number


def coerce_choice(value: Any, choices: Iterable[Any], default: Any = None) -> Any:
    """`value` when it is one of `choices` (and not a bool posing as 0/1), else `default`."""
    allowed = tuple(choices)
    if isinstance(value, bool) and not any(isinstance(c, bool) for c in allowed):
        return default
    try:
        return value if value in allowed else default
    except TypeError:  # an unhashable/uncomparable value
        return default


def is_single_line(value: str, max_len: Optional[int] = None) -> bool:
    """No line breaks or NULs, and at most `max_len` characters (video's _text_ok)."""
    if max_len is not None and len(value) > max_len:
        return False
    return "\n" not in value and "\r" not in value and "\x00" not in value


def _text_ok(value: str, single_line: bool, max_len: Optional[int]) -> bool:
    if single_line:
        return is_single_line(value, max_len)
    return max_len is None or len(value) <= max_len


def coerce_str(
    value: Any,
    default: Optional[str] = None,
    *,
    single_line: bool = False,
    max_len: Optional[int] = None,
    check: Optional[Callable[[str], bool]] = None,
) -> Optional[str]:
    """`value` when it is a str passing the checks, else `default`.
    single_line/max_len: video's text fields (single line, 2048 chars)."""
    if not isinstance(value, str) or not _text_ok(value, single_line, max_len):
        return default
    if check is not None:
        try:
            if not check(value):
                return default
        except Exception:
            return default
    return value


def coerce_str_list(
    value: Any,
    default: Optional[list[str]] = None,
    *,
    drop_blank: bool = False,
    strip: bool = False,
    dedupe: bool = False,
    forbidden: str = "",
    single_line: bool = False,
    max_len: Optional[int] = None,
    limit: Optional[int] = None,
) -> Optional[list[str]]:
    """A list of strings. A non-list, or a list with any non-string in it,
    gives `default` (every copy rejected those whole). The options then
    clean the items one by one:

    - strip: strip each item first (video, for its unit-separator lists);
    - drop_blank: drop items that are empty after stripping (epub, video);
      cbz and mp3 kept them;
    - forbidden: drop items containing any of these characters (video:
      the list separator it joins with);
    - single_line/max_len: drop items failing is_single_line (video);
    - dedupe: drop repeats, keeping the first (video);
    - limit: keep at most this many (video's pattern history).
    """
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return default
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if strip:
            item = item.strip()
        if drop_blank and not item.strip():
            continue
        if forbidden and any(ch in item for ch in forbidden):
            continue
        if not _text_ok(item, single_line, max_len):
            continue
        if dedupe:
            if item in seen:
                continue
            seen.add(item)
        out.append(item)
    return out[:limit] if limit is not None else out


def coerce_pairs(
    value: Any,
    default: Optional[list] = None,
    *,
    require_nonblank: bool = False,
    as_lists: bool = False,
) -> Optional[list]:
    """[(code, name), ...] from a list of 2-item lists/tuples of strings. Any
    malformed entry gives `default` for the whole value (every copy).
    require_nonblank: both parts must be non-blank (epub). as_lists: return
    [[code, name], ...] (epub's JSON shape) instead of tuples (cbz, mp3)."""
    if not isinstance(value, list):
        return default
    out: list = []
    for item in value:
        if not (isinstance(item, (list, tuple)) and len(item) == 2
                and all(isinstance(part, str) for part in item)):
            return default
        if require_nonblank and not (item[0].strip() and item[1].strip()):
            return default
        out.append([item[0], item[1]] if as_lists else (item[0], item[1]))
    return out


def coerce_width_map(
    value: Any,
    default: Optional[dict[str, int]] = None,
    *,
    lo: int = 1,
    hi: Optional[int] = None,
    strict: bool = False,
    allowed_keys: Optional[Iterable[str]] = None,
) -> Optional[dict[str, int]]:
    """{column key: width} with int widths in lo..hi (None = no upper bound).

    strict=True (cbz: 0 < w < 10000, i.e. lo=1, hi=9999): one bad entry
    gives `default` for the whole map. strict=False (epub: w > 0; video:
    0 < w <= 5000): bad entries are dropped, the rest kept. allowed_keys
    (epub: the table's column keys) drops unknown keys in either mode.
    A non-dict gives `default`."""
    if not isinstance(value, dict):
        return default
    allowed = set(allowed_keys) if allowed_keys is not None else None
    out: dict[str, int] = {}
    for key, width in value.items():
        ok = isinstance(key, str) and coerce_int(width, lo, hi) is not None
        if not ok:
            if strict:
                return default
            continue
        if allowed is not None and key not in allowed:
            continue
        out[key] = width
    return out


# -- raw ini values (text) --------------------------------------------------------

def raw_text(raw: Any, default: str = "") -> str:
    """An ini value as text. None gives `default`; a list (QSettings splits an
    unquoted hand-typed value at its commas) is joined back with ", "."""
    if raw is None:
        return default
    if isinstance(raw, str):
        return raw
    if isinstance(raw, bool):  # QSettings' in-process cache hands back the bool it was given
        return "true" if raw else "false"
    if isinstance(raw, (list, tuple)):
        return ", ".join(str(part) for part in raw)
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def parse_bool(raw: Any, default: bool = False, *, other_is_false: bool = False) -> bool:
    """true/1/yes/on and false/0/no/off (any case, surrounding blanks
    ignored), a real bool as itself. None or an empty value gives `default`
    (video's "" = unset). Any other text gives `default` too, unless
    other_is_false: then only a true word is True and the rest False (cbz's
    history switch, video's raw == "1")."""
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    if is_int(raw):
        return raw != 0
    text = raw_text(raw).strip().lower()
    if not text:
        return default
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS or other_is_false:
        return False
    return default


def parse_int(raw: Any, default: int, lo: Optional[int] = None, hi: Optional[int] = None) -> int:
    """int(text) of an ini value; garbage ("abc", "2.5", a list) or a value
    outside lo..hi gives `default` (cbz _int_value, video _int_codec.read)."""
    if isinstance(raw, bool):
        return default
    if is_int(raw):
        number = raw
    else:
        try:
            number = int(raw_text(raw).strip())
        except (TypeError, ValueError):
            return default
    if (lo is not None and number < lo) or (hi is not None and number > hi):
        return default
    return number


def parse_float(raw: Any, default: float, lo: Optional[float] = None, hi: Optional[float] = None) -> float:
    """float(text) of an ini value; garbage, NaN/inf or out of range gives `default`."""
    if isinstance(raw, bool):
        return default
    try:
        number = float(raw) if isinstance(raw, (int, float)) else float(raw_text(raw).strip())
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    if (lo is not None and number < lo) or (hi is not None and number > hi):
        return default
    return number


def parse_choice(raw: Any, choices: Iterable[str], default: str) -> str:
    """The ini text when it is one of `choices`, else `default` (epub text
    overflow mode, cbz on-load behaviour)."""
    text = raw_text(raw, default)
    return text if text in tuple(choices) else default


def recipe_from_json_value(value: Any) -> Optional[str]:
    """An imported Redact recipe (a JSON object/list in the file, "" for the
    defaults) as the one-line JSON text the apps keep in their ini; None
    when it is neither (cbz's _recipe)."""
    if value == "" and isinstance(value, str):
        return ""
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value)
        except (TypeError, ValueError):
            return None
    return None


def parse_json(raw: Any, default: Any = None) -> Any:
    """json.loads of an ini value, `default` when blank or not JSON."""
    text = raw_text(raw)
    if not text.strip():
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


def parse_json_list(raw: Any) -> list:
    """A JSON list from an ini value; [] when missing, corrupt or not a list."""
    data = parse_json(raw)
    return data if isinstance(data, list) else []


def parse_str_list(raw: Any, *, drop_blank: bool = False) -> list[str]:
    """The strings of a JSON list (non-strings dropped). drop_blank also
    drops empty/whitespace-only ones (epub's last-session files)."""
    return [v for v in parse_json_list(raw) if isinstance(v, str) and (not drop_blank or v.strip())]


def parse_width_map(
    raw: Any,
    *,
    lo: Optional[int] = 1,
    hi: Optional[int] = None,
    int_keys: bool = False,
    numeric_strings: bool = True,
    strict: bool = False,
) -> dict:
    """A {key: width} JSON object read back from an ini value.

    - lo/hi: widths outside are dropped (epub: lo=1; video: lo=1, hi=5000;
      cbz kept any int: lo=None).
    - int_keys: keys parsed as ints, for epub's old index-based widths.
    - numeric_strings: int("120") / int(120.0) accepted (epub, cbz used
      int(v)); False takes real ints only (video).
    - strict: one unparsable entry empties the whole map (cbz's
      all-or-nothing int(v)); otherwise only that entry is dropped (epub).
    """
    data = parse_json(raw)
    if not isinstance(data, dict):
        return {}
    out: dict = {}
    for key, value in data.items():
        if isinstance(value, bool):
            width = None
        elif numeric_strings:
            try:
                width = int(value)
            except (TypeError, ValueError, OverflowError):
                width = None
        else:
            width = value if is_int(value) else None
        if int_keys:
            try:
                key = int(key)
            except (TypeError, ValueError):
                key = None
        elif not isinstance(key, str):
            key = None
        if width is None or key is None:
            if strict:
                return {}
            continue
        if (lo is not None and width < lo) or (hi is not None and width > hi):
            continue
        out[key] = width
    return out


def parse_int_set(raw: Any, *, minimum: Optional[int] = 0) -> set[int]:
    """A set of ints from a JSON list (epub's index-based hidden columns);
    unparsable entries and ones below `minimum` dropped."""
    out: set[int] = set()
    for value in parse_json_list(raw):
        if isinstance(value, bool):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if minimum is None or number >= minimum:
            out.add(number)
    return out

"""
redactor_common/core/settings_bundle.py

Export/Import Settings for the Redactor apps: one JSON file per app
(`<app>-settings.json`) carrying the user's portable preferences (recipes,
rename patterns, column layout, view options, field defaults, lookup
preferences) so a fresh install or a second machine can pick them up.

The module knows nothing about where an app keeps its settings. The app
implements a `SettingsAdapter` (slug, list of sections, read/write a
section as a plain dict) and this module does the rest: building the file,
parsing it defensively, diffing it against the live settings and applying
chosen sections.

Two safety rules are enforced here so no single adapter can break them:

* SECRETS ARE NEVER EXPORTED OR APPLIED. Any key whose name looks like a
  credential (api_key, password, token, secret, pin, bearer ...) is
  dropped on the way out, on the way in and in the diff, even when an
  adapter offers it. Secrets belong to core/secret_store.py; carrying them
  between machines is a separate, later feature.
* MACHINE-SPECIFIC data (tool paths, last-used folders, local database
  paths, window geometry) is a section flagged `portable=False`. It is
  not in the default selection; a UI offers it as an explicit opt-in.

File format (version 1):
    {"format": "redactor-settings", "version": 1, "app": "<slug>",
     "app_version": "...", "exported": "<UTC ISO>",
     "sections": {"<key>": {"label": "...", "items": {"name": value}}}}

Unknown sections and unknown keys in a file are ignored (forward
compatibility); a file from a newer, incompatible format version or for a
different app raises SettingsBundleError with a message fit to show.

Adapter contract: read_section(key) must return every key the section
supports, with its current (or default) value, because that key set is what
an imported file is allowed to touch. write_section(key, values) receives
only known, non-secret keys and should store them as one unit (raise to
signal failure; the other sections are still applied).

Pure logic, no Qt dependency.

Usage:
    from redactor_common.core import settings_bundle as sb

    class MyAdapter(sb.SettingsAdapter):
        app_slug = "cbzredactor"
        def sections(self): return [sb.SectionSpec("columns", "Columns", True)]
        def read_section(self, key): ...
        def write_section(self, key, values): ...

    text = sb.dump_bundle(sb.build_bundle(adapter, {"columns"}))
    bundle = sb.parse_bundle(text, "cbzredactor")
    changes = sb.diff_bundle(adapter, bundle)
    result = sb.apply_bundle(adapter, bundle, {"columns"})
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

BUNDLE_FORMAT = "redactor-settings"
BUNDLE_VERSION = 1

# Substrings that mark a key as a credential wherever they appear; "pin" and
# "key" pairs are matched as whole words instead so "pinned" / "sort_key"
# are not caught.
_SECRET_SUBSTRINGS = ("password", "passwd", "secret", "token", "apikey", "bearer", "credential")
_SECRET_WORDS = {"pin"}


class SettingsBundleError(Exception):
    """Raised with a user-presentable message when a settings file can't be used."""


def looks_secret(name: str) -> bool:
    """True when a setting's key name looks like an API key, password, token,
    PIN or similar credential. Deliberately conservative: a false positive
    only costs one preference not travelling between machines."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(name))
    lowered = spaced.lower()
    words = [w for w in re.split(r"[^a-z0-9]+", lowered) if w]
    if any(w in _SECRET_WORDS for w in words):
        return True
    for a, b in zip(words, words[1:]):
        if (a, b) in (("api", "key"), ("private", "key"), ("access", "key")):
            return True
    flat = "".join(words)
    return any(s in flat for s in _SECRET_SUBSTRINGS)


@dataclass(frozen=True)
class SectionSpec:
    """One exportable group of settings. `portable=False` marks machine-specific
    data (paths, folders, geometry) that is opt-in on export and import."""
    key: str
    label: str
    portable: bool = True


class SettingsAdapter:
    """Base class an app subclasses. Only app_slug, sections(), read_section()
    and write_section() are required; `app_version` and `redetect_tools`
    are optional."""

    app_slug: str = ""
    app_version: str = ""
    # Set to a bound method/callable in a subclass to offer "Re-detect tools"
    # after an import (instead of copying another machine's tool paths).
    redetect_tools = None

    def sections(self) -> list[SectionSpec]:
        raise NotImplementedError

    def read_section(self, key: str) -> dict[str, Any]:
        raise NotImplementedError

    def write_section(self, key: str, values: dict[str, Any]) -> None:
        raise NotImplementedError


@dataclass
class BundleSection:
    label: str
    items: dict[str, Any] = field(default_factory=dict)


@dataclass
class Bundle:
    app: str
    app_version: str = ""
    exported: str = ""
    sections: dict[str, BundleSection] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "format": BUNDLE_FORMAT,
            "version": BUNDLE_VERSION,
            "app": self.app,
            "app_version": self.app_version,
            "exported": self.exported,
            "sections": {k: {"label": s.label, "items": s.items} for k, s in self.sections.items()},
        }


@dataclass(frozen=True)
class Change:
    section: str
    key: str
    old: Any
    new: Any


@dataclass
class ApplyResult:
    applied: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)  # section -> error text


def _json_safe(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


def _clean_items(items: dict) -> dict[str, Any]:
    """Drops secret-looking and non-JSON-able entries."""
    return {
        str(k): v for k, v in items.items()
        if not looks_secret(str(k)) and _json_safe(v)
    }


def default_selection(adapter: SettingsAdapter) -> set[str]:
    """Section keys a UI should tick by default: the portable ones."""
    return {s.key for s in adapter.sections() if s.portable}


def build_bundle(adapter: SettingsAdapter, include: Iterable[str]) -> Bundle:
    """Reads the chosen sections from the adapter. Sections the adapter doesn't
    declare are skipped; a section whose read fails is skipped too, so one
    broken section can't block the whole export."""
    wanted = set(include)
    bundle = Bundle(
        app=adapter.app_slug,
        app_version=str(getattr(adapter, "app_version", "") or ""),
        exported=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    )
    for spec in adapter.sections():
        if spec.key not in wanted:
            continue
        try:
            raw = adapter.read_section(spec.key)
        except Exception:
            continue
        bundle.sections[spec.key] = BundleSection(spec.label, _clean_items(dict(raw or {})))
    return bundle


def dump_bundle(bundle: Bundle) -> str:
    return json.dumps(bundle.to_dict(), indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def write_text_atomic(path: str | Path, text: str) -> None:
    """Temp file in the same folder, then os.replace, so a crash or full disk
    never leaves half a settings file behind."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def parse_bundle(text: str, expected_app: str) -> Bundle:
    """Parses a settings file. Raises SettingsBundleError for anything that is
    not a usable file for `expected_app`; tolerates junk inside sections."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        raise SettingsBundleError("This file is not a settings file (it isn't valid JSON).") from None
    if not isinstance(data, dict) or data.get("format") != BUNDLE_FORMAT:
        raise SettingsBundleError("This file is not a Redactor settings file.")
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise SettingsBundleError("This settings file has no valid format version.")
    if version > BUNDLE_VERSION:
        raise SettingsBundleError(
            f"This settings file uses a newer format (version {version}) than this "
            f"program understands (version {BUNDLE_VERSION}). Update the program and try again."
        )
    app = data.get("app")
    if app != expected_app:
        raise SettingsBundleError(
            f"This settings file is for {app!r}, not {expected_app!r}, so it can't be imported here."
            if isinstance(app, str) and app else
            f"This settings file doesn't say which program it is for, so it can't be imported into {expected_app!r}."
        )
    bundle = Bundle(
        app=app,
        app_version=str(data.get("app_version") or ""),
        exported=str(data.get("exported") or ""),
    )
    sections = data.get("sections")
    if isinstance(sections, dict):
        for key, sec in sections.items():
            if not isinstance(sec, dict) or not isinstance(sec.get("items"), dict):
                continue
            label = sec.get("label")
            bundle.sections[str(key)] = BundleSection(
                label if isinstance(label, str) and label else str(key),
                _clean_items(sec["items"]),
            )
    return bundle


def _importable(adapter: SettingsAdapter, bundle: Bundle, section: str) -> dict[str, Any]:
    """The file's values for `section` that the adapter actually knows about.
    Unknown sections/keys and secret-looking keys are dropped."""
    sec = bundle.sections.get(section)
    if sec is None or section not in {s.key for s in adapter.sections()}:
        return {}
    try:
        current = adapter.read_section(section)
    except Exception:
        return {}
    return {k: v for k, v in sec.items.items()
            if k in current and not looks_secret(k)}


def diff_bundle(adapter: SettingsAdapter, bundle: Bundle) -> list[Change]:
    """Every value in the file that would differ from the live settings."""
    changes: list[Change] = []
    for spec in adapter.sections():
        incoming = _importable(adapter, bundle, spec.key)
        if not incoming:
            continue
        current = adapter.read_section(spec.key)
        for k, new in incoming.items():
            if current.get(k) != new:
                changes.append(Change(spec.key, k, current.get(k), new))
    return changes


def apply_bundle(adapter: SettingsAdapter, bundle: Bundle, sections: Iterable[str]) -> ApplyResult:
    """Writes only the chosen sections, one write_section() call each holding
    only the keys that changed. A section that fails is reported and the
    rest still apply."""
    chosen = set(sections)
    result = ApplyResult()
    for spec in adapter.sections():
        if spec.key not in chosen:
            continue
        incoming = _importable(adapter, bundle, spec.key)
        if not incoming:
            continue
        current = adapter.read_section(spec.key)
        values = {k: v for k, v in incoming.items() if current.get(k) != v}
        if not values:
            continue
        try:
            adapter.write_section(spec.key, values)
        except Exception as exc:
            result.failed[spec.key] = f"{type(exc).__name__}: {exc}"
        else:
            result.applied.append(spec.key)
    return result

"""
redactor_common/core/secret_store.py

One place for the family's API keys and passwords (Comic Vine, GCD,
TMDB/TVDB/OpenSubtitles, Discogs ...), replacing each app's own
cleartext or "scrambled" settings value. Scrambling with a key that ships
in the source is not protection, so this module never does it: a secret
lives either in the OS credential store (via the optional `keyring`
library: Windows Credential Manager, macOS Keychain, Linux Secret
Service) or, only when the caller has explicitly opted in, in a clearly
named UNENCRYPTED file with owner-only permissions.

Resolution order for get_secret(): explicit env var (if the caller names
one) > OS keyring > opt-in fallback file > the caller's `legacy` value.
`keyring` is imported lazily, so this module imports (and degrades to
"unavailable") without it installed.

Where there is no usable keyring backend (headless Linux, keyring's
fail/null backends, any keyring exception) set_secret() raises
SecretStoreUnavailable, which an app's UI catches to ask the user whether
to use the unencrypted fallback (allow_unencrypted_fallback=True, or
set_allow_unencrypted_fallback(True) once for the whole process).

The fallback file sits in the per-user config folder (app_paths.
user_config_dir), not next to a portable exe: a secret must not travel
with a USB-stick copy of the app folder. On POSIX it is created 0600; on
Windows chmod is a no-op, so the user-profile folder's ACL is the only
protection, which is exactly why it is opt-in and named "UNENCRYPTED".

Secret values are never logged, never put in exception messages and never
in a repr; errors carry only the app/secret NAME and the exception type.

Pure logic, no Qt dependency.

The family's keys are SHARED (get_key / set_key / unshared_keys / share_keys, at the end): one entry per service for
every Redactor, edited from any app's API Keys; an app's own old entry is only a fallback while the apps move over.

Usage:
    from redactor_common.core import secret_store as ss

    key = ss.get_secret("cbzredactor", "comicvine_api_key",
                        env_var="COMICVINE_API_KEY")
    try:
        ss.set_secret("cbzredactor", "comicvine_api_key", new_key)
    except ss.SecretStoreUnavailable:
        ...ask the user, then retry with allow_unencrypted_fallback=True
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional, Union

from redactor_common.core import app_paths

SERVICE_PREFIX = "redactor/"
FALLBACK_FILE_TEMPLATE = "redactor_{app}_secrets_UNENCRYPTED.json"
FALLBACK_NOTE = (
    "UNENCRYPTED secrets file. These values are stored in plain text "
    "because no OS credential store was available and the user allowed "
    "this. Delete the file to remove them."
)

SOURCE_KEYRING = "keyring"
SOURCE_ENV = "env"
SOURCE_FILE = "unencrypted-file"
SOURCE_NONE = "none"

_backend_override = None  # an injected keyring-API object (tests, custom stores)
_fallback_dir_override: Optional[Path] = None
_allow_fallback = False


class SecretStoreUnavailable(RuntimeError):
    """No usable secure store, and the unencrypted fallback isn't enabled
    (or the store failed). The message never contains a secret value."""


def service_name(app: str) -> str:
    return SERVICE_PREFIX + app


# -- configuration ---------------------------------------------------------

def set_backend(backend) -> None:
    """Inject an object with keyring's get_password/set_password/
    delete_password(service, name) API, bypassing the `keyring` import
    (tests, or an app with its own store). None restores auto-detection."""
    global _backend_override
    _backend_override = backend


def set_fallback_dir(folder: Optional[Union[str, Path]]) -> None:
    """Override where the opt-in fallback file lives (tests; None resets)."""
    global _fallback_dir_override
    _fallback_dir_override = Path(folder) if folder is not None else None


def set_allow_unencrypted_fallback(enabled: bool) -> None:
    """Process-wide opt-in, for an app that has asked the user once and
    remembered the answer in its settings."""
    global _allow_fallback
    _allow_fallback = bool(enabled)


def allow_unencrypted_fallback_enabled() -> bool:
    return _allow_fallback


# -- keyring backend -------------------------------------------------------

def _get_backend():
    """The keyring-API object to use, or None when there is no secure one."""
    if _backend_override is not None:
        return _backend_override
    try:
        import keyring
        backend = keyring.get_keyring()
    except Exception:
        return None
    # keyring hands back its fail/null backends instead of raising when
    # nothing real exists; both mean "can't store securely".
    module = type(backend).__module__ or ""
    if module.startswith(("keyring.backends.fail", "keyring.backends.null")):
        return None
    return backend


def keyring_available() -> bool:
    """True when a real OS credential store can be used."""
    return _get_backend() is not None


def _keyring_get(app: str, name: str) -> str:
    backend = _get_backend()
    if backend is None:
        return ""
    try:
        return backend.get_password(service_name(app), name) or ""
    except Exception:
        return ""  # a broken/locked keyring reads as "not there"


def _keyring_set(backend, app: str, name: str, value: str) -> None:
    try:
        backend.set_password(service_name(app), name, value)
    except Exception as exc:
        raise SecretStoreUnavailable(
            f"The OS credential store refused to save '{name}' for {app} "
            f"({type(exc).__name__})."
        ) from None


def _keyring_delete(backend, app: str, name: str) -> None:
    try:
        backend.delete_password(service_name(app), name)
    except Exception as exc:
        # "not found" is the normal no-op case and is a PasswordDeleteError;
        # anything else is a real failure worth surfacing.
        if type(exc).__name__ == "PasswordDeleteError":
            return
        raise SecretStoreUnavailable(
            f"The OS credential store refused to delete '{name}' for {app} "
            f"({type(exc).__name__})."
        ) from None


# -- opt-in fallback file --------------------------------------------------

def fallback_path(app: str) -> Path:
    folder = _fallback_dir_override or app_paths.user_config_dir(app)
    return Path(folder) / FALLBACK_FILE_TEMPLATE.format(app=app)


def _read_fallback(app: str) -> dict:
    try:
        data = json.loads(fallback_path(app).read_text(encoding="utf-8"))
        values = data.get("secrets", {}) if isinstance(data, dict) else {}
        return {k: v for k, v in values.items() if isinstance(v, str)}
    except (OSError, ValueError):
        return {}


def _write_fallback(app: str, values: dict) -> None:
    path = fallback_path(app)
    payload = json.dumps({"note": FALLBACK_NOTE, "secrets": values}, indent=2)
    try:
        # A folder that can't be created (a file in the way, no permission)
        # is the same "can't store it" as a failed write, not a raw OSError.
        path.parent.mkdir(parents=True, exist_ok=True)
        # Temp file in the same folder + replace: never a half-written file,
        # and the 0600 mode is set at creation, before any secret is written.
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    except OSError as exc:
        raise SecretStoreUnavailable(
            f"Could not write the fallback secrets file for {app} ({type(exc).__name__})."
        ) from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            if sys.platform != "win32":
                os.fchmod(fh.fileno(), 0o600)
            fh.write(payload)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise SecretStoreUnavailable(
            f"Could not write the fallback secrets file for {app} ({type(exc).__name__})."
        ) from None


# -- public API ------------------------------------------------------------

def _read_stored(app: str, name: str) -> tuple[str, str]:
    """(value, source) from keyring, then the fallback file; env excluded."""
    value = _keyring_get(app, name)
    if value:
        return value, SOURCE_KEYRING
    value = _read_fallback(app).get(name, "")
    if value:
        return value, SOURCE_FILE
    return "", SOURCE_NONE


def _legacy_value(legacy: Union[None, str, Callable[[], str]]) -> str:
    if legacy is None:
        return ""
    try:
        return (legacy() if callable(legacy) else legacy) or ""
    except Exception:
        return ""


def get_secret(app: str, name: str, env_var: Optional[str] = None,
               legacy: Union[None, str, Callable[[], str]] = None) -> str:
    """The secret, or "" if none. Order: env var > keyring > fallback file >
    `legacy` (a value or a callable returning the app's old plaintext
    setting, so an app works before its migration has run; this never
    writes anything -- see migrate_legacy_secret)."""
    if env_var:
        env = os.environ.get(env_var, "").strip()
        if env:
            return env
    value, _ = _read_stored(app, name)
    if value:
        return value
    return _legacy_value(legacy)


def secret_source(app: str, name: str, env_var: Optional[str] = None) -> str:
    """Where get_secret() would read from: 'keyring', 'env',
    'unencrypted-file' or 'none' (legacy values don't count)."""
    if env_var and os.environ.get(env_var, "").strip():
        return SOURCE_ENV
    return _read_stored(app, name)[1]


def set_secret(app: str, name: str, value: str,
               allow_unencrypted_fallback: Optional[bool] = None) -> str:
    """Store a secret; returns where it went ('keyring'/'unencrypted-file').
    An empty value deletes. Raises SecretStoreUnavailable when there is no
    usable keyring and the fallback isn't allowed (argument, else the
    process-wide set_allow_unencrypted_fallback())."""
    if not value:
        delete_secret(app, name)
        return SOURCE_NONE
    allow = _allow_fallback if allow_unencrypted_fallback is None else allow_unencrypted_fallback
    backend = _get_backend()
    if backend is not None:
        try:
            _keyring_set(backend, app, name, value)
        except SecretStoreUnavailable:
            if not allow:
                raise
        else:
            # A stale fallback copy must not outlive the secure one.
            values = _read_fallback(app)
            if name in values:
                del values[name]
                _write_fallback(app, values)
            return SOURCE_KEYRING
    elif not allow:
        raise SecretStoreUnavailable(
            f"No secure credential store is available to save '{name}' for {app}. "
            "Install the 'keyring' package or enable the unencrypted fallback."
        )
    values = _read_fallback(app)
    values[name] = value
    _write_fallback(app, values)
    return SOURCE_FILE


def delete_secret(app: str, name: str) -> None:
    """Remove the secret from the keyring and the fallback file (no-op if
    absent). The env var is the user's, never touched."""
    backend = _get_backend()
    if backend is not None:
        _keyring_delete(backend, app, name)
    values = _read_fallback(app)
    if name in values:
        del values[name]
        _write_fallback(app, values)


# -- the "may I use an unencrypted file?" consent ---------------------------
#
# Every app asked the same question the same way (mp3/cbz gui/secret_prompts,
# videoredactor's API Keys dialog) and re-applied the remembered answer at
# startup in its own settings code. The flow lives here, Qt-free; the
# question itself is redactor_common.gui.secret_prompts.

def bind_fallback_preference(load: Callable[[], object],
                             save: Callable[[bool], object]) -> Callable[[], None]:
    """Startup wiring for the remembered consent. Applies load() now
    (set_allow_unencrypted_fallback(bool(load())); a load that raises
    counts as "no") and returns the `remember` callable to hand to
    save_with_consent()/save_with_fallback_prompt(): it persists a yes via
    save(True) and switches the process-wide opt-in on. Only a yes is ever
    remembered -- a "no" is asked again next time.

        remember = secret_store.bind_fallback_preference(
            lambda: settings.allow_unencrypted_fallback,
            lambda on: (setattr(settings, "allow_unencrypted_fallback", on), save_settings(settings)))
    """
    try:
        allowed = bool(load())
    except Exception:
        allowed = False
    set_allow_unencrypted_fallback(allowed)

    def remember() -> None:
        set_allow_unencrypted_fallback(True)
        save(True)

    return remember


def save_with_consent(
    save: Callable[[Optional[bool]], object],
    ask: Callable[[], bool],
    remember: Optional[Callable[[], None]] = None,
    on_error: Optional[Callable[[SecretStoreUnavailable], None]] = None,
) -> bool:
    """Run a save that writes secrets, asking for the unencrypted fallback
    only when it is needed. Returns True when the save happened.

    1. save(None) -- uses the keyring, or the fallback if already allowed.
    2. On SecretStoreUnavailable: ask(); a no returns False (the caller
       keeps its dialog open with what was typed; nothing is lost).
    3. A yes: remember() (persist the answer; it should also switch the
       process-wide opt-in on, as bind_fallback_preference's does), then
       the process-wide opt-in is set anyway, then save(True).
    4. If that second save fails too (the fallback file can't be written),
       on_error(exc) is told and False is returned -- it never propagates.

    `save` receives the allow_unencrypted_fallback value to pass on to
    set_secret() (None = the process-wide setting; True = allowed). A
    `remember` that raises is reported through on_error like a failed save."""
    try:
        save(None)
        return True
    except SecretStoreUnavailable:
        pass
    if not ask():
        return False
    try:
        if remember is not None:
            remember()
    except Exception as exc:  # the consent could not be stored; still don't crash
        if on_error is not None:
            on_error(SecretStoreUnavailable(f"Could not remember the choice ({type(exc).__name__})."))
        return False
    set_allow_unencrypted_fallback(True)
    try:
        save(True)
    except SecretStoreUnavailable as exc:
        if on_error is not None:
            on_error(exc)
        return False
    return True


def migrate_legacy_secret(app: str, name: str, read_legacy: Callable[[], str],
                          clear_legacy: Callable[[], None]) -> bool:
    """Move an old plaintext/scrambled setting into the store. Reads it via
    read_legacy, stores it, re-reads it from the store, and only if that
    matches calls clear_legacy. Returns True when the secret is safely in
    the store and the legacy copy cleared; False (legacy untouched) when
    there was nothing to migrate, the store is unavailable, or the
    read-back didn't match. Never loses the secret."""
    try:
        value = read_legacy() or ""
    except Exception:
        return False
    if not value:
        return False
    try:
        set_secret(app, name, value)
    except SecretStoreUnavailable:
        return False
    stored, _ = _read_stored(app, name)
    if stored != value:
        return False
    clear_legacy()
    return True


# -- the family's shared keys (2026-11-06, Erlend: one key per service for every Redactor) ----------------------
#
# A key used to live under each app's own name (discogs_token@redactor/mp3redactor AND
# discogs_token@redactor/folderredactor ...): a new key had to be entered in every app that used it. Now one entry
# per service is shared by the family (<name>@redactor/shared), edited from any app's API Keys. An app reads
# env var > the shared entry > its own old entry (kept while the apps move over: an app on an older redactor_common
# still reads and writes its own), and every save goes to the shared entry. These are keys, tokens and the odd
# service password: no Redactor signs in to an account of the person's.

SHARED_APP = "shared"  # service name: redactor/shared


class FamilyKey:
    """One key of the family: its entry name (permanent), what a person calls it, its environment variable, and the apps
    that use it (shown in API Keys: 'used by ...')."""

    __slots__ = ("name", "label", "env_var", "apps")

    def __init__(self, name: str, label: str, env_var: Optional[str], apps: tuple):
        self.name, self.label, self.env_var, self.apps = name, label, env_var, tuple(apps)

    def __repr__(self) -> str:
        return f"FamilyKey({self.name!r})"


# Every key a Redactor keeps. A new key (or a new app using one) is added here.
FAMILY_KEYS: dict[str, FamilyKey] = {k.name: k for k in (
    FamilyKey("discogs_token", "Discogs Token", "DISCOGS_TOKEN", ("mp3redactor", "folderredactor")),
    FamilyKey("tmdb_api_key", "TMDB API Key", "TMDB_API_KEY", ("videoredactor", "folderredactor")),
    FamilyKey("tvdb_api_key", "TheTVDB API Key", "TVDB_API_KEY", ("videoredactor",)),
    FamilyKey("opensubtitles_api_key", "OpenSubtitles API Key", "OPENSUBTITLES_API_KEY", ("videoredactor",)),
    FamilyKey("libretranslate_api_key", "LibreTranslate API Key", "LIBRETRANSLATE_API_KEY", ("videoredactor",)),
    FamilyKey("theporndb_api_key", "ThePornDB API Token", "THEPORNDB_API_KEY", ("videoredactor",)),
    FamilyKey("stashdb_api_key", "StashDB API Key", "STASHDB_API_KEY", ("videoredactor",)),
    FamilyKey("comicvine_api_key", "Comic Vine API Key", "COMICVINE_API_KEY", ("cbzredactor",)),
    FamilyKey("gcd_password", "Grand Comics Database password", None, ("cbzredactor",)),
)}

WHERE_ENV, WHERE_SHARED, WHERE_OWN, WHERE_NONE = "env", "shared", "own", "none"


def users_of(name: str) -> tuple:
    """The apps that use this key ('' tuple for a key the registry does not know)."""
    key = FAMILY_KEYS.get(name)
    return key.apps if key else ()


def get_key(name: str, app: str, env_var: Optional[str] = None,
            legacy: Union[None, str, Callable[[], str]] = None) -> str:
    """The key: env var > the family's shared entry > `app`'s own old entry > `legacy`. "" if none."""
    if env_var:
        env = os.environ.get(env_var, "").strip()
        if env:
            return env
    for owner in (SHARED_APP, app):
        value, _ = _read_stored(owner, name)
        if value:
            return value
    return _legacy_value(legacy)


def key_location(name: str, app: str, env_var: Optional[str] = None) -> tuple[str, str]:
    """(where, source) of what get_key() would return: where is WHERE_ENV / WHERE_SHARED / WHERE_OWN / WHERE_NONE,
    source the SOURCE_* of that entry (keyring or the unencrypted file)."""
    if env_var and os.environ.get(env_var, "").strip():
        return WHERE_ENV, SOURCE_ENV
    for owner, where in ((SHARED_APP, WHERE_SHARED), (app, WHERE_OWN)):
        value, source = _read_stored(owner, name)
        if value:
            return where, source
    return WHERE_NONE, SOURCE_NONE


def set_key(name: str, value: str, allow_unencrypted_fallback: Optional[bool] = None) -> str:
    """Save the key for the whole family (the shared entry); returns where it went. An empty value deletes it there."""
    return set_secret(SHARED_APP, name, value, allow_unencrypted_fallback)


def delete_key(name: str, app: Optional[str] = None) -> None:
    """Remove the shared key, and `app`'s own old entry too (else that copy would quietly come back as the key)."""
    delete_secret(SHARED_APP, name)
    if app:
        delete_secret(app, name)


class UnsharedKey:
    """A key in some app's own entry that the family's shared entry does not have yet. The value is never in the repr."""

    __slots__ = ("name", "label", "app", "value")

    def __init__(self, name: str, label: str, app: str, value: str):
        self.name, self.label, self.app, self.value = name, label, app, value

    def __repr__(self) -> str:
        return f"UnsharedKey({self.name!r}, from {self.app!r})"


def unshared_keys(names: Optional[list[str]] = None) -> list[UnsharedKey]:
    """Keys of the registry (or only `names`) stored under an app's own name but not yet shared: the first app found,
    in the registry's order of apps. Nothing is read from an environment variable: that is the person's, not stored."""
    out = []
    for key in FAMILY_KEYS.values():
        if names is not None and key.name not in names:
            continue
        if _read_stored(SHARED_APP, key.name)[0]:
            continue
        for app in key.apps:
            value, _ = _read_stored(app, key.name)
            if value:
                out.append(UnsharedKey(key.name, key.label, app, value))
                break
    return out


def share_keys(found: list[UnsharedKey], allow_unencrypted_fallback: Optional[bool] = None) -> list[str]:
    """Copies each into the shared entry (the app's own entry stays: an app on an older redactor_common reads it).
    Returns plain-language problems, one per key that could not be shared (never a value)."""
    problems = []
    for key in found:
        try:
            set_key(key.name, key.value, allow_unencrypted_fallback)
        except SecretStoreUnavailable as exc:
            problems.append(f"{key.label}: {exc}")
    return problems

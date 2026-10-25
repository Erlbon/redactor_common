"""
redactor_common/core/selfupdate.py

The Qt-free half of "Help > Check for Updates" (the dialogs are gui/update_controller.py).

Every app publishes one GitHub Release per version: tag `v2026-10-24-01` for APP_VERSION "2026-10-24#01", with
`<slug>.exe` (Windows) and `<slug>-linux-x86_64.tar.gz` attached (see each app's .github/workflows/release.yml). So
an update is:

  1. check:    GET api.github.com/repos/<owner>/<slug>/releases/latest (no token: the repos are public; drafts and
               pre-releases are never "latest") and compare its tag with APP_VERSION;
  2. download: stream the asset to `<exe>.new.part` next to the running exe, hashing as it goes, and refuse it unless
               its size and SHA-256 match what the API lists for that asset (GitHub's `digest` field). An asset with
               no digest is refused too: nothing unverified ever replaces the program;
  3. install:  Windows will not overwrite a running exe but WILL rename one. The running exe becomes `<exe>.old`, the
               new file takes its name, and the next start deletes `<exe>.old` (remove_leftovers()). No helper
               script, no second process waiting for the first to exit;
  4. restart:  start the new exe with PYINSTALLER_RESET_ENVIRONMENT=1 and the _PYI*/_MEIPASS2 variables removed, so a
               one-file build unpacks itself afresh instead of reusing the old process's temp folder.

Only a frozen Windows build replaces itself (can_self_replace()). From source, or on Linux, the check still works and
the dialog links to the release page.

The check is OFF by default (the family rule for anything that phones out or logs): KEY_CHECK_FOR_UPDATES turns on a
once-a-day check at startup. Help > Check for Updates works either way. The few values this needs live in their own
small JSON file (UpdateSettings), not in each app's own settings store, so all four apps share one implementation;
with_update_settings() routes the Preferences key there.

Nothing is sent except the HTTPS GET itself (GitHub sees the address it comes from).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from redactor_common.core import lookup_client
from redactor_common.core.preferences import CallbackBackend, PreferencesBackend, PrefSection, PrefSpec

KEY_CHECK_FOR_UPDATES = "check_for_updates"
SECTION_UPDATES = "updates"
CHECK_INTERVAL = 24 * 3600           # the automatic check runs at most once a day
API_LATEST = "https://api.github.com/repos/{repo}/releases/latest"
USER_AGENT = "redactor-update-check"
MAX_API_BYTES = 2 * 1024 * 1024
MAX_ASSET_BYTES = 1024 * 1024 * 1024
CHUNK = 256 * 1024
DOWNLOAD_TIMEOUT = 30.0
OLD_SUFFIX = ".old"
NEW_SUFFIX = ".new"
PART_SUFFIX = ".new.part"

_REPO_URL = re.compile(r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9][A-Za-z0-9_.-]*?)(?:\.git)?/?")
# "2026-10-24#01", "v2026-10-24-01", "2026-10-24-1"
_VERSION = re.compile(r"v?(\d{4})-(\d{1,2})-(\d{1,2})[#-](\d{1,3})")
_SHA256 = re.compile(r"sha256:([0-9a-fA-F]{64})")


class UpdateError(Exception):
    """A check or download that failed; the message is fit to show the user."""


# --- versions and names ----------------------------------------------------------------------------------------------


def parse_version(text: object) -> tuple[int, int, int, int] | None:
    """'2026-10-24#01' or the tag 'v2026-10-24-01' -> (2026, 10, 24, 1). None for anything else."""
    m = _VERSION.fullmatch(str(text or "").strip())
    return tuple(int(part) for part in m.groups()) if m else None


def display_version(text: str) -> str:
    """A tag as the apps show their version: 'v2026-10-24-01' -> '2026-10-24#01'."""
    parsed = parse_version(text)
    if parsed is None:
        return str(text)
    y, mo, d, n = parsed
    return f"{y:04d}-{mo:02d}-{d:02d}#{n:02d}"


def repo_from_url(url: str) -> str:
    """'https://github.com/Erlbon/cbzredactor' -> 'Erlbon/cbzredactor'. ValueError for anything that is not a GitHub
    repository address (the result is put into an API URL)."""
    m = _REPO_URL.fullmatch(str(url or "").strip())
    if not m:
        raise ValueError(f"not a GitHub repository address: {url!r}")
    return f"{m.group(1)}/{m.group(2)}"


def asset_name(slug: str, platform: str = sys.platform) -> str:
    """The release file for this platform: 'cbzredactor.exe' on Windows, the Linux tarball elsewhere."""
    return f"{slug}.exe" if platform == "win32" else f"{slug}-linux-x86_64.tar.gz"


# --- the check -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Release:
    tag: str
    version: tuple[int, int, int, int]
    notes: str
    page_url: str
    asset_name: str = ""
    asset_url: str = ""
    asset_size: int = 0
    sha256: str = ""            # lower-case hex, "" when GitHub lists no digest

    @property
    def display(self) -> str:
        return display_version(self.tag)

    @property
    def downloadable(self) -> bool:
        """An asset for this platform with a size and a digest to check it against."""
        return bool(self.asset_url and self.asset_size > 0 and self.sha256)


def release_from_json(data: object, slug: str, platform: str = sys.platform) -> Release:
    """The Release a /releases/latest answer describes. UpdateError if it has no tag that reads as a version."""
    data = lookup_client.as_dict(data)
    tag = lookup_client.as_text(data.get("tag_name"))
    version = parse_version(tag)
    if version is None:
        raise UpdateError(f"The newest release has a tag that is not a version number: {tag!r}")
    page = lookup_client.as_text(data.get("html_url"))
    if not page.startswith("https://github.com/"):
        page = ""
    wanted = asset_name(slug, platform)
    for asset in lookup_client.dict_items(data.get("assets")):
        if lookup_client.as_text(asset.get("name")) != wanted:
            continue
        url = lookup_client.as_text(asset.get("browser_download_url"))
        m = _SHA256.fullmatch(lookup_client.as_text(asset.get("digest")))
        return Release(tag, version, lookup_client.as_text(data.get("body")), page, wanted,
                       url if url.startswith("https://github.com/") else "",
                       lookup_client.as_int(asset.get("size")), m.group(1).lower() if m else "")
    return Release(tag, version, lookup_client.as_text(data.get("body")), page)


def fetch_latest(repo: str, slug: str, fetch: lookup_client.FetchFn | None = None,
                 platform: str = sys.platform) -> Release:
    """Asks GitHub for the newest published release of `repo` ('Owner/name'). Raises UpdateError."""
    repo_from_url(f"https://github.com/{repo}")      # validates
    fetch = fetch or lookup_client.make_default_fetch(USER_AGENT, max_bytes=MAX_API_BYTES)
    request = lookup_client.build_request(API_LATEST.format(repo=repo), headers={
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    data = lookup_client.fetch_json(
        request, fetch, error_cls=UpdateError, source_name="GitHub",
        status_messages={404: "GitHub lists no published release for this program yet.",
                         403: "GitHub refused the check (too many checks from this address); try again in an hour."},
    )
    return release_from_json(data, slug, platform)


def is_newer(release: Release, current_version: str) -> bool:
    current = parse_version(current_version)
    return current is None or release.version > current


# --- download and install --------------------------------------------------------------------------------------------


def running_exe() -> Path:
    return Path(sys.executable)


def can_self_replace(exe: Path | None = None, platform: str = sys.platform,
                     frozen: bool | None = None) -> tuple[bool, str]:
    """(True, "") when this program can replace its own file; otherwise (False, why) for the dialog."""
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if not frozen:
        return False, "This copy runs from source; update it with git pull."
    if platform != "win32":
        return False, "Automatic updating is only built for the Windows program; download the new version instead."
    exe = exe or running_exe()
    try:
        fd, probe = tempfile.mkstemp(prefix=".update-probe-", dir=exe.parent)
        os.close(fd)
        os.remove(probe)
    except OSError:
        return False, f"The program's folder cannot be written to ({exe.parent}); download the new version instead."
    return True, ""


def download(release: Release, dest: Path, progress: Callable[[int, int], None] | None = None,
             cancel: threading.Event | None = None, opener=None) -> Path | None:
    """Streams the release's asset to `dest` (via `dest` + '.part') and checks its size and SHA-256. Returns `dest`,
    or None when `cancel` was set (the partial file is removed). Raises UpdateError for a failed or wrong download;
    nothing is left behind then either."""
    if not release.downloadable:
        raise UpdateError("This release has no verifiable download for this system.")
    if release.asset_size > MAX_ASSET_BYTES:
        raise UpdateError("The download is larger than any version of this program could be.")
    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    opener = opener or urllib.request.build_opener(lookup_client.SafeRedirects())
    request = urllib.request.Request(release.asset_url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    done = 0
    ok = False
    try:
        with opener.open(request, timeout=DOWNLOAD_TIMEOUT) as response, open(part, "wb") as out:
            while True:
                if cancel is not None and cancel.is_set():
                    return None
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                done += len(chunk)
                if done > release.asset_size:
                    raise UpdateError("The download is larger than GitHub says it is; it was not used.")
                digest.update(chunk)
                out.write(chunk)
                if progress is not None:
                    progress(done, release.asset_size)
        if done != release.asset_size:
            raise UpdateError(f"The download stopped early ({done:,} of {release.asset_size:,} bytes); try again.")
        if digest.hexdigest() != release.sha256:
            raise UpdateError("The downloaded file does not match the checksum GitHub lists for it; it was not used.")
        os.replace(part, dest)
        ok = True
        return dest
    except UpdateError:
        raise
    except (OSError, ValueError) as exc:
        raise UpdateError(f"Could not download the update: {lookup_client.redact_text(exc)}") from exc
    finally:
        if not ok:
            _remove_quietly(part)


def install(new_file: Path, exe: Path | None = None) -> Path:
    """Puts `new_file` in place of the (running) `exe`: exe -> exe.old, new_file -> exe. If the second step fails,
    the first is undone and UpdateError is raised. Returns the path of the old copy."""
    exe = Path(exe or running_exe())
    old = exe.with_name(exe.name + OLD_SUFFIX)
    _remove_quietly(old)        # left by an earlier update that was never restarted
    try:
        os.replace(exe, old)
    except OSError as exc:
        raise UpdateError(f"Could not move the running program aside: {exc}") from exc
    try:
        os.replace(new_file, exe)
    except OSError as exc:
        try:
            os.replace(old, exe)
        except OSError:
            pass
        raise UpdateError(f"Could not put the new version in place: {exc}") from exc
    return old


def remove_leftovers(exe: Path | None = None, frozen: bool | None = None) -> None:
    """At startup: deletes `<exe>.old` (the copy an update replaced) and any half-finished download. Harmless when
    there is nothing to delete, when running from source, or when a file is still locked (tried again next start)."""
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if not frozen:
        return
    exe = Path(exe or running_exe())
    for suffix in (OLD_SUFFIX, PART_SUFFIX, NEW_SUFFIX):
        _remove_quietly(exe.with_name(exe.name + suffix))


def restart_command(exe: Path | None = None, environ: Mapping[str, str] | None = None) -> tuple[list[str], dict]:
    """(argv, env) to start the newly installed exe. The PyInstaller variables of THIS process are dropped and
    PYINSTALLER_RESET_ENVIRONMENT set, so the new one-file build unpacks into its own temp folder instead of
    trying to reuse ours (which goes away when we exit)."""
    env = {k: v for k, v in (os.environ if environ is None else environ).items()
           if not (k.upper().startswith("_PYI") or k.upper() == "_MEIPASS2")}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return [str(exe or running_exe())], env


def _remove_quietly(path: Path) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# --- settings --------------------------------------------------------------------------------------------------------


class UpdateSettings:
    """The check's own few values in a small JSON file (`<slug>_updates.json` next to the app's settings):
    whether the daily check is on (off by default), when the last one succeeded, and a version the user chose to
    skip. A missing or broken file reads as the defaults; writes are atomic."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def update(self, **values) -> None:
        data = self._load()
        data.update(values)
        self._save(data)

    @property
    def enabled(self) -> bool:
        return self._load().get(KEY_CHECK_FOR_UPDATES) is True

    @property
    def last_check(self) -> float:
        value = self._load().get("last_check")
        return float(value) if isinstance(value, (int, float)) else 0.0

    @property
    def skipped(self) -> str:
        value = self._load().get("skipped")
        return value if isinstance(value, str) else ""

    def due(self, now: float | None = None) -> bool:
        """Is an automatic check wanted now? Only when switched on and the last one is a day old (or in the future,
        after a clock change)."""
        now = time.time() if now is None else now
        last = self.last_check
        return self.enabled and (now - last >= CHECK_INTERVAL or last > now)


def updates_section(title: str = "Updates") -> PrefSection:
    """The Preferences page: one switch for the daily check."""
    return PrefSection(SECTION_UPDATES, title, (
        PrefSpec(KEY_CHECK_FOR_UPDATES, "Check for a new version once a day", "bool", False,
                 help="At startup, at most once a day, ask GitHub whether a newer version has been released, and "
                      "offer it if so. Nothing about you or your files is sent. Help > Check for Updates works "
                      "whether this is on or not."),
    ))


def with_update_settings(backend: PreferencesBackend, settings: UpdateSettings) -> CallbackBackend:
    """Wraps an app's Preferences backend so KEY_CHECK_FOR_UPDATES is read from and saved to `settings`; every
    other key goes to `backend` unchanged."""

    def get(key: str) -> object:
        return settings.enabled if key == KEY_CHECK_FOR_UPDATES else backend.get(key)

    def set_many(values: dict) -> None:
        values = dict(values)
        if KEY_CHECK_FOR_UPDATES in values:
            settings.update(**{KEY_CHECK_FOR_UPDATES: bool(values.pop(KEY_CHECK_FOR_UPDATES))})
        if values:
            backend.set_many(values)

    return CallbackBackend(get, set_many)

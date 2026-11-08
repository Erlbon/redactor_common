"""
redactor_common/core/selfupdate.py

The Qt-free half of "Help > Check for Updates" (the dialogs are gui/update_controller.py), following the update
framework in docs/update-framework.md.

Every app publishes one GitHub Release per version: tag `v2026-10-24-01` for APP_VERSION "2026-10-24#01". Each release
carries `update-manifest.json` (core/update_manifest.py), which names, per channel, the exact file with its size and
SHA-256. An update is:

  1. decide:   GET api.github.com/repos/<owner>/<slug>/releases/latest (or /releases/tags/<tag>; no token: the repos
               are public; drafts and pre-releases are never "latest"), compare its tag with APP_VERSION, read this
               copy's declared channel, fetch the manifest (checked against GitHub's digest for it) and find the
               channel's package among the release's assets, with the same size and digest. The result is a
               Decision: INSTALL (with the file), PAGE (with the reason this copy cannot update itself) or NONE.
               Nothing is guessed: a missing manifest, channel or package is a named reason, never a quiet fallback;
  2. download: stream the package next to where it is used, hashing as it goes, and refuse it unless its size and
               SHA-256 match;
  3. install:  windows-exe: Windows will not overwrite a running exe but WILL rename one. The running exe becomes
               `<exe>.old`, the new file takes its name, and the next start deletes `<exe>.old` (remove_leftovers()).
               windows-installer: the new installer is started with the family's installer contract
               (installer_command(): `/relaunch=yes`) once the app has quit; it replaces the folder and starts the
               program again;
  4. restart:  (exe) start the new exe with PYINSTALLER_RESET_ENVIRONMENT=1 and the _PYI*/_MEIPASS2 variables removed,
               so a one-file build unpacks itself afresh instead of reusing the old process's temp folder.

The check is OFF by default (the family rule for anything that phones out or logs): KEY_CHECK_FOR_UPDATES turns on a
once-a-day check at startup. Help > Check for Updates works either way. The few values this needs live in their own
small JSON file (UpdateSettings), not in each app's own settings store, so all apps share one implementation;
with_update_settings() routes the Preferences key there.

Nothing is sent except the HTTPS GETs themselves (GitHub sees the address they come from).
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
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from redactor_common.core import lookup_client, update_manifest
from redactor_common.core.preferences import CallbackBackend, PreferencesBackend, PrefSection, PrefSpec
from redactor_common.core.update_manifest import (
    LINUX_TARBALL, MANIFEST_NAME, SOURCE, WINDOWS_EXE, WINDOWS_INSTALLER, WINDOWS_ZIP,
)

KEY_CHECK_FOR_UPDATES = "check_for_updates"
SECTION_UPDATES = "updates"
CHECK_INTERVAL = 24 * 3600           # the automatic check runs at most once a day
API_LATEST = "https://api.github.com/repos/{repo}/releases/latest"
API_TAG = "https://api.github.com/repos/{repo}/releases/tags/{tag}"
USER_AGENT = "redactor-update-check"
MAX_API_BYTES = 2 * 1024 * 1024
MAX_ASSET_BYTES = 1024 * 1024 * 1024
CHUNK = 256 * 1024
DOWNLOAD_TIMEOUT = 30.0
OLD_SUFFIX = ".old"
NEW_SUFFIX = ".new"
PART_SUFFIX = ".new.part"

# Decision.action
INSTALL = "install"     # this copy can update itself with Decision.release's file
PAGE = "page"           # it cannot (Decision.reason says why): the release page is the way
NONE = "none"           # nothing newer

_REPO_URL = re.compile(r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9][A-Za-z0-9_.-]*?)(?:\.git)?/?")
# "2026-10-24#01", "v2026-10-24-01", "2026-10-24-1"
_VERSION = re.compile(r"v?(\d{4})-(\d{1,2})-(\d{1,2})[#-](\d{1,3})")
_SHA256 = re.compile(r"sha256:([0-9a-fA-F]{64})")
_TAG = re.compile(r"v\d{4}-\d{2}-\d{2}-\d{2,3}")


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


def tag_for(version: str) -> str:
    """'2026-10-24#01' -> 'v2026-10-24-01'."""
    return "v" + display_version(version).replace("#", "-")


def repo_from_url(url: str) -> str:
    """'https://github.com/Erlbon/cbzredactor' -> 'Erlbon/cbzredactor'. ValueError for anything that is not a GitHub
    repository address (the result is put into an API URL)."""
    m = _REPO_URL.fullmatch(str(url or "").strip())
    if not m:
        raise ValueError(f"not a GitHub repository address: {url!r}")
    return f"{m.group(1)}/{m.group(2)}"


def installer_command(setup: Path, visible: bool = True, relaunch: bool = True) -> list[str]:
    """How a downloaded installer is started for an update, by the family's installer contract
    (installer/update-contract.iss): quietly (`visible`: with Inno's progress window), no reboot, and `/relaunch=yes`
    so it starts the program again when it is done."""
    command = [str(setup), "/SILENT" if visible else "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]
    if relaunch:
        command.append("/relaunch=yes")
    return command


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
    asset_api_url: str = ""     # the same file by GitHub's API, when its github.com address fails

    @property
    def display(self) -> str:
        return display_version(self.tag)

    @property
    def downloadable(self) -> bool:
        """A file with a size and a digest to check it against."""
        return bool(self.asset_url and self.asset_size > 0 and self.sha256)


@dataclass(frozen=True)
class Decision:
    """What this copy does with a release: `action` INSTALL / PAGE / NONE, and for anything but INSTALL the reason in
    words. `release` carries the notes and page, and for INSTALL the verified file to download."""
    action: str
    reason: str
    channel: str
    current: str
    release: Release

    def as_json(self) -> dict:
        """What `--check-update` prints."""
        return {"action": self.action, "reason": self.reason, "channel": self.channel, "current": self.current,
                "release": self.release.tag, "file": self.release.asset_name if self.action == INSTALL else "",
                "size": self.release.asset_size if self.action == INSTALL else 0}

    def details(self, app_name: str) -> str:
        """The "Copy details" text of the update box, for a bug report."""
        return "\n".join((
            f"{app_name} {self.current}",
            f"Channel: {self.channel or '(none: no update-channel file)'}",
            f"Release: {self.release.tag}",
            f"Action: {self.action}",
            f"File: {self.release.asset_name or '-'}",
            f"Reason: {self.reason or '-'}",
            f"Executable: {sys.executable}",
        ))


def release_info(data: object) -> Release:
    """The tag, notes and page of a release API answer. UpdateError if its tag does not read as a version."""
    data = lookup_client.as_dict(data)
    tag = lookup_client.as_text(data.get("tag_name"))
    version = parse_version(tag)
    if version is None:
        raise UpdateError(f"The newest release has a tag that is not a version number: {tag!r}")
    page = lookup_client.as_text(data.get("html_url"))
    if not page.startswith("https://github.com/"):
        page = ""
    return Release(tag, version, lookup_client.as_text(data.get("body")), page)


def _assets(data: dict, repo: str) -> dict[str, dict]:
    """The release's files by name, each with its https://github.com/ URL, its API URL, size and sha256 ("" when
    none)."""
    found = {}
    api_prefix = f"https://api.github.com/repos/{repo}/releases/assets/"
    for asset in lookup_client.dict_items(data.get("assets")):
        url = lookup_client.as_text(asset.get("browser_download_url"))
        api_url = lookup_client.as_text(asset.get("url"))
        m = _SHA256.fullmatch(lookup_client.as_text(asset.get("digest")))
        found[lookup_client.as_text(asset.get("name"))] = {
            "url": url if url.startswith("https://github.com/") else "",
            "api_url": api_url if api_url.startswith(api_prefix) and api_url[len(api_prefix):].isdigit() else "",
            "size": lookup_client.as_int(asset.get("size")), "sha256": m.group(1).lower() if m else ""}
    return found


def api_request(api_url: str) -> urllib.request.Request:
    """The API's way to a release file: it answers with the file itself (by a redirect) for this Accept header."""
    return urllib.request.Request(api_url, headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"})


def _fetch_asset(entry: dict, fetch: lookup_client.FetchFn, what: str) -> bytes:
    """A release file by its github.com address, else by the API (2026-11-08: github.com's release downloads
    answered 504 for an hour while the API served the same files). When both fail, the first failure is told."""
    try:
        return lookup_client.fetch_bytes(entry["url"], fetch, error_cls=UpdateError, what=what)
    except UpdateError:
        if not entry.get("api_url"):
            raise
        first = sys.exc_info()[1]
    try:
        return lookup_client.fetch_bytes(api_request(entry["api_url"]), fetch, error_cls=UpdateError, what=what)
    except UpdateError:
        raise first from None


def fetch_release(repo: str, fetch: lookup_client.FetchFn | None = None, tag: str = "") -> dict:
    """GitHub's answer for the newest published release of `repo` ('Owner/name'), or for `tag` (a pre-release too).
    Raises UpdateError."""
    repo_from_url(f"https://github.com/{repo}")      # validates
    if tag and not _TAG.fullmatch(tag):
        raise UpdateError(f"Not a release tag: {tag!r}")
    fetch = fetch or lookup_client.make_default_fetch(USER_AGENT, max_bytes=MAX_API_BYTES)
    url = API_TAG.format(repo=repo, tag=urllib.parse.quote(tag)) if tag else API_LATEST.format(repo=repo)
    request = lookup_client.build_request(url, headers={
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    missing = f"GitHub lists no release {tag}." if tag else "GitHub lists no published release for this program yet."
    return lookup_client.as_dict(lookup_client.fetch_json(
        request, fetch, error_cls=UpdateError, source_name="GitHub",
        status_messages={404: missing,
                         403: "GitHub refused the check (too many checks from this address); try again in an hour."},
    ))


_CANNOT = {
    WINDOWS_ZIP: "This copy was unpacked from the zip, so it cannot replace itself. Download the new version from the "
                 "release page (or install it with the installer, which then keeps it up to date).",
    LINUX_TARBALL: "The Linux build cannot replace itself yet. Download the new version from the release page.",
    SOURCE: "This copy runs from source; update it with git pull.",
    "": "This copy cannot tell how it was installed (it has no update-channel file), so it does not guess. "
        "Download the new version from the release page.",
}


def decide(repo: str, slug: str, current_version: str, channel: str, fetch: lookup_client.FetchFn | None = None,
           tag: str = "", writable: Callable[[], tuple[bool, str]] | None = None) -> Decision:
    """What this copy (version `current_version`, declared `channel`) does with the newest release of `repo`, or with
    release `tag`. Raises UpdateError only when GitHub cannot be asked; everything else is a Decision with a reason.
    `writable` (windows-exe only) says whether the exe's folder can be written: can_self_replace() by default."""
    fetch = fetch or lookup_client.make_default_fetch(USER_AGENT, max_bytes=MAX_API_BYTES)
    data = fetch_release(repo, fetch, tag)
    release = release_info(data)

    def page(reason: str) -> Decision:
        return Decision(PAGE, reason, channel, current_version, release)

    if not is_newer(release, current_version):
        return Decision(NONE, f"You have the newest version ({current_version}).", channel, current_version, release)
    if channel not in update_manifest.SELF_UPDATING:
        return page(_CANNOT.get(channel, _CANNOT[""]))
    assets = _assets(data, repo)
    entry = assets.get(MANIFEST_NAME)
    if entry is None:
        return page(f"Release {release.display} has no {MANIFEST_NAME}: it was made before this program's update "
                    "framework, so this copy cannot tell which file to install. Use the release page.")
    if not (entry["url"] and entry["size"] > 0 and entry["sha256"]):
        return page(f"GitHub lists no checksum for release {release.display}'s {MANIFEST_NAME}, so it cannot be "
                    "trusted. Use the release page.")
    raw = _fetch_asset(entry, fetch, f"the release's {MANIFEST_NAME}")
    if len(raw) != entry["size"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
        return page(f"Release {release.display}'s {MANIFEST_NAME} does not match the checksum GitHub lists for it; "
                    "it was not used. Use the release page.")
    try:
        manifest = update_manifest.parse(raw)
    except ValueError as exc:
        return page(f"Release {release.display} cannot be installed automatically: {exc}. Use the release page.")
    if manifest.schema > update_manifest.SCHEMA:
        return page(f"Release {release.display} uses a newer update format than this copy knows. Use the release page.")
    if manifest.min_client > update_manifest.CLIENT:
        return page(f"This copy's updater is too old to install release {release.display}. Use the release page.")
    if manifest.app != slug:
        return page(f"Release {release.display}'s manifest is for another program ({manifest.app}). Use the release page.")
    if manifest.tag != release.tag:
        return page(f"Release {release.display}'s manifest describes another release ({manifest.tag}). "
                    "Use the release page.")
    package = manifest.packages.get(channel)
    if package is None:
        return page(f"Release {release.display} has no package for this kind of copy ({channel}). "
                    "Use the release page.")
    held = assets.get(package.file)
    if held is None or not held["url"]:
        return page(f"Release {release.display}'s manifest names {package.file}, which the release does not hold. "
                    "Use the release page.")
    if held["size"] != package.size or held["sha256"] != package.sha256:
        return page(f"{package.file} in release {release.display} does not match its manifest (size or checksum). "
                    "Use the release page.")
    if channel == WINDOWS_EXE:
        can, why = (writable or can_self_replace)()
        if not can:
            return page(why)
    release = Release(release.tag, release.version, release.notes, release.page_url, package.file, held["url"],
                      package.size, package.sha256, held["api_url"])
    return Decision(INSTALL, "", channel, current_version, release)


def is_newer(release: Release, current_version: str) -> bool:
    current = parse_version(current_version)
    return current is None or release.version > current


# --- download and install --------------------------------------------------------------------------------------------


def running_exe() -> Path:
    return Path(sys.executable)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def channel_of(exe: Path | None = None, frozen: bool | None = None) -> str:
    """This copy's declared channel (update_manifest.read_channel), with the one-file build's bundle as fallback."""
    frozen = is_frozen() if frozen is None else frozen
    bundle = getattr(sys, "_MEIPASS", None)
    return update_manifest.read_channel(Path(exe or running_exe()), frozen, Path(bundle) if bundle else None)


def can_self_replace(exe: Path | None = None) -> tuple[bool, str]:
    """(True, "") when the program's folder can be written to (the windows-exe swap needs it); else (False, why)."""
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
    opener = opener or urllib.request.build_opener(lookup_client.SafeRedirects())
    first = urllib.request.Request(release.asset_url, headers={"User-Agent": USER_AGENT})
    try:
        return _download_from(first, release, dest, progress, cancel, opener)
    except _Unreachable as failed:
        if not release.asset_api_url:
            raise failed.error from None
        try:                    # github.com's download failed, not the file: the API's way (2026-11-08)
            return _download_from(api_request(release.asset_api_url), release, dest, progress, cancel, opener)
        except _Unreachable:
            raise failed.error from None


class _Unreachable(Exception):
    """A download that could not be fetched (as opposed to a file that came and was wrong): `error` says how."""

    def __init__(self, error: UpdateError):
        super().__init__(str(error))
        self.error = error


def _download_from(request: urllib.request.Request, release: Release, dest: Path,
                   progress: Callable[[int, int], None] | None, cancel: threading.Event | None, opener) -> Path | None:
    part = dest.with_name(dest.name + ".part")
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
        raise _Unreachable(UpdateError(f"Could not download the update: {lookup_client.redact_text(exc)}")) from exc
    finally:
        if not ok:
            _remove_quietly(part)


def installer_download_path(release: Release) -> Path:
    """Where an installer update is downloaded: its own folder in the temp folder, under the manifest's file name."""
    folder = Path(tempfile.gettempdir()) / "redactor-updates"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / release.asset_name


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
        frozen = is_frozen()
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

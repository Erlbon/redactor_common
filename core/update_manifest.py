"""
redactor_common/core/update_manifest.py

The two facts the update framework (docs/update-framework.md) never guesses:

  * WHAT to download: every release carries ``update-manifest.json``, which names, per channel, the exact file with its
    size and SHA-256. A copy reads it and never builds a file name (2026-11-06: an installed Accipio built
    "accipio-setup-setup.exe", which no release had, and silently offered only the download page).
  * HOW this copy was put on the PC: its channel, declared in a one-line ``update-channel`` file beside the exe
    (written by the build, overwritten by the installer), or bundled inside a one-file build. A frozen copy with
    neither says it cannot tell instead of guessing from what lies next to it.

Qt-free; selfupdate.decide() combines these with GitHub's release answer.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

MANIFEST_NAME = "update-manifest.json"
CHANNEL_FILE = "update-channel"
SCHEMA = 1          # the manifest format this code reads and writes
CLIENT = 1          # this updater's level; a release whose min_client is higher must not be installed by it

WINDOWS_INSTALLER = "windows-installer"     # put there by the app's Inno Setup installer: runs the new installer
WINDOWS_EXE = "windows-exe"                 # the one-file exe: swaps itself
WINDOWS_ZIP = "windows-zip"                 # the folder build from the zip: cannot replace itself
LINUX_TARBALL = "linux-tarball"             # the Linux build: cannot replace itself (yet)
SOURCE = "source"                           # not frozen: git pull
CHANNELS = (WINDOWS_INSTALLER, WINDOWS_EXE, WINDOWS_ZIP, LINUX_TARBALL)
SELF_UPDATING = (WINDOWS_INSTALLER, WINDOWS_EXE)

_SHA256 = re.compile(r"[0-9a-f]{64}")
_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._#+-]*")


@dataclass(frozen=True)
class Package:
    file: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Manifest:
    schema: int
    app: str
    tag: str
    version: str
    min_client: int
    packages: Mapping[str, Package]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(app: str, tag: str, version: str, files: Mapping[str, Path], min_client: int = 1) -> dict:
    """The manifest for a release: `files` maps each channel the release serves to its built file. ValueError for an
    unknown channel or a file that is not there."""
    packages = {}
    for channel, path in files.items():
        if channel not in CHANNELS:
            raise ValueError(f"unknown update channel {channel!r} (known: {', '.join(CHANNELS)})")
        path = Path(path)
        if not path.is_file():
            raise ValueError(f"the {channel} package was not found: {path}")
        packages[channel] = {"file": path.name, "size": path.stat().st_size, "sha256": file_sha256(path)}
    return {"schema": SCHEMA, "app": app, "tag": tag, "version": version, "min_client": int(min_client),
            "packages": packages}


def write(path: Path, manifest: dict) -> Path:
    path = Path(path)
    path.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return path


def parse(data: bytes) -> Manifest:
    """Reads a manifest. ValueError, with a message fit for the update box, for anything malformed. A schema newer than
    SCHEMA is returned as read (its fields may not be understood); the caller says so."""
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("the release's update manifest is not readable") from None
    if not isinstance(raw, dict):
        raise ValueError("the release's update manifest is not readable")
    schema = raw.get("schema")
    if not isinstance(schema, int) or isinstance(schema, bool) or schema < 1:
        raise ValueError("the release's update manifest has no valid schema number")
    min_client = raw.get("min_client", 1)
    if not isinstance(min_client, int) or isinstance(min_client, bool):
        raise ValueError("the release's update manifest has no valid min_client")
    texts = {key: raw.get(key) for key in ("app", "tag", "version")}
    if not all(isinstance(value, str) and value for value in texts.values()):
        raise ValueError("the release's update manifest lacks its app, tag or version")
    packages = {}
    for channel, entry in (raw.get("packages") or {}).items() if isinstance(raw.get("packages"), dict) else ():
        if schema <= SCHEMA and channel not in CHANNELS:
            raise ValueError(f"the release's update manifest names an unknown channel {channel!r}")
        if not isinstance(entry, dict):
            raise ValueError(f"the release's update manifest has an unreadable {channel} entry")
        name, size, digest = entry.get("file"), entry.get("size"), entry.get("sha256")
        if not isinstance(name, str) or not _FILE.fullmatch(name):
            raise ValueError(f"the release's update manifest has a bad file name for {channel}: {name!r}")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ValueError(f"the release's update manifest has no valid size for {channel}")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ValueError(f"the release's update manifest has no valid checksum for {channel}")
        packages[channel] = Package(name, size, digest)
    return Manifest(schema, texts["app"], texts["tag"], texts["version"], min_client, packages)


def write_channel(folder: Path, channel: str) -> Path:
    if channel not in CHANNELS:
        raise ValueError(f"unknown update channel {channel!r}")
    path = Path(folder) / CHANNEL_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(channel + "\n", encoding="utf-8")
    return path


def _channel_in(folder: Path | None) -> str:
    if folder is None:
        return ""
    try:
        text = (Path(folder) / CHANNEL_FILE).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    return text if text in CHANNELS else ""


def read_channel(exe: Path, frozen: bool, bundle: Path | None = None) -> str:
    """How this copy was put here: SOURCE when not frozen; else the channel file beside `exe`, else the one bundled
    in a one-file build (`bundle`: sys._MEIPASS); "" when neither says (or says something unknown)."""
    if not frozen:
        return SOURCE
    return _channel_in(Path(exe).parent) or _channel_in(bundle)

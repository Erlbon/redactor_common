"""
redactor_common/core/app_paths.py

Where an app's persistent, non-bundled files live (settings ini, crash
log, bundled tools/ folder): next to the real executable when frozen,
or the project root when running from source. Deliberately not
sys._MEIPASS for that: a one-file PyInstaller build extracts to a temp
directory recreated on every launch, not a stable place for anything
meant to persist. Bundled DATA assets (icon, README) are the opposite
case and DO resolve via sys._MEIPASS -- see asset_path().

Every project reimplemented the frozen-vs-dev walk itself (epub, mp3
and cbz in core/app_paths.py, video in core/config._app_dir()). This
package is installed in site-packages, so it can't find a project's
root from its own __file__ -- each project passes its own `dev_root`
(typically `Path(__file__).resolve().parent.parent` from its core/).

Pure logic, no Qt dependency, so a crash logger can import it before
QApplication exists.
"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """True when running as a PyInstaller-built executable."""
    return bool(getattr(sys, "frozen", False))


def base_dir(dev_root: str | Path) -> Path:
    """The app's home for writable state: the .exe's folder when frozen,
    `dev_root` (the project root) when running from source."""
    if is_frozen():
        # sys.executable is already absolute for a frozen build; not
        # resolve()d, so a caller comparing against its own folder string
        # (or an exe reached through a symlinked folder) sees that folder.
        return Path(sys.executable).parent
    return Path(dev_root).resolve()


def tools_dir(dev_root: str | Path) -> Path:
    """Where sidecar CLI tools bundled next to a frozen build live
    (base_dir()/tools) -- pass as tool_locator.find_tool(tools_dir=...)."""
    return base_dir(dev_root) / "tools"


def asset_path(relative: str | Path, dev_root: str | Path) -> Path:
    """Resolves a bundled data asset (icon.ico, README.md -- anything
    passed via PyInstaller's datas/--add-data). Those unpack to
    sys._MEIPASS in a frozen build, not next to the .exe."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / relative
    return Path(dev_root).resolve() / relative


def settings_ini_path(app_slug: str, dev_root: str | Path) -> Path:
    """"<app_slug>_settings.ini" in base_dir(). App-prefixed, never a
    bare "settings.ini": two apps sharing a portable folder would
    otherwise read and write each other's settings."""
    return base_dir(dev_root) / f"{app_slug}_settings.ini"


def crash_log_path(app_slug: str, dev_root: str | Path) -> Path:
    """"<app_slug>_crash.log" in base_dir()."""
    return base_dir(dev_root) / f"{app_slug}_crash.log"

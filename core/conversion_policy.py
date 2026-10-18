"""
redactor_common/core/conversion_policy.py

One conversion policy for all four Redactor apps (video, music, books, comics), so "do I convert files that need it,
and what happens to the original" is answered the same way everywhere:

  on_open     what happens when a folder or file that needs converting is opened:
              ASK (a dialog lists the files), AUTO (convert without asking; the report says what was done), NEVER
              (do not convert; the app lists such files as read-only rows or leaves them out, as it always did).
  originals   after a VERIFIED conversion: QUARANTINE (the original is moved to a dated folder with an undo log; the
              folder is the one set, else a folder beside the file), RECYCLE (the Recycle Bin) or KEEP (the original stays
              where it is; only possible when the new file has a different name, otherwise it is quarantined).
  dry_run_first   whether a conversion run starts as a dry run (list what would happen, change nothing); the run's own
              dialog can still change it.

The apps keep their own engines (ffmpeg, Calibre, zip) and their own extra options; this module only holds the shared
choices and how they are stored. Each app passes the callables over its own settings file, and the defaults that keep an
existing user's behaviour (an app that never had a quarantine folder defaults to RECYCLE, for example).

Container fixes (a wrong container) and Join Files always use the quarantine: they write an undo log, which the Recycle
Bin cannot honour.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

SECTION = "conversion_policy"

ON_OPEN_ASK = "ask"
ON_OPEN_AUTO = "auto"
ON_OPEN_NEVER = "never"
ON_OPEN_CHOICES = (ON_OPEN_ASK, ON_OPEN_AUTO, ON_OPEN_NEVER)

ORIGINALS_QUARANTINE = "quarantine"
ORIGINALS_RECYCLE = "recycle"
ORIGINALS_KEEP = "keep"
ORIGINALS_CHOICES = (ORIGINALS_QUARANTINE, ORIGINALS_RECYCLE, ORIGINALS_KEEP)

DEFAULT_RECENT_MINUTES = 10

Get = Callable[[str, str, str], str]
Put = Callable[[str, str, str], None]


@dataclass
class ConversionPolicy:
    on_open: str = ON_OPEN_ASK
    originals: str = ORIGINALS_QUARANTINE
    quarantine_folder: str = ""   # "" = a dated folder beside each file
    undo_log_folder: str = ""     # "" = beside the quarantine folder
    recent_minutes: int = DEFAULT_RECENT_MINUTES
    dry_run_first: bool = True

    @property
    def uses_quarantine(self) -> bool:
        return self.originals == ORIGINALS_QUARANTINE


def _int(text: str, default: int) -> int:
    try:
        value = int(text)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def load_policy(get: Get, defaults: ConversionPolicy | None = None) -> ConversionPolicy:
    """The saved policy. Until the user has saved it once (save_policy writes a `saved` marker) the app's `defaults`
    are returned as they are: they carry what the app did before this page existed, so nothing changes silently."""
    d = defaults or ConversionPolicy()
    if (get(SECTION, "saved", "") or "") != "1":
        return d

    def choice(key: str, allowed: tuple[str, ...], default: str) -> str:
        value = (get(SECTION, key, "") or "").strip().lower()
        return value if value in allowed else default

    return ConversionPolicy(
        on_open=choice("on_open", ON_OPEN_CHOICES, d.on_open),
        originals=choice("originals", ORIGINALS_CHOICES, d.originals),
        quarantine_folder=(get(SECTION, "quarantine_folder", "") or "").strip(),
        undo_log_folder=(get(SECTION, "undo_log_folder", "") or "").strip(),
        recent_minutes=_int(get(SECTION, "recent_minutes", ""), d.recent_minutes),
        dry_run_first=(get(SECTION, "dry_run_first", "1") or "1") != "0",
    )


def save_policy(policy: ConversionPolicy, put: Put) -> None:
    put(SECTION, "saved", "1")
    put(SECTION, "on_open", policy.on_open)
    put(SECTION, "originals", policy.originals)
    put(SECTION, "quarantine_folder", policy.quarantine_folder.strip())
    put(SECTION, "undo_log_folder", policy.undo_log_folder.strip())
    put(SECTION, "recent_minutes", str(policy.recent_minutes))
    put(SECTION, "dry_run_first", "1" if policy.dry_run_first else "0")

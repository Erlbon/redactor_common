"""
redactor_common/core/folder_refresh.py

Pure logic behind "Refresh List" (F5/Ctrl+R): given the paths already
loaded, re-scan the folder(s) they live in and report which matching
files are there now but weren't loaded before -- so a file added to
the same folder since Load Folder/Load Files ran gets picked up
without a manual re-browse.

Generalized off epubredactor's own refresh_list() (the original) and
cbzredactor's independently-rewritten equivalent (same behavior,
duplicated rather than shared) -- mp3redactor and videoredactor had
neither.

Deliberately does NOT discover a brand-new folder nothing has been
loaded from yet -- only folders already represented in `existing_paths`
get scanned, and whether that per-folder scan itself walks subfolders
is entirely up to the caller's own `find_files_in_folder`. Pass one
that's already bound to non-recursive (e.g.
`lambda folder: find_mp3_files([Path(folder)], recursive=False)`,
or video's own `discover_video_files(Path(folder))`, already
non-recursive by default) -- Refresh is meant to notice what changed
in the folder(s) you're already looking at, not to go discover an
entire new subfolder tree, which stays Load Folder's job.
"""

from __future__ import annotations

import os
from typing import Callable, Iterable


def find_new_files_in_loaded_folders(
    existing_paths: Iterable[str],
    find_files_in_folder: Callable[[str], Iterable[str]],
) -> list[str]:
    """Returns whichever paths `find_files_in_folder` reports for each
    distinct folder among `existing_paths` aren't already in
    `existing_paths` themselves -- sorted, de-duplicated against a
    normalized (normpath + normcase, so a case or separator difference
    on Windows doesn't produce a false "new" file) comparison.

    Reloading the files already known about is the caller's own job,
    same as it always was before this existed -- this only figures out
    what's new.
    """
    existing_list = [str(p) for p in existing_paths]
    seen = {_normalize(p) for p in existing_list}
    folders = sorted({os.path.dirname(os.path.normpath(p)) for p in existing_list})

    new_paths: list[str] = []
    for folder in folders:
        for path in find_files_in_folder(folder):
            path = str(path)
            normalized = _normalize(path)
            if normalized not in seen:
                seen.add(normalized)
                new_paths.append(os.path.normpath(path))

    new_paths.sort()
    return new_paths


def _normalize(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))

"""
redactor_common/core/folder_discovery.py

Finding the files of some kinds in a mix of files and folders: what Open
Folder, Refresh List, a drop and the convert offer ("this folder also has
FLAC files, convert them?") all do. Promoted 2026-10-14 from six copies
that each did it slightly differently (mp3redactor's find_mp3_files and
find_importable_files, epubredactor's _find_epubs_in_folder and
find_convertible_in_folder, videoredactor's discover_video_files and
discover_convertible_files, cbzredactor's comic_files_in_folder).

    find_by_extension(paths, {".mp3"}, recursive=True, skip=is_app_temp_name)

What every copy agreed on, and this keeps:
  - folders are searched (their subfolders too when `recursive`), files
    are taken when their extension is wanted; matching is case-insensitive
  - the result is de-duplicated (C:/a.mp3 and c:\\A.MP3 are one file on
    Windows) and sorted
  - an unreadable folder or a path that is not there is passed over, not
    an error (a scan must not stop on one locked folder)

What only some had, now for everyone:
  - `skip`: a name test for the app's own leftovers (core/temp_names'
    is_app_temp_name), applied to what a folder search finds. A file
    named directly is taken as the user meant it (videocli's rule) unless
    `skip_named` says otherwise (mp3redactor's old rule)
  - `ignored`: a list that collects what `skip` left out (videoredactor)
  - `resolve`: symlinks resolved like mp3redactor's Path.resolve()
  - a folder named "x.cbz" is not a file (cbzredactor's listing took it)

Paths come back as plain strings (absolute, native separators); an app
that works with pathlib wraps them: [Path(p) for p in found]. Qt-free.
This is one slow call for a big tree: GUI code runs it under
gui/progress.run_busy().
"""

from __future__ import annotations

import os
from typing import Callable, Iterable

Skip = Callable[[str], bool]


def normalize_extensions(extensions: Iterable[str]) -> tuple[str, ...]:
    """Lower-case extensions, each with its leading dot (".MP3", "mp3" -> ".mp3"). A compound extension such
    as ".tar.gz" is kept whole."""
    out = []
    for ext in extensions:
        ext = str(ext).strip().lower()
        if ext and not ext.startswith("."):
            ext = "." + ext
        if ext and ext not in out:
            out.append(ext)
    return tuple(out)


def has_extension(name: str, extensions: tuple[str, ...]) -> bool:
    """Whether the file name `name` ends in one of `extensions` (as normalize_extensions() returns them) and
    has a name before it: ".mp3" alone is not an MP3 file."""
    lower = name.lower()
    return any(lower.endswith(ext) and len(lower) > len(ext) for ext in extensions)


def _sort_key(path: str) -> tuple[str, ...]:
    # By folder levels, case-insensitively where the platform is, like sorting pathlib paths does.
    return tuple(os.path.normcase(path).split(os.sep))


def _walk(folder: str, recursive: bool) -> Iterable[str]:
    """The files in `folder` (and below it when `recursive`). Unreadable folders are passed over; folder
    links are not followed (no loops)."""
    if recursive:
        for root, _dirs, files in os.walk(folder):
            for name in files:
                yield os.path.join(root, name)
        return
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if entry.is_file():
                        yield entry.path
                except OSError:
                    continue
    except OSError:
        return


def find_by_extension(
    paths: Iterable[str | os.PathLike],
    extensions: Iterable[str],
    recursive: bool = True,
    skip: Skip | None = None,
    *,
    skip_named: bool = False,
    ignored: list[str] | None = None,
    resolve: bool = False,
) -> list[str]:
    """The files with one of `extensions` among `paths`, a mix of files and folders (as a drop, Open Files or
    Open Folder hands them over). Folders are searched, their subfolders too when `recursive`. De-duplicated
    (case-insensitively on Windows) and sorted; absolute paths as strings.

    `skip(name)` (a bare file name, e.g. core/temp_names.is_app_temp_name) leaves out what a folder search
    finds; files named in `paths` are kept unless `skip_named`. What `skip` left out is appended to
    `ignored` when a list is given (only files of a wanted extension: a "notes.partial.txt" is not counted).
    `resolve` resolves symbolic links (os.path.realpath) before de-duplicating."""
    wanted = normalize_extensions(extensions)
    found: dict[str, str] = {}
    skipped: dict[str, str] = {}

    def consider(path: str, named: bool) -> None:
        name = os.path.basename(path)
        if not has_extension(name, wanted):
            return
        full = os.path.realpath(path) if resolve else os.path.abspath(path)
        key = os.path.normcase(full)
        if key in found:
            return
        if skip is not None and (skip_named or not named) and skip(name):
            skipped.setdefault(key, full)
            return
        skipped.pop(key, None)  # named directly after a folder search skipped it: the user means it
        found[key] = full

    for raw in paths:
        path = os.fspath(raw)
        if os.path.isdir(path):
            for child in _walk(path, recursive):
                consider(child, named=False)
        elif os.path.isfile(path):
            consider(path, named=True)
    if ignored is not None:
        ignored.extend(sorted(skipped.values(), key=_sort_key))
    return sorted(found.values(), key=_sort_key)


def extension_labels(paths: Iterable[str | os.PathLike]) -> list[str]:
    """The distinct extensions among `paths` as upper-case labels without the dot, sorted ("FLAC", "WAV"):
    the "(FLAC, WAV)" in a "found N files" message."""
    labels = {os.path.splitext(os.fspath(p))[1].lstrip(".").upper() for p in paths}
    labels.discard("")
    return sorted(labels)

"""
redactor_common/core/lookups/cover_finder.py (the folder-cover part of mp3redactor's core/cover_art.py, 2026-11-04)

The album-art image that rippers and download stores put next to an album's tracks (cover.jpg, folder.jpg,
front.jpg ...), found by name, case-insensitively. Reads only the folder listing, never an image. Qt-free.
"""

from __future__ import annotations

import os
from pathlib import Path

from redactor_common.core.long_paths import literal

# Filenames checked, in order -- the first one present wins. Not albumartsmall.jpg: Windows Media Player's
# thumbnail (about 75 px) would be embedded as the album's cover (mp3redactor review card 68, 2026-11-27).
FOLDER_IMAGE_NAMES = (
    "cover.jpg", "cover.jpeg", "cover.png",
    "folder.jpg", "folder.jpeg", "folder.png",
    "front.jpg", "front.jpeg", "front.png",
    "albumart.jpg",
)


def find_image_in_folder(folder: str | Path) -> Path | None:
    """The album-art image in `folder` (cover.jpg, folder.jpg, front.jpg, ... case-insensitively), or None."""
    try:  # literal(): also a folder whose name ends in a dot ('R.E.M.'); the path given back is plain
        with os.scandir(literal(folder)) as entries:
            by_lower = {entry.name.lower(): Path(folder) / entry.name for entry in entries if entry.is_file()}
    except OSError:
        return None
    for name in FOLDER_IMAGE_NAMES:
        if name in by_lower:
            return by_lower[name]
    return None


def find_folder_image(audio_path: str | Path) -> Path | None:
    """The album-art image next to the file `audio_path`, or None."""
    return find_image_in_folder(Path(audio_path).parent)

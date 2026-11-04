"""
redactor_common/core/zip_names.py

Promoted from cbzredactor (2026-11-04) so every app that rewrites a ZIP keeps
legacy entry names the same way (core/zip_rewrite.py uses it).

Entry names in ZIP files written by older tools. Python's zipfile decodes a
name as UTF-8 only when the entry's general-purpose flag 0x800 is set, and
as cp437 otherwise; many tools (and every non-Latin one before ~2010) write
UTF-8 or a local code page WITHOUT the flag, so a page called "Märchen 01.jpg"
showed up as "MÃ¤rchen 01.jpg" -- and rewriting the archive with a fresh
ZipInfo(name) then stored that mojibake for good.

What this module does about it. The raw bytes are always recoverable
exactly (`orig_filename.encode("cp437")`, since cp437 maps every byte), so
for an entry WITHOUT the flag and with a non-ASCII name:

- bytes that are valid UTF-8 are taken to be a UTF-8 name that merely lacks
  the flag: the entry is known by that proper Unicode name (page list,
  sorting, ComicInfo lookup) and written back flagged as UTF-8, which
  repairs the archive for every reader;
- any other bytes (a Shift-JIS, GBK or cp1252 name, say) can't be decoded
  without guessing, so they are kept EXACTLY: the entry is written with
  its original filename bytes and flag bits (RawNameInfo). Its name in the
  app is zipfile's cp437 reading of those bytes -- odd to look at, but
  stable, and never turned into something else on save. No guessed legacy
  decoding is attempted.

decode_zip_name() is the one place that decides a name; open_zip() applies
it to every entry of an opened archive, so all readers (`zf.read(name)` with a
name from the page list included) agree.
"""

from __future__ import annotations

import os
import zipfile
from typing import Optional

_UTF8_FLAG = 0x800


def _sanitize(name: str) -> str:
    """What zipfile does to every name it reads: cut at the first NUL and
    use "/" as the separator (zipfile._sanitize_filename)."""
    null = name.find(chr(0))
    if null >= 0:
        name = name[:null]
    if os.sep != "/" and os.sep in name:
        name = name.replace(os.sep, "/")
    return name


def _utf8_reading(info: zipfile.ZipInfo) -> Optional[str]:
    """The entry's name read as UTF-8, for an unflagged non-ASCII name whose
    raw bytes are valid UTF-8; otherwise None."""
    try:
        return _sanitize(info.orig_filename.encode("cp437").decode("utf-8"))
    except UnicodeError:
        return None


def _is_unflagged_non_ascii(info: zipfile.ZipInfo) -> bool:
    """A name zipfile decoded as cp437 that is not plain ASCII, and that
    nothing (the Info-ZIP Unicode Path extra field) already overrode."""
    return (
        not info.flag_bits & _UTF8_FLAG
        and not info.orig_filename.isascii()
        and info.filename == _sanitize(info.orig_filename)
    )


def decode_zip_name(info: zipfile.ZipInfo) -> str:
    """The entry's real name (see the module docstring). Safe to call on an
    entry open_zip() already repaired: it returns the same name."""
    if _is_unflagged_non_ascii(info):
        repaired = _utf8_reading(info)
        if repaired is not None:
            return repaired
    return info.filename


def legacy_raw_name(info: zipfile.ZipInfo) -> Optional[bytes]:
    """The original filename bytes of an entry that must be written back
    byte for byte (unflagged, non-ASCII, not valid UTF-8), else None."""
    if _is_unflagged_non_ascii(info) and _utf8_reading(info) is None:
        try:
            return info.orig_filename.encode("cp437")
        except UnicodeEncodeError:  # decoded with another encoding: not ours to preserve
            return None
    return None


class RawNameInfo(zipfile.ZipInfo):
    """A ZipInfo whose filename bytes and flag bits are written exactly as
    given, instead of being re-encoded from `filename` (zipfile has no
    public way to do that; _encodeFilenameFlags is the hook it uses for both
    the local and the central header)."""

    def __init__(self, filename: str, date_time: tuple, raw_name: bytes):
        super().__init__(filename, date_time)
        self.raw_name = raw_name

    def _encodeFilenameFlags(self):
        return self.raw_name, self.flag_bits & ~_UTF8_FLAG


def raw_names_supported() -> bool:
    """False on a Python whose zipfile lacks the hook RawNameInfo relies on."""
    return callable(getattr(zipfile.ZipInfo, "_encodeFilenameFlags", None))


class ArchiveReader(zipfile.ZipFile):
    """A ZipFile whose entries carry decode_zip_name()'s names, so namelist(),
    getinfo() and read() all use the repaired name. ZipInfo.orig_filename is
    left alone: zipfile checks it against the local header on every read."""

    def __init__(self, file, mode="r", *args, **kwargs):
        super().__init__(file, mode, *args, **kwargs)
        if mode == "r":
            for info in self.filelist:
                info.filename = decode_zip_name(info)
            self.NameToInfo = {info.filename: info for info in self.filelist}


def open_zip(path, mode: str = "r") -> ArchiveReader:
    """zipfile.ZipFile(path, "r") with repaired entry names. Raises what
    zipfile raises."""
    return ArchiveReader(path, mode)

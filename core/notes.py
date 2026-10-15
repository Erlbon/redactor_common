"""
redactor_common/core/notes.py

The shared, Qt-free core for writing Markdown *folder notes* (artist.md, album.md, author.md, series.md,
about.md) that the Pluteus media server shows beside the folder they describe. The contract is Pluteus's own
docs/notes-format.md and docs/library-layout.md; this module only ever produces what Pluteus's own front matter
parser (pluteus/markdown.py `front_matter`) reads back to the same keys.

Rules (confirmed by the owner, 2026-10-15):

- A note is a UTF-8 file (no BOM, LF newlines) beside what it describes, at most 64 KB.
- Every note this module creates says ``status: draft`` until a person has read it.
- Never overwrite: `write_note()` writes only where the folder has no note of the recognised names at all
  (any letter case, and ANY of the five names: Pluteus shows the first one found in its priority order, so a
  new artist.md next to an existing about.md would hide the person's own note).
- Only `.md` notes count as "a note of our own". `.nfo` / `.opf` sidecars are reported separately by
  `existing_sidecar()` and do not stop a write (Pluteus shows our .md instead of them).

Front matter is deliberately tiny: plain ``key: value`` lines, ``key: [a, b]`` lists and ``- item`` block lists,
nothing nested, keys title, years, genres, tags, links, audience, status. Pluteus reads only ``title``; the rest
is for other tools. Quoting is minimal: a value is quoted only when a YAML reader or Pluteus's parser would
otherwise read it differently (see `_needs_quotes`). Pluteus's unquoting has no escape processing and its inline
list split is a plain comma split, so: a value holding both quote characters gets its straight double quotes
turned into typographic ones, and a list whose items contain a comma or bracket is written as a ``- item`` block.
"""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass, field
from typing import Optional

from redactor_common.core import temp_names

# Note kind -> file name. The order is Pluteus's priority order (pluteus/notes.py NOTE_NAMES[:5]): the first
# existing one wins on the folder page.
NOTE_KINDS: tuple[str, ...] = ("artist", "author", "series", "album", "about")
NOTE_FILE_NAMES: tuple[str, ...] = tuple(f"{kind}.md" for kind in NOTE_KINDS)
SIDECAR_FILE_NAMES: tuple[str, ...] = ("artist.nfo", "album.nfo", "metadata.opf")

MAX_NOTE_BYTES = 64 * 1024
DEFAULT_STATUS = "draft"

STATUS_WRITTEN = "written"
STATUS_EXISTS = "exists"
STATUS_TOO_LARGE = "too_large"
STATUS_ERROR = "error"


# ---- front matter ----------------------------------------------------------------------------------------------

_SPECIAL_START = set("[]{}-?!&*|>'\"%@`#,")


def _one_line(value: object) -> str:
    """A value as one tidy line: line breaks and runs of whitespace become a single space."""
    return re.sub(r"\s+", " ", str(value)).strip()


def _needs_quotes(value: str) -> bool:
    """True when `value` has to be quoted to be read back as the same string by Pluteus and by YAML readers.

    A leading special character (``[ { - ? ! & * | > ' " % @ # ,``, a leading ``[`` would otherwise become a
    list), a ``: `` or trailing ``:`` (a YAML key), a `` #`` (a YAML comment), or a bare ``---`` / ``...`` line."""
    if not value:
        return False
    if value[0] in _SPECIAL_START:
        return True
    if ": " in value or value.endswith(":") or " #" in value:
        return True
    return value in ("---", "...")


def _quote(value: str) -> str:
    """`value` wrapped in quotes Pluteus's `_unquote` strips again. It has no escapes, so pick the quote
    character that does not occur inside; with both present, straight double quotes become typographic ones."""
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    return '"' + value.replace('"', "”") + '"'


def _scalar(value: object) -> str:
    text = _one_line(value)
    return _quote(text) if _needs_quotes(text) else text


@dataclass
class NoteFrontMatter:
    """The front matter block of a note. Empty fields are left out of the output; `status` defaults to draft."""

    title: str = ""
    years: str = ""
    genres: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    audience: str = ""
    status: str = DEFAULT_STATUS

    def render(self) -> str:
        """The block, ``---`` line to ``---`` line, with a trailing newline. Key order is fixed: title, years,
        genres, tags, links, audience, status (a status is always written: an empty one becomes draft)."""
        lines = ["---"]
        for key, value in (("title", self.title), ("years", self.years)):
            text = _one_line(value)
            if text:
                lines.append(f"{key}: {_scalar(text)}")
        for key, values in (("genres", self.genres), ("tags", self.tags), ("links", self.links)):
            items = _clean_items(values)
            if not items:
                continue
            if any(_awkward(item) for item in items):
                lines.append(f"{key}:")
                lines.extend(f"  - {_scalar(item)}" for item in items)
            else:
                lines.append(f"{key}: [{', '.join(items)}]")
        audience = _one_line(self.audience)
        if audience:
            lines.append(f"audience: {_scalar(audience)}")
        lines.append(f"status: {_scalar(_one_line(self.status) or DEFAULT_STATUS)}")
        lines.append("---")
        return "\n".join(lines) + "\n"

    @classmethod
    def from_meta(cls, meta: dict) -> "NoteFrontMatter":
        """Builds one from a parse_front_matter() dict (unknown keys are dropped; a list where a string is
        expected is joined with ', ')."""

        def text(key: str) -> str:
            value = meta.get(key, "")
            return ", ".join(value) if isinstance(value, list) else str(value)

        def items(key: str) -> list[str]:
            value = meta.get(key, [])
            return list(value) if isinstance(value, list) else ([value] if value else [])

        return cls(title=text("title"), years=text("years"), genres=items("genres"), tags=items("tags"),
                   links=items("links"), audience=text("audience"), status=text("status") or DEFAULT_STATUS)


def _clean_items(values: object) -> list[str]:
    seen: list[str] = []
    for value in values or []:  # type: ignore[union-attr]
        text = _one_line(value)
        if text and text not in seen:
            seen.append(text)
    return seen


def _awkward(item: str) -> bool:
    """An item that forces the block list form: it holds a comma or bracket (Pluteus splits inline lists on
    commas), or it would need quotes (quotes inside an inline list are not safe across readers)."""
    return any(c in item for c in ",[]") or _needs_quotes(item)


_FRONT_KEY = re.compile(r"^([A-Za-z_][\w-]{0,40})\s*:\s*(.*)$")
_FRONT_ITEM = re.compile(r"^\s*-\s+(.*)$")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def parse_front_matter(text: str) -> tuple[dict, str]:
    """(meta, body): the front matter block at the very top of `text` and the text without it, read exactly
    the way Pluteus reads it (same grammar, keys lower-cased, a value is a string or a list of strings). Text
    without a clean block gives ({}, text unchanged). For tests and for tools reading an existing note."""
    text = text.lstrip("﻿")
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n", 80)[:80]
    if lines[0].rstrip() != "---":
        return {}, text
    meta: dict = {}
    current: Optional[str] = None
    for number, line in enumerate(lines[1:], start=1):
        line = line.rstrip("\r")
        if line.strip() in ("---", "..."):
            return meta, "\n".join(text.split("\n")[number + 1:]).lstrip("\n")
        item = _FRONT_ITEM.match(line)
        if item and current is not None:
            value = meta[current]
            if isinstance(value, str):
                value = meta[current] = [value] if value else []
            value.append(_unquote(item.group(1)))
            continue
        found = _FRONT_KEY.match(line)
        if not found:
            if line.strip():
                return {}, text
            continue
        current = found.group(1).lower()
        value = found.group(2).strip()
        if value.startswith("[") and value.endswith("]"):
            meta[current] = [_unquote(v) for v in value[1:-1].split(",") if v.strip()]
        else:
            meta[current] = _unquote(value)
    return {}, text


def render_note(front: NoteFrontMatter, body_markdown: str) -> str:
    """The whole note text: the front matter block, a blank line, the body, one trailing newline. LF only."""
    body = body_markdown.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    return front.render() + ("\n" + body + "\n" if body else "")


# ---- file names and finding existing notes -----------------------------------------------------------------------

def note_file_name(kind: str) -> str:
    """The file name for a note kind ('artist' -> 'artist.md'). ValueError for an unknown kind."""
    if kind not in NOTE_KINDS:
        raise ValueError(f"unknown note kind {kind!r}; expected one of {', '.join(NOTE_KINDS)}")
    return f"{kind}.md"


def note_path(folder: str | os.PathLike, kind: str) -> str:
    """Where the note of `kind` goes in `folder` (the path is built, nothing is touched)."""
    return os.path.join(os.fspath(folder), note_file_name(kind))


def _files_by_lower_name(folder: str | os.PathLike) -> dict[str, str]:
    """lower-cased name -> real path, for the plain files (and anything that is not a folder) in `folder`.
    A missing or unreadable folder gives {}."""
    found: dict[str, str] = {}
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                found.setdefault(entry.name.lower(), entry.path)
    except OSError:
        return {}
    return found


def existing_note(folder: str | os.PathLike) -> Optional[str]:
    """The path of the note Pluteus would show for `folder` (artist.md, author.md, series.md, album.md, about.md,
    any letter case, first in that priority order), or None. Only .md notes count; see existing_sidecar()."""
    files = _files_by_lower_name(folder)
    for name in NOTE_FILE_NAMES:
        if name in files:
            return files[name]
    return None


def existing_sidecar(folder: str | os.PathLike) -> Optional[str]:
    """The path of an artist.nfo / album.nfo / metadata.opf in `folder` (any letter case), or None. These are
    not notes: the writer may write beside them."""
    files = _files_by_lower_name(folder)
    for name in SIDECAR_FILE_NAMES:
        if name in files:
            return files[name]
    return None


# ---- the safe writer ---------------------------------------------------------------------------------------------

@dataclass
class NoteWriteResult:
    """What write_note() did. `status` is 'written', 'exists', 'too_large' or 'error'.

    `path` is the note's path ('written'), or the existing note's path ('exists'). With `dry_run` the status
    is what WOULD happen and nothing was touched."""

    status: str
    path: str = ""
    message: str = ""
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return self.status == STATUS_WRITTEN


def normalize_note_text(text: str) -> str:
    """The text as it is stored: no BOM, LF newlines, exactly one trailing newline (empty stays empty)."""
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    return text.rstrip("\n") + "\n" if text.strip() else ""


def _temp_path(folder: str, file_name: str) -> str:
    """A scratch name in the family's temp convention (core/temp_names: '<stem>.redact-part<ext>', hidden)."""
    stem, ext = os.path.splitext(file_name)
    name = f".{stem}.{secrets.token_hex(4)}.redact-part{ext}"
    assert temp_names.is_app_temp_name(name)
    return os.path.join(folder, name)


def _link_or_reserve(temp: str, target: str) -> None:
    """Puts the finished temp file at `target` WITHOUT ever replacing something that is there.

    First choice: a hard link (atomic, fails with FileExistsError if `target` exists, on Windows too). Where
    hard links do not exist (FAT/exFAT, some network shares) fall back to an exclusive create of `target`
    (O_EXCL, also atomic and also refusing) as a reservation, then os.replace() over our own reservation. The
    residual window of the fallback: for a few microseconds `target` is an empty file we created; another
    program replacing it in that gap would lose its data. Raises FileExistsError when `target` appeared."""
    try:
        os.link(temp, target)
        return
    except FileExistsError:
        raise
    except OSError:
        pass  # no hard links here: reserve and replace
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o666)
    os.close(fd)
    try:
        os.replace(temp, target)
    except OSError:
        try:
            os.unlink(target)
        except OSError:
            pass
        raise


def write_note(folder: str | os.PathLike, kind: str, text: str, *, dry_run: bool = False) -> NoteWriteResult:
    """Writes `text` as the note of `kind` in `folder`, refusing to overwrite. Never raises for an expected
    condition; the answer is in the result's status:

    - 'written'   the file now exists (with dry_run: it would be written, nothing was touched)
    - 'exists'    the folder already has a recognised .md note (any case, any of the five names); `path` is it
    - 'too_large' the text is over 64 KB as UTF-8
    - 'error'     unknown kind, missing folder, a read-only folder, an I/O failure; `message` says which

    The bytes are UTF-8 without BOM, LF newlines, one trailing newline. The file is written to a hidden temp
    file in the same folder (family name '.<stem>.<hex>.redact-part.md', so the apps' leftover filters skip it),
    flushed and fsynced, then linked into place (see _link_or_reserve): the target never holds a partial note
    and an existing file is never replaced. A temp file is removed on every path. Race note: existence is
    checked right before the link, and the link itself refuses an existing exact name; the only window left is
    a differently-cased name created by another program in that instant on a case-sensitive file system."""
    folder_s = os.fspath(folder)
    try:
        file_name = note_file_name(kind)
    except ValueError as exc:
        return NoteWriteResult(STATUS_ERROR, "", str(exc), dry_run)
    target = os.path.join(folder_s, file_name)
    if not os.path.isdir(folder_s):
        return NoteWriteResult(STATUS_ERROR, target, f"The folder does not exist: {folder_s}", dry_run)
    data = normalize_note_text(text).encode("utf-8")
    if len(data) > MAX_NOTE_BYTES:
        return NoteWriteResult(STATUS_TOO_LARGE, target,
                               f"The note is {len(data)} bytes; the limit is {MAX_NOTE_BYTES}.", dry_run)
    if not data:
        return NoteWriteResult(STATUS_ERROR, target, "The note is empty.", dry_run)
    existing = existing_note(folder_s)
    if existing:
        return NoteWriteResult(STATUS_EXISTS, existing, f"A note already exists: {existing}", dry_run)
    if os.path.lexists(target):
        return NoteWriteResult(STATUS_EXISTS, target, f"Something named {file_name} already exists.", dry_run)
    if dry_run:
        return NoteWriteResult(STATUS_WRITTEN, target, f"Would write {len(data)} bytes to {target}", True)

    temp = _temp_path(folder_s, file_name)
    try:
        try:
            fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o666)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            existing = existing_note(folder_s)  # last check before the link
            if existing:
                return NoteWriteResult(STATUS_EXISTS, existing, f"A note already exists: {existing}")
            _link_or_reserve(temp, target)
        except FileExistsError:
            return NoteWriteResult(STATUS_EXISTS, target, f"A note already exists: {target}")
        except OSError as exc:
            return NoteWriteResult(STATUS_ERROR, target, f"The note could not be written: {exc}")
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass
    return NoteWriteResult(STATUS_WRITTEN, target, f"Wrote {len(data)} bytes to {target}")

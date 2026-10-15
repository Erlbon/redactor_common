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

import hashlib
import os
import re
import secrets
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

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
STATUS_CHANGED = "changed"  # replace_note(): the note is not what the caller saw (edited meanwhile, or gone)

# The shape of what an app wrote, stamped into the front matter (see "stamps" below). The first released shapes
# (before the stamp existed) are format 1 and carry no key at all; a stamped note starts at 2.
CURRENT_NOTE_FORMAT = 2
STAMP_KEYS = ("generator", "note-format", "body-sha")

# The state of an existing note (note_status): what an app may do with it.
STATE_CURRENT = "current"                      # stamped, the current format, untouched: nothing to do
STATE_OUTDATED = "outdated-untouched"          # stamped, an older format, nobody edited it: may be refreshed
STATE_EDITED = "edited"                        # stamped but the hash differs: a person edited it. NEVER touched
STATE_LEGACY = "legacy"                        # no stamp, our 'Draft.' footer and status: draft (first releases)
STATE_FOREIGN = "foreign"                      # anything else: not ours. NEVER touched
REPLACEABLE_STATES = (STATE_OUTDATED, STATE_LEGACY)


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
    # The stamp (all optional, plain ``key: value`` lines Pluteus ignores): who wrote the note, in which shape, and a
    # hash of the note as written so a later run can tell whether a person edited it. render_note() fills body_sha.
    generator: str = ""  # e.g. 'mp3redactor 2026-10-15#08'
    note_format: int = 0  # 0 = no stamp (the first released shapes are format 1 and have no key); 2 and up are stamped
    body_sha: str = ""  # note_hash() of the note as written, 16 hex characters

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
        generator = _one_line(self.generator)
        if generator:
            lines.append(f"generator: {_scalar(generator)}")
        if self.note_format:
            lines.append(f"note-format: {int(self.note_format)}")
        if self.body_sha:
            lines.append(f"body-sha: {_one_line(self.body_sha)}")
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

        try:
            note_format = int(text("note-format") or 0)
        except ValueError:
            note_format = 0
        return cls(title=text("title"), years=text("years"), genres=items("genres"), tags=items("tags"),
                   links=items("links"), audience=text("audience"), status=text("status") or DEFAULT_STATUS,
                   generator=text("generator"), note_format=note_format, body_sha=text("body-sha"))


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
    tail = "\n" + body + "\n" if body else ""
    if (front.generator or front.note_format) and not front.body_sha:
        front = replace(front, body_sha=note_hash(front.render() + tail))
    return front.render() + tail


# ---- stamps: telling an old note of ours from one a person edited ----------------------------------------------

def note_hash(text: str) -> str:
    """The stamp's hash: the first 16 hex characters of the SHA-256 of the note as stored (normalize_note_text) with
    the ``body-sha`` line left out. It covers the whole note, front matter included, so changing the body, the
    status (draft -> reviewed), the genres or anything else counts as an edit; only the stamp line itself is free."""
    lines = normalize_note_text(text).split("\n")
    if lines and lines[0].rstrip() == "---":
        for number in range(1, min(len(lines), 80)):
            if lines[number].strip() in ("---", "..."):
                lines[1:number] = [l for l in lines[1:number] if not re.match(r"^body-sha\s*:", l)]
                break
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]


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
    replaced: str = ""  # replace_note(): the old note that went to the Recycle Bin

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


# ---- what an existing note is, and replacing an old one of ours ------------------------------------------------------

@dataclass
class NoteState:
    """What note_status() found. `kind` is one of the STATE_* constants. `sha` is the note_hash() of the file as it
    is now (what replace_note() checks again before it touches anything); `stored_sha` is what the stamp says."""

    kind: str
    path: str = ""
    generator: str = ""
    note_format: int = 0  # 0: no stamp
    sha: str = ""
    stored_sha: str = ""
    text: str = ""
    message: str = ""

    @property
    def replaceable(self) -> bool:
        return self.kind in REPLACEABLE_STATES


_LEGACY_FOOTER = re.compile(r"^\*Draft\.\s.*\*$")


def note_status(path: str | os.PathLike, current_format: int = CURRENT_NOTE_FORMAT) -> NoteState:
    """The state of the note file at `path` (see the STATE_* constants):

    - stamped (generator / note-format / body-sha keys): ``current`` when the hash matches and the format is
      `current_format` or newer; ``outdated-untouched`` when the hash matches and the format is older (nobody
      edited it); ``edited`` when the hash differs or is missing (a person edited it);
    - not stamped: ``legacy`` when it has our closing footer (a ``---`` line then ``*Draft. ...*``) and
      ``status: draft`` (written by the first releases: nobody can tell whether it was edited), else ``foreign``.

    A file that cannot be read as UTF-8 text, or is over the size limit, is ``foreign``."""
    path = os.fspath(path)
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_NOTE_BYTES + 1)
        if len(raw) > MAX_NOTE_BYTES:
            return NoteState(STATE_FOREIGN, path, message="over the size limit")
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return NoteState(STATE_FOREIGN, path, message=f"not readable as text: {exc}")
    meta, _body = parse_front_matter(text)
    sha = note_hash(text)
    stamped = any(key in meta for key in STAMP_KEYS)
    if stamped:
        try:
            fmt = int(str(meta.get("note-format") or 0))
        except ValueError:
            fmt = 0
        generator = str(meta.get("generator") or "")
        stored = str(meta.get("body-sha") or "")
        state = STATE_EDITED
        if stored and stored == sha:
            state = STATE_CURRENT if fmt >= current_format else STATE_OUTDATED
        return NoteState(state, path, generator, fmt, sha, stored, text)
    lines = [l for l in text.replace("\r\n", "\n").split("\n") if l.strip()]
    legacy = (str(meta.get("status", "")).strip().lower() == "draft" and len(lines) >= 2
              and lines[-2].strip() == "---" and bool(_LEGACY_FOOTER.match(lines[-1].strip())))
    return NoteState(STATE_LEGACY if legacy else STATE_FOREIGN, path, sha=sha, text=text)


def find_note(folder: str | os.PathLike, kind: str) -> Optional[str]:
    """The existing note file of exactly this `kind` in `folder` (any letter case), or None."""
    return _files_by_lower_name(folder).get(note_file_name(kind))


def replace_note(
    folder: str | os.PathLike, kind: str, new_text: str, *, expect: NoteState | str,
    dry_run: bool = False, trash: Optional[Callable[[str], None]] = None,
) -> NoteWriteResult:
    """Replaces the existing note of `kind` in `folder` with `new_text`: ONLY when it is still what the caller saw.

    `expect` is the NoteState the caller looked at (or just its ``sha``). Refused, and nothing touched, when:
    the note is gone, or its hash is no longer `expect`'s (a person edited it since: status 'changed'); its state
    is not ``outdated-untouched`` or ``legacy`` (``edited``, ``foreign`` and ``current`` notes are never replaced:
    status 'exists'); or `new_text` is too large.

    The new note is written to a hidden temp file first (so a failure leaves the old one alone), the old note is
    checked once more, then moved to the Recycle Bin (core/trash.py trash_with_retries; `trash` is the test
    seam) and the new one linked into place with the same no-overwrite machinery as write_note(). The result is
    like write_note()'s; ``replaced`` holds the path of the old note that went to the Recycle Bin."""
    folder_s = os.fspath(folder)
    try:
        file_name = note_file_name(kind)
    except ValueError as exc:
        return NoteWriteResult(STATUS_ERROR, "", str(exc), dry_run)
    target = os.path.join(folder_s, file_name)
    old = find_note(folder_s, kind)
    if old is None:
        return NoteWriteResult(STATUS_CHANGED, target, f"There is no {file_name} to replace any more.", dry_run)
    seen = expect.sha if isinstance(expect, NoteState) else str(expect)
    state = note_status(old)
    if not seen or state.sha != seen:
        return NoteWriteResult(STATUS_CHANGED, old, "The note was changed since it was looked at: left alone.", dry_run)
    if not state.replaceable:
        return NoteWriteResult(STATUS_EXISTS, old, f"The note is {state.kind}: it is never replaced.", dry_run)
    data = normalize_note_text(new_text).encode("utf-8")
    if len(data) > MAX_NOTE_BYTES:
        return NoteWriteResult(STATUS_TOO_LARGE, target,
                               f"The note is {len(data)} bytes; the limit is {MAX_NOTE_BYTES}.", dry_run)
    if not data:
        return NoteWriteResult(STATUS_ERROR, target, "The note is empty.", dry_run)
    if dry_run:
        return NoteWriteResult(STATUS_WRITTEN, target, f"Would replace {old} ({len(data)} bytes)", True, replaced=old)

    from redactor_common.core.trash import TrashError, trash_with_retries

    mover = trash or trash_with_retries()
    temp = _temp_path(folder_s, file_name)
    try:
        try:
            fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o666)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            return NoteWriteResult(STATUS_ERROR, target, f"The new note could not be written: {exc}")
        if note_status(old).sha != seen:  # the last look before the old note goes
            return NoteWriteResult(STATUS_CHANGED, old, "The note was changed meanwhile: left alone.")
        try:
            mover(old)
        except TrashError as exc:
            return NoteWriteResult(STATUS_ERROR, old, f"The old note could not be moved to the Recycle Bin, so it "
                                                      f"was kept: {exc}")
        if os.path.lexists(old):
            return NoteWriteResult(STATUS_ERROR, old, "The old note is still there after the move: left alone.")
        try:
            _link_or_reserve(temp, target)
        except FileExistsError:
            return NoteWriteResult(STATUS_EXISTS, target, f"A note appeared at {target} meanwhile: left alone.",
                                   replaced=old)
        except OSError as exc:
            return NoteWriteResult(STATUS_ERROR, target,
                                   f"The old note is in the Recycle Bin but the new one could not be written: {exc}",
                                   replaced=old)
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass
    return NoteWriteResult(STATUS_WRITTEN, target, f"Replaced {old}: the old note is in the Recycle Bin.", replaced=old)

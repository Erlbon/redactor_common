"""
redactor_common/core/audio_write.py

The ONE way the apps write audio tags with mutagen (mp3redactor's Save and
m4b builder, Dossier's tag editor). Before 2026-11-04 each had its own: Dossier
upgraded every ID3 v2.3 tag to v2.4 (which some car stereos and older
players can't read), mp3redactor replaced a file without reading it back.

- save_tags(audio) is the only place a mutagen object is saved: an ID3 tag
  keeps its version (2.3 stays 2.3; a new tag is 2.4), other formats get
  mutagen's defaults.
- edit_audio_copy(path, edit) is the safe way to change a file: the edit
  goes into a copy beside the real file (symlinks are followed, so the
  link stays a link), the copy is read back, and only then put in place
  (core/zip_rewrite.swap_in: renamed aside, optionally to the Recycle Bin).
  Any failure or cancel removes the copy and leaves the file as it was.
- read_audio_tags / write_audio_tags are the "easy" field interface
  (mutagen's EasyID3/EasyMP4/Vorbis keys) for an editor that edits a few
  common fields of mp3, flac, ogg, opus and m4a alike. Values are lists in
  and lists out (an ID3 v2.3 tag joins a list with "/", as the version
  requires). A genre reads as mutagen gives it: ID3 "(17)" is "Rock" on
  load and on save alike, so a compare and a write see the same value.

mutagen is imported lazily: an app without it can still import this module.
"""

from __future__ import annotations

import os
import stat
import tempfile
from typing import Callable, Iterable, Optional

from redactor_common.core.os_utils import is_lock_error
from redactor_common.core.zip_rewrite import RewriteCancelled, backup_name, copy_file, swap_in

# (easy key, label): the fields a generic tag editor offers.
AUDIO_FIELDS = (
    ("title", "Title"), ("artist", "Artist"), ("album", "Album"), ("albumartist", "Album artist"),
    ("tracknumber", "Track"), ("discnumber", "Disc"), ("date", "Year or date"), ("genre", "Genre"),
    ("composer", "Composer"),
)
AUDIO_EXTENSIONS = (".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".m4b", ".mp4a")
# Fields a format may store with more than was written ("3" read back as "3/12", "2019" as "2019-05-01").
_PREFIX_FIELDS = ("tracknumber", "discnumber", "date")


class AudioWriteError(OSError):
    """A file's tags could not be written; the file is as it was."""


# --- saving ------------------------------------------------------------------


def id3_save_kwargs(tags) -> dict:
    """The save() arguments that keep an ID3 tag's version: mutagen writes
    2.4 unless told otherwise."""
    version = getattr(tags, "version", None)
    if isinstance(version, tuple) and version[:2] == (2, 3):
        return {"v2_version": 3}
    return {}


def _inner_id3(tags):
    """The ID3 object behind mutagen's EasyID3 wrapper (it has no public
    accessor), or `tags` itself."""
    return getattr(tags, "_EasyID3__id3", tags)


def save_tags(audio, *, keep_id3_version: bool = True, **kwargs) -> None:
    """Saves a mutagen file object (or a bare tag object) in place. An ID3
    tag keeps its version unless `keep_id3_version` is False."""
    if keep_id3_version:
        tags = getattr(audio, "tags", None)
        found = id3_save_kwargs(_inner_id3(tags if tags is not None else audio))
        for key, value in found.items():
            kwargs.setdefault(key, value)
    audio.save(**kwargs)


# --- a safe edit -------------------------------------------------------------


def _make_writable(path: str) -> None:
    try:
        mode = os.stat(path).st_mode
        if not mode & stat.S_IWRITE:
            os.chmod(path, mode | stat.S_IWRITE)
    except OSError:
        pass


def _remove(path: str) -> None:
    _make_writable(path)
    try:
        os.remove(path)
    except OSError:
        pass


def check_audio_readable(path: str, opener: Optional[Callable[[str], object]] = None) -> None:
    """Raises AudioWriteError unless the file at `path` opens as audio
    (`opener`, default mutagen.File; mp3redactor passes mutagen's MP3). A
    transient lock error is raised as it is, so a caller can retry it."""
    if opener is None:
        import mutagen
        opener = mutagen.File
    try:
        opened = opener(path)
    except OSError as exc:
        if is_lock_error(exc):
            raise
        raise AudioWriteError(f"the new file does not read back ({exc}); the file was left as it was") from exc
    except Exception as exc:  # noqa: BLE001 -- any parse failure means "does not read back"
        raise AudioWriteError(f"the new file does not read back ({exc}); the file was left as it was") from exc
    if opened is None:
        raise AudioWriteError("the new file does not read back as audio; the file was left as it was")


def edit_audio_copy(
    path: str,
    edit: Callable[[str], None],
    *,
    verify: Optional[Callable[[str], None]] = check_audio_readable,
    before_swap: Optional[Callable[[str, str], None]] = None,
    dispose_original: Optional[Callable[[str], None]] = None,
    namer: Callable[[str], str] = backup_name,
    keep_new_if_dispose_fails: bool = False,
    should_cancel: Optional[Callable[[], bool]] = None,
    temp_prefix: str = ".redactor-",
) -> None:
    """Makes a copy of `path` beside it (`path` is resolved through
    symlinks), calls `edit(copy)` to change the copy, `verify(copy)` to read
    it back (default: it must still open as audio), `before_swap(target,
    copy)` for anything that must follow the file (Dossier's own marks), and
    then swaps the copy in. Anything `edit` or `verify` raises propagates,
    with the copy removed and the file as it was."""
    target = os.path.realpath(path)
    # The copy keeps the extension: mutagen tells some formats apart by it (a bare mp3 without a tag).
    fd, tmp = tempfile.mkstemp(prefix=temp_prefix, suffix=".tmp" + os.path.splitext(target)[1], dir=os.path.dirname(target))
    os.close(fd)
    try:
        copy_file(target, tmp, should_cancel)
        _make_writable(tmp)  # a read-only original gives a read-only copy: ours to edit and delete
        edit(tmp)
        if verify is not None:
            verify(tmp)
        if before_swap is not None:
            before_swap(target, tmp)
        if should_cancel is not None and should_cancel():
            raise RewriteCancelled("Cancelled")
        swap_in(tmp, target, dispose_original, namer=namer, keep_new_if_dispose_fails=keep_new_if_dispose_fails)
    except BaseException:
        _remove(tmp)
        raise


# --- the easy field interface ------------------------------------------------


def read_audio_tags(path: str, keys: Iterable[str] = tuple(k for k, _ in AUDIO_FIELDS)) -> dict:
    """{easy key: [values]} of the file at `path` ([] where it has none).
    Raises AudioWriteError when it can't be read as audio."""
    import mutagen
    try:
        easy = mutagen.File(path, easy=True)
    except Exception as exc:  # noqa: BLE001 -- a damaged file is a per-file problem, not a crash
        raise AudioWriteError(f"the file could not be read: {exc}") from exc
    if easy is None:
        raise AudioWriteError("this is not an audio file that can be read")
    tags = easy.tags or {}
    out = {}
    for key in keys:
        try:
            values = tags.get(key)
        except (KeyError, ValueError):
            values = None
        out[key] = [str(v) for v in values] if values else []
    return out


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return [str(v) for v in value if str(v)]


def _reads_back(key: str, wanted: list, got: list) -> bool:
    if got == wanted or (len(wanted) > 1 and got == ["/".join(wanted)]):  # ID3 v2.3 has one text per frame
        return True
    return (key in _PREFIX_FIELDS and len(got) == len(wanted)
            and all(g.startswith(w) for g, w in zip(got, wanted)))


def write_audio_tags(
    path: str,
    edits: dict,
    *,
    labels: Optional[dict] = None,
    **copy_kwargs,
) -> None:
    """Sets each easy `key: value` of `edits` (a string or a list of strings;
    empty removes the field) on the file at `path`, through
    edit_audio_copy() (its keyword arguments pass through). Only the keys
    given are touched: cover art, lyrics and unknown frames stay. The copy
    must read back with exactly what was asked for before it replaces the
    file. `labels` names the keys in error messages."""
    import mutagen
    wanted = {key: _as_list(value) for key, value in edits.items()}
    names = labels or {}

    def edit(tmp: str) -> None:
        easy = mutagen.File(tmp, easy=True)
        if easy is None:
            raise AudioWriteError("this is not an audio file that can be written")
        if easy.tags is None:
            easy.add_tags()
        for key, values in wanted.items():
            try:
                if values:
                    easy[key] = values
                elif key in easy:
                    del easy[key]
            except (ValueError, KeyError) as exc:
                raise AudioWriteError(f"{names.get(key, key)}: {exc}") from exc
        save_tags(easy)

    def verify(tmp: str) -> None:
        check_audio_readable(tmp)
        again = read_audio_tags(tmp, wanted)
        for key, values in wanted.items():
            if not _reads_back(key, values, again.get(key, [])):
                raise AudioWriteError(f"{names.get(key, key)} did not read back as written; the file was left as it was")

    edit_audio_copy(path, edit, verify=verify, **copy_kwargs)

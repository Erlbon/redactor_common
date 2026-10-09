"""
redactor_common/core/move_plan.py

The "Move into folders" mode of the Rename/Export dialog, Qt-free: turn a
pattern like `%author%/%series%/%title%` into a folder tree under a
library root, plan the moves for a batch, and execute them safely.

Three pieces:

  render_relative_path()  pattern + field values -> list of path segments
                          (folders..., file stem). The pattern is split on
                          "/" and "\\" BEFORE any token is substituted, so
                          a "/" inside a field value ("AC/DC") can never
                          create an extra folder: it is stripped like in a
                          plain filename. Every segment then goes through
                          the ordinary render_filename() rules.
  plan_moves()            a batch of items -> PlannedMove list, with
                          collision numbering (on disk and within the
                          batch), the folders each move would create, and
                          blocking warnings for anything unsafe (a
                          destination that resolves outside the root, e.g.
                          through a symlink/junction; a path too long).
  execute_move()          one move/copy: creates the missing folders
                          (reporting which it made), renames on the same
                          volume, and across volumes copies to a temp name,
                          verifies, renames into place and only then sends
                          the original to the Recycle Bin. Never overwrites
                          and never deletes outright.
  prune_empty_dirs()      the post-move tidy-up: removes directories that
                          are genuinely empty, never the root.

Optional (...)/[...]/{...} groups work per segment; a group must not span a
separator ("[%series%/]%title%" is not supported -- write
"%series%/%title%" and let the empty segment drop).

Windows MAX_PATH: paths over 259 characters fail in many tools (and in
Python without long-path support), so render_relative_path() takes a
`max_total` budget and plan_moves() derives it from the root's own length
(MAX_PATH_LENGTH minus root, extension and room for a " (99)" collision
suffix), shortening the longest segments first. A path that still doesn't
fit (an enormous root) is flagged blocking rather than silently failing
mid-batch. The same limit is applied on every platform so a library
stays portable between them.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, TypeVar

from redactor_common.core.os_utils import rename_no_clobber
from redactor_common.core.rename_pattern import (
    is_reserved_name, render_filename, sanitize_filename, unique_path,
)
from redactor_common.core.trash import move_to_trash

T = TypeVar("T")

MAX_PATH_LENGTH = 259  # Windows MAX_PATH (260) minus the terminator
_COLLISION_SUFFIX_ROOM = 5  # " (99)"
_MIN_SEGMENT = 8  # the trimming below never shortens a segment under this
_MIN_BUDGET = 40  # floor for the relative-path budget, however long the root is
_SEPARATORS_RE = re.compile(r"[\\/]")
_FILE_FALLBACK = "untitled"
# The notes _render_relative() adds when the file part of the pattern gave nothing and "untitled" was used
# instead. PlannedMove.nameless is set from them, so callers never have to read message text.
NOTE_EMPTY_NAME = f'the file name was empty, using "{_FILE_FALLBACK}"'
NOTE_NOTHING = f'the pattern produced nothing, using "{_FILE_FALLBACK}"'


def _split_pattern(pattern: str) -> list[str]:
    """The pattern's parts, split on / and \\ with blank parts (doubled,
    leading or trailing separators) dropped. The last part is the file."""
    return [part for part in _SEPARATORS_RE.split(pattern) if part.strip()]


def _shorten(segment: str, length: int) -> str:
    cut = sanitize_filename(segment[:length])
    if is_reserved_name(cut):
        cut = f"_{cut}"
    return cut or segment[:length].strip() or "_"


def _render_relative(
    values: dict[str, str],
    pattern: str,
    fallback_segment: str | None,
    aliases: dict[str, str] | None,
    ascii_only: bool,
    max_total: int | None,
) -> tuple[list[str], list[str]]:
    """Returns (segments, notes); notes are human-readable remarks
    (not errors) such as "file name was empty"."""
    parts = _split_pattern(pattern)
    notes: list[str] = []
    segments: list[str] = []
    for index, part in enumerate(parts):
        is_file = index == len(parts) - 1
        rendered = render_filename(values, part, fallback="", aliases=aliases, ascii_only=ascii_only)
        if not rendered:
            if is_file:
                rendered = _FILE_FALLBACK
                notes.append(NOTE_EMPTY_NAME)
            elif fallback_segment:
                rendered = fallback_segment
            else:
                continue
        segments.append(rendered)
    if not segments:
        segments = [_FILE_FALLBACK]
        notes.append(NOTE_NOTHING)

    if max_total is not None:
        def total() -> int:
            return sum(len(s) for s in segments) + len(segments) - 1

        trimmed = False
        while total() > max_total:
            longest = max(range(len(segments)), key=lambda i: len(segments[i]))
            if len(segments[longest]) <= _MIN_SEGMENT:
                break  # nothing left to shorten; the caller flags it
            target = max(_MIN_SEGMENT, len(segments[longest]) - (total() - max_total))
            segments[longest] = _shorten(segments[longest], target)
            trimmed = True
        if trimmed:
            notes.append("shortened to fit the path length limit")
    return segments, notes


def render_relative_path(
    values: dict[str, str],
    pattern: str,
    fallback_segment: str | None = None,
    aliases: dict[str, str] | None = None,
    ascii_only: bool = False,
    max_total: int | None = None,
) -> list[str]:
    """Path segments for `pattern`: every folder, then the file stem
    (no extension) as the last element; always at least one element.

    Each segment is rendered with render_filename(), so it can never
    contain a separator, "." / ".." (they sanitize to nothing), trailing
    dots or spaces, or a reserved Windows device name (prefixed with "_"),
    and is capped at MAX_FILENAME_LENGTH. A FOLDER segment that renders
    empty is dropped, or replaced by `fallback_segment` ("Unknown") if
    given. An empty FILE segment is always "untitled" -- a file needs a
    name. `max_total` caps the length of the joined relative path
    (separators included) by shortening the longest segments first."""
    return _render_relative(values, pattern, fallback_segment, aliases, ascii_only, max_total)[0]


@dataclass
class PlannedMove:
    item: Any
    old_path: str
    new_path: str  # absolute
    dirs_to_create: list[str] = field(default_factory=list)  # outermost first
    warning: str = ""
    blocking: bool = False  # True: must not be executed
    root: str = ""
    nameless: bool = False  # the pattern gave this file no name and "untitled" was used (see the NOTE_* constants)

    @property
    def is_noop(self) -> bool:
        return _same_path(self.old_path, self.new_path)

    def relative_path(self) -> str:
        """new_path relative to the root, with "/" separators."""
        try:
            return os.path.relpath(self.new_path, self.root).replace(os.sep, "/")
        except ValueError:
            return self.new_path


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _same_path(a: str, b: str) -> bool:
    return _norm(a) == _norm(b)


def resolves_inside(root: str, path: str) -> bool:
    """True if `path` stays under `root` after resolving symlinks and
    junctions (os.path.realpath copes with paths that don't exist yet by
    resolving the part that does). A path equal to the root is not inside."""
    try:
        real_root = os.path.normcase(os.path.realpath(root))
        real_path = os.path.normcase(os.path.realpath(path))
        return os.path.commonpath([real_root, real_path]) == real_root and real_path != real_root
    except ValueError:  # different drives, or an empty path
        return False


def _missing_dirs(root: str, directory: str) -> list[str]:
    """Directories between `root` and `directory` (inclusive of the
    latter) that don't exist yet, outermost first."""
    missing = []
    current = os.path.abspath(directory)
    stop = _norm(root)
    while _norm(current) != stop and not os.path.isdir(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return list(reversed(missing))


def plan_moves(
    items: Iterable[T],
    root: str,
    pattern: str,
    get_values: Callable[[T], dict[str, str]],
    get_current_path: Callable[[T], str],
    taken: set[str] | None = None,
    copy: bool = False,
    ascii_only: bool = False,
    aliases: dict[str, str] | None = None,
    fallback_segment: str | None = None,
) -> list[PlannedMove]:
    """One PlannedMove per item, in order. `get_values` should already
    include any zero-padding (the caller's job, as in the dialog).
    Collisions are numbered "Name (2).ext" against files on disk and
    against earlier items in the batch (`taken`, shared across calls if
    given); in move mode an item's own current path isn't a collision, so
    re-running over a tidy library plans no-ops. A move whose destination
    resolves outside `root` (symlink, junction) or is too long is
    `blocking`."""
    root_abs = os.path.abspath(root) if root else ""
    taken = taken if taken is not None else set()
    root_ok = bool(root_abs) and os.path.isdir(root_abs)
    planned: list[PlannedMove] = []

    for item in items:
        old_path = get_current_path(item)
        if not root_ok:
            planned.append(PlannedMove(
                item, old_path, old_path, warning="The library root folder doesn't exist.",
                blocking=True, root=root_abs,
            ))
            continue
        ext = os.path.splitext(old_path)[1]
        budget = max(_MIN_BUDGET, MAX_PATH_LENGTH - len(root_abs) - 1 - len(ext) - _COLLISION_SUFFIX_ROOM)
        segments, notes = _render_relative(
            dict(get_values(item)), pattern, fallback_segment, aliases, ascii_only, budget,
        )
        directory = os.path.join(root_abs, *segments[:-1])
        own = None if copy else old_path
        new_path = unique_path(directory, segments[-1], ext, taken, own)
        taken.add(_norm(new_path))

        warning, blocking = "; ".join(notes), False
        if not resolves_inside(root_abs, new_path):
            warning, blocking = "the destination would end up outside the library root", True
        elif len(new_path) > MAX_PATH_LENGTH:
            warning, blocking = f"the path is longer than {MAX_PATH_LENGTH} characters", True
        dirs = [] if blocking else _missing_dirs(root_abs, os.path.dirname(new_path))
        nameless = NOTE_EMPTY_NAME in notes or NOTE_NOTHING in notes
        planned.append(PlannedMove(item, old_path, new_path, dirs, warning, blocking, root_abs, nameless))
    return planned


# --- execution ------------------------------------------------------------

@dataclass
class MoveResult:
    new_path: str
    created_dirs: list[str] = field(default_factory=list)  # folders this call made, outermost first
    cross_volume: bool = False
    original_trashed: bool = False  # cross-volume move: the original is in the Recycle Bin
    original_kept: bool = False  # cross-volume move: trashing failed, both copies exist
    warning: str = ""


def _is_cross_device(exc: OSError) -> bool:
    return exc.errno == errno.EXDEV or getattr(exc, "winerror", None) == 17  # ERROR_NOT_SAME_DEVICE


def _copy_verified(old_path: str, new_path: str) -> None:
    """Copy to a temp name beside the destination, verify size and content
    fingerprint, then rename into place without clobbering. The temp file
    is removed on any failure."""
    from redactor_common.core.scan_stamp import content_fingerprint

    temp = os.path.join(os.path.dirname(new_path), f".{uuid.uuid4().hex[:8]}.part")
    try:
        shutil.copy2(old_path, temp)
        if os.path.getsize(temp) != os.path.getsize(old_path):
            raise OSError("the copy's size doesn't match the original")
        source_print = content_fingerprint(old_path)
        if not source_print or source_print != content_fingerprint(temp):
            raise OSError("the copy didn't verify against the original")
        rename_no_clobber(temp, new_path)
    except BaseException:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise


def _make_dirs(directory: str, on_created_dir: Callable[[str], None] | None) -> list[str]:
    missing = []
    current = os.path.abspath(directory)
    while not os.path.isdir(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    created = []
    for path in reversed(missing):
        try:
            os.mkdir(path)
        except FileExistsError:
            if not os.path.isdir(path):
                raise
            continue  # someone else just made it: not ours to report
        created.append(path)
        if on_created_dir is not None:
            on_created_dir(path)
    return created


def execute_move(
    old_path: str,
    new_path: str,
    copy: bool = False,
    trash: Callable[[str], None] = move_to_trash,
    on_created_dir: Callable[[str], None] | None = None,
) -> MoveResult:
    """Moves (or, with `copy`, copies) one file. Raises FileNotFoundError
    if the source is gone, FileExistsError if the destination exists --
    nothing is ever overwritten -- or another OSError. Missing folders
    are created first and reported in the result (and to `on_created_dir`
    as each is made).

    Same volume: rename_no_clobber. Across volumes (EXDEV /
    ERROR_NOT_SAME_DEVICE): copy to a temp name in the destination folder,
    verify, rename into place, then `trash(old_path)`. If trashing fails
    the original is kept and the result says so (`original_kept`); if the
    copy fails to verify nothing is changed and OSError is raised. Copy
    mode only ever copies (verified the same way) and leaves the original."""
    if not os.path.exists(old_path):
        raise FileNotFoundError(old_path)
    if os.path.lexists(new_path):
        raise FileExistsError(new_path)
    result = MoveResult(new_path=new_path)
    result.created_dirs = _make_dirs(os.path.dirname(new_path), on_created_dir)

    if copy:
        _copy_verified(old_path, new_path)
        return result
    try:
        rename_no_clobber(old_path, new_path)
        return result
    except FileExistsError:
        raise
    except OSError as exc:
        if not _is_cross_device(exc):
            raise
    result.cross_volume = True
    _copy_verified(old_path, new_path)
    try:
        trash(old_path)
        result.original_trashed = True
    except Exception as exc:  # noqa: BLE001 -- any trash failure keeps the original
        result.original_kept = True
        result.warning = f"copied, but the original couldn't be moved to the Recycle Bin ({exc}); it was kept"
    return result


def prune_empty_dirs(dirs: Iterable[str], stop_at_root: str | None = None, climb: bool = True) -> list[str]:
    """Removes each directory in `dirs` that is genuinely empty (no files,
    no hidden files, no sub-folders; symlinks are left alone), deepest
    first, and returns the ones removed. `stop_at_root`: never removed, and
    with `climb` the parents of a removed directory are tried too, but only
    while they are strictly inside it (so emptied Author/Series folders
    vanish together, and nothing above the root is ever touched). Without
    a root nothing is climbed."""
    root = _norm(stop_at_root) if stop_at_root else None
    removed: list[str] = []

    def try_remove(path: str) -> bool:
        if root is not None and _norm(path) == root:
            return False
        if os.path.islink(path) or not os.path.isdir(path):
            return False
        try:
            if os.listdir(path):
                return False
            os.rmdir(path)  # refuses a non-empty directory on its own, too
        except OSError:
            return False
        removed.append(path)
        return True

    def inside_root(path: str) -> bool:
        if root is None:
            return False
        try:
            return os.path.commonpath([root, _norm(path)]) == root and _norm(path) != root
        except ValueError:
            return False

    unique = {_norm(d): d for d in dirs if d}
    for path in sorted(unique.values(), key=lambda p: len(_norm(p)), reverse=True):
        if not try_remove(path):
            continue
        parent = os.path.dirname(os.path.abspath(path))
        while climb and inside_root(parent) and try_remove(parent):
            parent = os.path.dirname(parent)
    return removed

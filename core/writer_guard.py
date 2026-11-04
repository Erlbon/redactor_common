"""
redactor_common/core/writer_guard.py

Finds code that writes tags or archives behind the shared writers' backs.
Each app (cbzredactor, mp3redactor, Dossier) has a test that calls
find_private_writers() on its own source and fails on any hit, so a second
ComicInfo writer, zip rewriter or mutagen save can't creep back in after
2026-11-04, when the three apps' own writers were merged into
core/comicinfo_write.py, core/zip_rewrite.py and core/audio_write.py.

What counts as writing outside the shared modules:

- zipfile.ZipFile(...) opened with mode "w", "a" or "x", or a .writestr()
  call (a new or changed zip goes through zip_rewrite.rewrite_archive or
  write_new_archive);
- a .save() or .delete() call on something a mutagen call made (`audio =
  MP3(path)`, `easy = mutagen.File(path, easy=True)`, `audio.tags`): tags
  are saved by audio_write.save_tags / write_audio_tags;
- a tostring() call, or an ElementTree .write(), in a module that mentions
  ComicInfo (ComicInfo.xml is serialized by comicinfo_write).

The scan reads the source with ast, so a comment or a docstring that
mentions these calls is not a hit.
"""

from __future__ import annotations

import ast
import os
from typing import Iterable

_SKIP_DIRS = {"tests", "test", "build", "dist", "build-linux", "dist-linux", "__pycache__", ".git", ".venv", "venv", "node_modules"}


def _mode(call: ast.Call):
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        return call.args[1].value
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return None


def _name(func) -> str:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _root_name(node):
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else None


def _mutagen_objects(tree) -> set:
    """Names bound (anywhere in the module) to a value made by a mutagen
    call: `audio = MP3(path)`, `easy = mutagen.File(path, easy=True)`,
    `audio = retry_on_lock(lambda: MP3(path))`."""
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {(a.asname or a.name).split(".")[0] for a in node.names if a.name.split(".")[0] == "mutagen"}
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "mutagen":
            imported |= {a.asname or a.name for a in node.names}
    if not imported:
        return set()
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            made = any(isinstance(sub, ast.Call) and _root_name(sub.func) in imported for sub in ast.walk(node.value))
            if made:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                bound |= {t.id for t in targets if isinstance(t, ast.Name)}
    return bound | imported


def scan_source(source: str, filename: str = "<source>") -> list:
    """(line, what) for each private write in one module's source."""
    tree = ast.parse(source, filename)
    mutagen_objects = _mutagen_objects(tree)
    mentions_comicinfo = "ComicInfo" in source
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _name(node.func)
        if name == "ZipFile":
            mode = _mode(node)
            if isinstance(mode, str) and mode[:1] in ("w", "a", "x"):
                hits.append((node.lineno, f'ZipFile opened with mode "{mode}"'))
        elif name == "writestr" and isinstance(node.func, ast.Attribute):
            hits.append((node.lineno, "ZipFile.writestr()"))
        elif (name in ("save", "delete") and isinstance(node.func, ast.Attribute)
              and _root_name(node.func.value) in mutagen_objects):
            hits.append((node.lineno, f"a mutagen .{name}()"))
        elif mentions_comicinfo and name == "tostring":
            hits.append((node.lineno, "XML serialized in a module that handles ComicInfo"))
        elif (mentions_comicinfo and name == "write" and isinstance(node.func, ast.Attribute)
              and isinstance(node.func.value, ast.Call) and _name(node.func.value.func) == "ElementTree"):
            hits.append((node.lineno, "ElementTree.write() in a module that handles ComicInfo"))
    return sorted(hits)


def find_private_writers(root: str, *, skip: Iterable[str] = ()) -> list:
    """"path:line: what" for each private write in the .py files under
    `root` (tests, build output and virtualenvs skipped; `skip` adds paths
    relative to `root`, with "/" separators)."""
    skipped = {s.strip("/") for s in skip}
    found = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            if not name.endswith(".py") or name.startswith("test_") or name == "conftest.py":
                continue
            path = os.path.join(folder, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            if rel in skipped:
                continue
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            for line, what in scan_source(source, rel):
                found.append(f"{rel}:{line}: {what}")
    return found

"""move_to_trash hands the shell a plain path (no extended-length prefix, one slash kind)."""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from redactor_common.core import trash  # noqa: E402

EXT = chr(92) * 2 + "?" + chr(92)
# Elsewhere a backslash is part of a file name, not a separator, so normpath rightly keeps it.
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="backslash is a separator only on Windows")


@windows_only
def test_shell_path_strips_the_extended_prefix_and_unifies_slashes():
    got = trash.shell_path(EXT + "D:/Download" + chr(92) + "a.cbz")
    assert not got.startswith(EXT)
    assert got == os.path.normpath("D:/Download/a.cbz")


def test_shell_path_unc_form():
    got = trash.shell_path(EXT + "UNC" + chr(92) + "srv" + chr(92) + "share" + chr(92) + "a.cbz")
    assert got == os.path.normpath(chr(92) * 2 + "srv" + chr(92) + "share" + chr(92) + "a.cbz")


@windows_only
def test_move_to_trash_passes_the_plain_path(monkeypatch):
    seen = []
    monkeypatch.setitem(sys.modules, "send2trash", types.SimpleNamespace(send2trash=seen.append))
    monkeypatch.setattr(trash, "trash_is_reliable", lambda p, drive_type=None: True)
    trash.move_to_trash(EXT + "D:/Download" + chr(92) + "a.cbz")
    assert seen == [os.path.normpath("D:/Download/a.cbz")]

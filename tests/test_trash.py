"""Tests for core/trash.py (send2trash faked -- nothing reaches the real Recycle Bin)."""

import sys
import types

import pytest

from redactor_common.core import trash


def test_moves_through_send2trash(monkeypatch, tmp_path):
    moved = []
    monkeypatch.setitem(sys.modules, "send2trash", types.SimpleNamespace(send2trash=moved.append))
    trash.move_to_trash(tmp_path / "a.mkv")
    assert moved == [str(tmp_path / "a.mkv")]


def test_failures_become_trash_errors(monkeypatch, tmp_path):
    def refuse(_path):
        raise OSError("drive has no Recycle Bin")

    monkeypatch.setitem(sys.modules, "send2trash", types.SimpleNamespace(send2trash=refuse))
    with pytest.raises(trash.TrashError, match="Recycle Bin"):
        trash.move_to_trash(tmp_path / "a.mkv")
    monkeypatch.setitem(sys.modules, "send2trash", None)  # import fails
    with pytest.raises(trash.TrashError, match="isn't installed"):
        trash.move_to_trash(tmp_path / "a.mkv")

"""Tests for gui/column_menu.py: the top entries, plain and on/off (offscreen; the menu is captured, not shown)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QApplication, QMenu, QTableWidget

from redactor_common.gui import column_menu

app = QApplication.instance() or QApplication([])


def _open(monkeypatch, top_actions):
    shown = {}

    def fake_exec(self, *args):
        shown["menu"] = self
        return None

    monkeypatch.setattr(QMenu, "exec", fake_exec)
    table = QTableWidget(1, 2)
    column_menu.show_column_header_context_menu(
        table, table, QPoint(0, 0), column_order=["a", "b"], label_lookup={"a": "A", "b": "B"},
        protected_columns=frozenset(), hidden_fields=set(), is_visible=lambda k, h: k not in h,
        on_toggle=lambda k, c: None, top_actions=top_actions)
    _keep.append(table)               # the menu's parent; dropping it would delete the menu and its actions
    return [a for a in shown["menu"].actions() if not a.isSeparator()]


_keep: list = []


def test_plain_top_entry_is_not_checkable(monkeypatch):
    calls = []
    actions = _open(monkeypatch, [("Clear All Filters", lambda: calls.append(1), True)])
    assert actions[0].text() == "Clear All Filters"
    assert not actions[0].isCheckable()
    actions[0].trigger()
    assert calls == [1]


def test_fourth_item_makes_an_on_off_entry(monkeypatch):
    states = []
    actions = _open(monkeypatch, [("Fit Columns to Panel", states.append, True, True)])
    entry = actions[0]
    assert entry.isCheckable() and entry.isChecked()
    entry.trigger()
    assert states == [False]


def test_fourth_item_none_is_a_plain_entry(monkeypatch):
    actions = _open(monkeypatch, [("Plain", lambda: None, False, None)])
    assert not actions[0].isCheckable()
    assert not actions[0].isEnabled()

"""show_table_context_menu: the optional "Save Selected (N)" item."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QApplication, QMenu, QTableWidget, QTableWidgetItem, QWidget

from redactor_common.gui import context_menu
from redactor_common.gui.menu_builder import MenuAction, Separator


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class Doc:
    def __init__(self, name, dirty):
        self.name, self.dirty = name, dirty


def build(qapp, monkeypatch, docs, selected_rows, **kw):
    window = QWidget()
    table = QTableWidget(len(docs), 1, window)
    for i, d in enumerate(docs):
        table.setItem(i, 0, QTableWidgetItem(d.name))
    table.show()
    qapp.processEvents()
    table.setSelectionMode(QTableWidget.SelectionMode.MultiSelection)
    for r in selected_rows:
        table.selectRow(r)
    shown = {}

    def fake_exec(self, *a, **k):
        shown["actions"] = [(a_.text(), a_.isEnabled(), a_.toolTip(), a_) for a_ in self.actions()]

    monkeypatch.setattr(QMenu, "exec", fake_exec)
    pos = QPoint(5, table.rowViewportPosition(selected_rows[0]) + 3)
    context_menu.show_table_context_menu(
        window, table, pos,
        get_selected_items=lambda: [docs[i.row()] for i in sorted(table.selectionModel().selectedRows(), key=lambda x: x.row())],
        get_path=lambda d: d.name,
        **kw,
    )
    return shown["actions"], window


def texts(actions):
    return [t for t, _e, _tip, _a in actions if t]


def test_no_item_without_the_callback(qapp, monkeypatch):
    docs = [Doc("a", True)]
    actions, _w = build(qapp, monkeypatch, docs, [0])
    assert not any(t.startswith("Save Selected") for t in texts(actions))


def test_count_enabled_and_callback_gets_only_dirty_selected(qapp, monkeypatch):
    docs = [Doc("a", True), Doc("b", False), Doc("c", True), Doc("d", True)]
    saved = []
    actions, _w = build(qapp, monkeypatch, docs, [0, 1, 2], save_selected=saved.append, is_dirty=lambda d: d.dirty)
    item = next(x for x in actions if x[0].startswith("Save Selected"))
    assert item[0] == "Save Selected (2)" and item[1]
    item[3].trigger()
    assert [[d.name for d in batch] for batch in saved] == [["a", "c"]]  # not b (clean), not d (not selected)


def test_greyed_with_tooltip_when_nothing_dirty(qapp, monkeypatch):
    docs = [Doc("a", False), Doc("b", False)]
    saved = []
    actions, _w = build(qapp, monkeypatch, docs, [0, 1], save_selected=saved.append, is_dirty=lambda d: d.dirty)
    item = next(x for x in actions if x[0].startswith("Save Selected"))
    assert item[0] == "Save Selected (0)" and not item[1]
    assert item[2] == context_menu.NO_CHANGES_TIP == "The selected files have no unsaved changes"
    item[3].trigger()  # a disabled action does nothing
    assert saved == []


def test_item_is_after_the_file_actions_and_before_the_extras(qapp, monkeypatch):
    docs = [Doc("a", True)]
    actions, _w = build(
        qapp, monkeypatch, docs, [0], save_selected=lambda b: None, is_dirty=lambda d: d.dirty,
        extra_items=lambda items: [Separator(), MenuAction("x", "Extra Thing", lambda: None)],
    )
    names = texts(actions)
    assert names.index("Copy Path") < names.index("Save Selected (1)") < names.index("Extra Thing")


def test_without_is_dirty_every_selected_item_counts(qapp, monkeypatch):
    docs = [Doc("a", False), Doc("b", False)]
    actions, _w = build(qapp, monkeypatch, docs, [0, 1], save_selected=lambda b: None)
    assert "Save Selected (2)" in texts(actions)

"""Tests for gui/column_filter.py: the pure filter logic, plus an offscreen widget test of the header."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QTableWidget, QTableWidgetItem

from redactor_common.gui.column_filter import (
    BLANK_LABEL, ColumnFilterHeader, ColumnFilters, KIND_TEXT, KIND_VALUES, distinct_values,
)


# ---------------------------------------------------------------- pure logic

def test_no_filter_matches_everything():
    f = ColumnFilters()
    assert not f.any_active()
    assert f.active_columns() == []
    assert f.matches({"a": "x"})
    assert f.matches({})


def test_allowed_values():
    f = ColumnFilters()
    f.set_allowed("type", {"Movie", "TV"})
    assert f.is_active("type")
    assert f.matches({"type": "Movie"})
    assert not f.matches({"type": "Clip"})
    assert not f.matches({})  # a missing value counts as blank, which is not allowed
    f.set_allowed("type", {""})
    assert f.matches({"type": ""}) and f.matches({})


def test_contains_is_case_insensitive():
    f = ColumnFilters()
    f.set_contains("name", "  show ")
    assert f.contains("name") == "show"
    assert f.matches({"name": "The SHOW.S01"})
    assert not f.matches({"name": "Movie"})
    f.set_contains("name", "")
    assert not f.is_active("name")


def test_columns_combine_with_and():
    f = ColumnFilters()
    f.set_allowed("type", {"TV"})
    f.set_contains("name", "s01")
    assert f.matches({"type": "TV", "name": "x.S01E01"})
    assert not f.matches({"type": "Movie", "name": "x.S01E01"})
    assert not f.matches({"type": "TV", "name": "x.S02E01"})
    assert set(f.active_columns()) == {"type", "name"}


def test_same_column_values_and_contains_both_apply():
    f = ColumnFilters()
    f.set_allowed("name", {"alpha", "beta"})
    f.set_contains("name", "al")
    assert f.active_columns() == ["name"]
    assert f.matches({"name": "alpha"})
    assert not f.matches({"name": "beta"})


def test_clear_one_and_all():
    f = ColumnFilters()
    f.set_allowed("a", {"1"})
    f.set_contains("b", "x")
    f.clear("a")
    assert f.active_columns() == ["b"]
    f.clear()
    assert not f.any_active()


def test_set_allowed_none_removes_the_value_filter():
    f = ColumnFilters()
    f.set_allowed("a", {"1"})
    f.set_allowed("a", None)
    assert not f.is_active("a")


def test_distinct_values_sorted_blank_last():
    assert distinct_values(["b", "A", "", "b", "a"]) == ["A", "a", "b", ""]


# ---------------------------------------------------------------- widget (offscreen)

@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


_MADE: list = []


@pytest.fixture(autouse=True)
def _close_tables(app):
    yield
    while _MADE:
        table = _MADE.pop()
        table.close()
        table.deleteLater()
    app.processEvents()


def _table(app, rows):
    table = QTableWidget(len(rows), 3)
    header = ColumnFilterHeader(table)
    table.setHorizontalHeader(header)
    table.setHorizontalHeaderLabels(["Name", "Type", "Year"])
    keys = ["name", "type", "year"]
    header.configure(lambda col: keys[col], {"name": KIND_TEXT, "type": KIND_VALUES})
    for r, values in enumerate(rows):
        for c, v in enumerate(values):
            table.setItem(r, c, QTableWidgetItem(v))
    table.setColumnWidth(0, 200)
    table.setColumnWidth(1, 150)
    table.setColumnWidth(2, 100)
    table.resize(500, 200)
    pass  # not shown: the events are sent straight to the widgets
    _MADE.append(table)
    return table, header


ROWS = [("a.mkv", "Movie", "1999"), ("b.mkv", "TV", "2001"), ("c.mkv", "TV", "2002"), ("d.mkv", "", "2003")]


def test_header_value_filter_hides_rows_and_marks_the_column(app):
    table, header = _table(app, ROWS)
    seen = []
    header.filtersChanged.connect(lambda: seen.append(1))
    assert not header.is_active(1)
    header.set_value_filter("type", {"TV"})
    assert seen == [1]
    assert header.is_active(1) and not header.is_active(0)
    assert header.apply_to_table() == (2, 4)
    assert [table.isRowHidden(r) for r in range(4)] == [True, False, False, True]
    assert header.visible_rows() == [1, 2]
    header.clear_all_filters()
    assert not header.is_active(1)
    assert header.apply_to_table() == (4, 4)
    assert header.visible_rows() == [0, 1, 2, 3]


def test_header_text_filter(app):
    table, header = _table(app, ROWS)
    header.set_text_filter("name", "B.")
    assert header.apply_to_table() == (1, 4)
    assert header.visible_rows() == [1]


def test_unfilterable_column_has_no_funnel(app):
    _table_, header = _table(app, ROWS)
    assert header.funnel_rect(2) is None
    assert header.funnel_rect(0) is not None


def test_funnel_hit_test(app):
    _table_, header = _table(app, ROWS)
    rect = header.funnel_rect(1)
    assert rect is not None
    right_edge = header.sectionViewportPosition(1) + header.sectionSize(1)
    assert rect.right() < right_edge - 3  # clear of the resize grip
    assert header.funnel_at(rect.center()) == 1
    assert header.funnel_at(QPoint(header.sectionViewportPosition(1) + 5, rect.center().y())) is None
    assert header.funnel_at(QPoint(right_edge - 1, rect.center().y())) is None  # the resize grip, not the funnel


def test_funnel_click_does_not_sort(app):
    table, header = _table(app, ROWS)
    table.setSortingEnabled(True)
    before = header.sortIndicatorSection()
    order_before = [table.item(r, 0).text() for r in range(4)]
    rect = header.funnel_rect(1)
    opened = []
    header.open_popup = lambda col: opened.append(col)  # the real popup blocks in exec(); tested below
    QTest.mouseClick(header.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    assert opened == [1]
    assert header.sortIndicatorSection() == before
    assert [table.item(r, 0).text() for r in range(4)] == order_before
    # a click elsewhere on the section still sorts
    QTest.mouseClick(header.viewport(), Qt.MouseButton.LeftButton,
                     pos=QPoint(header.sectionViewportPosition(1) + 8, rect.center().y()))
    assert header.sortIndicatorSection() == 1


def test_popup_drives_the_filter(app):
    table, header = _table(app, ROWS)
    popup = header.build_popup(1)
    assert [it.text() for it in popup.items()] == ["Movie", "TV", BLANK_LABEL]
    assert popup.contains_edit is None  # values-only column
    popup.set_checked_values({"TV"})
    assert header.filters.allowed("type") == frozenset({"TV"})
    assert header.apply_to_table() == (2, 4)
    popup.select_all()
    assert header.filters.allowed("type") is None and not header.is_active(1)
    text_popup = header.build_popup(0)
    assert text_popup.contains_edit is not None
    text_popup.set_contains_text("c.")
    assert header.filters.contains("name") == "c."
    assert header.apply_to_table() == (1, 4)
    text_popup.clear_button.click()
    assert not header.is_active(0)


def test_hidden_selected_rows_are_deselected(app):
    table, header = _table(app, ROWS)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.selectAll()
    header.set_value_filter("type", {"TV"})
    header.apply_to_table()
    assert sorted({i.row() for i in table.selectedItems()}) == [1, 2]


def test_paints_active_and_inactive_headers(app):
    table, header = _table(app, ROWS)
    header.set_value_filter("type", {"TV"})
    image = header.grab().toImage()
    funnel = header.funnel_rect(1)
    # the active column's header carries the accent tint: its centre differs from the inactive column's
    active = image.pixelColor(header.sectionViewportPosition(1) + 5, header.height() // 2)
    inactive = image.pixelColor(header.sectionViewportPosition(0) + 5, header.height() // 2)
    assert active != inactive
    assert funnel is not None

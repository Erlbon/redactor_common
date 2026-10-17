"""enable_sorting / NaturalTableWidgetItem / row_of / lint_sortable_headers / remembered_layout (2026-10-17)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QDialog, QLabel, QTableWidget, QTextEdit  # noqa: E402

from redactor_common.gui.remembered_layout import remember_layout, results_detail_splitter  # noqa: E402
from redactor_common.gui.sortable_table import (  # noqa: E402
    NaturalTableWidgetItem, NumericTableWidgetItem, enable_sorting, natural_key, row_of, suspend_sorting,
)
from redactor_common.gui.table_columns import lint_sortable_headers  # noqa: E402

_app = QApplication.instance() or QApplication([])


def test_natural_key_orders_numbers_inside_text_and_ignores_case():
    names = ["Episode 10", "episode 2", "Episode 1"]
    assert sorted(names, key=natural_key) == ["Episode 1", "episode 2", "Episode 10"]
    assert sorted(["2014-02-17", "2014", "2013-12-31"], key=natural_key) == ["2013-12-31", "2014", "2014-02-17"]


def _filled():
    table = QTableWidget(0, 2)
    table.setHorizontalHeaderLabels(["Name", "Size"])
    enable_sorting(table)
    with suspend_sorting(table):
        table.setRowCount(3)
        for row, (name, size) in enumerate([("b10", 9), ("b2", 100), ("a1", 20)]):
            first = NaturalTableWidgetItem(name)
            first.setData(Qt.ItemDataRole.UserRole, row)
            table.setItem(row, 0, first)
            table.setItem(row, 1, NumericTableWidgetItem(str(size)))
    return table


def test_enable_sorting_keeps_the_filled_order_until_a_header_is_clicked():
    table = _filled()
    assert [table.item(r, 0).text() for r in range(3)] == ["b10", "b2", "a1"]
    assert table.horizontalHeader().isSortIndicatorShown()


def test_sorting_is_natural_and_numeric_and_the_key_follows_the_row():
    table = _filled()
    table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
    assert [table.item(r, 0).text() for r in range(3)] == ["a1", "b2", "b10"]
    assert table.item(0, 0).data(Qt.ItemDataRole.UserRole) == 2
    assert row_of(table, 0) == 2
    table.sortByColumn(1, Qt.SortOrder.DescendingOrder)
    assert [table.item(r, 1).text() for r in range(3)] == ["100", "20", "9"]


def test_lint_flags_a_table_that_never_sorts_and_accepts_the_marked_or_sorted_ones(tmp_path):
    (tmp_path / "bad.py").write_text("t = QTableWidget(0, 2)\n", encoding="utf-8")
    (tmp_path / "good.py").write_text("t = QTableWidget(0, 2)\nenable_sorting(t)\n", encoding="utf-8")
    (tmp_path / "native.py").write_text("t = QTableView()\nt.setSortingEnabled(True)\n", encoding="utf-8")
    (tmp_path / "log.py").write_text("# sortable: no (a log, newest last)\nt = QTableWidget(0, 2)\n", encoding="utf-8")
    (tmp_path / "bare.py").write_text("t = QTableWidget(0, 2)  # sortable: no\n", encoding="utf-8")
    (tmp_path / "imp.py").write_text("from PyQt6.QtWidgets import QTableWidget\nx = QTableWidget.SelectionBehavior\n",
                                     encoding="utf-8")
    problems = lint_sortable_headers(tmp_path)
    flagged = sorted(p.split(":")[0] for p in problems)
    assert flagged == ["bad.py", "bare.py"]  # a marker needs a reason in brackets


def test_remembered_layout_restores_and_saves_size_and_splitter():
    store: dict = {}
    get = lambda s, k, d="": store.get((s, k), d)  # noqa: E731
    put = lambda s, k, v: store.__setitem__((s, k), v)  # noqa: E731
    dialog = QDialog()
    splitter = results_detail_splitter(QLabel("results"), QTextEdit())
    assert splitter.orientation() == Qt.Orientation.Vertical and splitter.handleWidth() >= 6
    assert not splitter.childrenCollapsible()
    remember_layout(dialog, "x", get, put, splitter)
    dialog.resize(700, 500)
    dialog.finished.emit(0)
    assert store[("dialog_layout", "x_size")] == "700x500"
    again = QDialog()
    split2 = results_detail_splitter(QLabel("r"), QTextEdit())
    remember_layout(again, "x", get, put, split2)
    assert (again.width(), again.height()) == (700, 500)
    store[("dialog_layout", "y_size")] = "garbage"
    third = QDialog()
    remember_layout(third, "y", get, put)  # garbage is ignored


def test_enable_header_sort_calls_back_ascending_then_descending():
    from redactor_common.gui.sortable_table import enable_header_sort

    table = QTableWidget(2, 2)
    table.setHorizontalHeaderLabels(["A", "B"])
    calls = []
    enable_header_sort(table, lambda col, order: calls.append((col, order)))
    header = table.horizontalHeader()
    header.sectionClicked.emit(1)
    header.sectionClicked.emit(1)
    header.sectionClicked.emit(0)
    asc, desc = Qt.SortOrder.AscendingOrder, Qt.SortOrder.DescendingOrder
    assert calls == [(1, asc), (1, desc), (0, asc)]
    assert header.sortIndicatorSection() == 0 and header.isSortIndicatorShown()


def test_lint_accepts_the_rebuild_style_sort(tmp_path):
    (tmp_path / "w.py").write_text("t = QTableWidget(0, 2)\nenable_header_sort(t, f)\n", encoding="utf-8")
    assert lint_sortable_headers(tmp_path) == []

"""The shared Path column: folder text, full-path tooltip/copy, natural sort, migration, lint."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QGuiApplication  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QTableWidget  # noqa: E402

from redactor_common.core.path_column import (  # noqa: E402
    PATH_KEY,
    folder_of,
    insert_path_key,
    lint_path_column,
    migrate_saved_columns,
    natural_sort_key,
    path_tooltip,
)
from redactor_common.gui.path_column import PathTableWidgetItem, install_cell_copy, selected_column_text  # noqa: E402

_app = QApplication.instance() or QApplication([])
NL = chr(10)


def test_folder_and_tooltip(tmp_path):
    p = tmp_path / "Sub" / "a.mp3"
    assert folder_of(p) == str(tmp_path / "Sub")
    assert path_tooltip(p) == str(p)
    assert folder_of("") == "" and folder_of(None) == ""


def test_natural_sort_is_case_insensitive():
    names = ["Vol 10", "vol 2", "Vol 1"]
    assert sorted(names, key=natural_sort_key) == ["Vol 1", "vol 2", "Vol 10"]


def test_insert_path_key_after_filename():
    assert insert_path_key(["a", "filename", "b"]) == ["a", "filename", "path", "b"]
    assert insert_path_key(["path", "x", "filename", "y"]) == ["x", "filename", "path", "y"]
    assert insert_path_key(["x"]) == ["x", "path"]


def test_migration_moves_and_unhides():
    order, hidden = migrate_saved_columns(["title", "filename", "year", "path"], {"path", "year"})
    assert order == ["title", "filename", "path", "year"]
    assert hidden == {"year"}
    assert migrate_saved_columns([], {"path"}) == ([], set())


def test_lint():
    assert lint_path_column(["filename", "path", "ext"]) == []
    assert lint_path_column(["filename", "ext"])
    assert lint_path_column(["path", "filename"])
    assert lint_path_column(["filename", "ext", "path"])
    assert lint_path_column(["a", "b"])
    assert PATH_KEY == "path"


def test_item_shows_folder_sorts_naturally_and_refreshes(tmp_path):
    table = QTableWidget(3, 1)
    for row, d in enumerate(["Dir 10", "dir 2", "Dir 1"]):
        table.setItem(row, 0, PathTableWidgetItem(tmp_path / d / "f.epub"))
    assert table.item(0, 0).text() == str(tmp_path / "Dir 10")
    assert table.item(0, 0).toolTip() == str(tmp_path / "Dir 10" / "f.epub")
    table.sortItems(0, Qt.SortOrder.AscendingOrder)
    assert [os.path.basename(table.item(r, 0).text()) for r in range(3)] == ["Dir 1", "dir 2", "Dir 10"]
    table.item(0, 0).set_path(tmp_path / "moved" / "g.epub")
    assert table.item(0, 0).text() == str(tmp_path / "moved")


def test_ctrl_c_copies_full_paths(tmp_path):
    table = QTableWidget(2, 2)
    paths = [tmp_path / "a" / "one.cbz", tmp_path / "b" / "two.cbz"]
    for row, p in enumerate(paths):
        table.setItem(row, 1, PathTableWidgetItem(p))
    install_cell_copy(table)
    table.show()
    table.setCurrentCell(0, 1)
    table.selectAll()
    expected = NL.join(str(p) for p in paths)
    assert selected_column_text(table) == expected
    QGuiApplication.clipboard().clear()
    QTest.keyClick(table, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert QGuiApplication.clipboard().text() == expected
    table.close()

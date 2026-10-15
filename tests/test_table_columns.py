"""gui/table_columns: every column draggable, sensible starting widths, and the lint that keeps it so."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QHeaderView, QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem  # noqa: E402

from redactor_common.gui.table_columns import lint_resizable_headers, make_columns_resizable  # noqa: E402

_app = QApplication.instance() or QApplication([])


def _table():
    table = QTableWidget(4, 4)
    table.setHorizontalHeaderLabels(["Name", "Year", "Country", "Reasons"])
    for row in range(4):
        for col, text in enumerate(["A long candidate name " * 3, "1984", "GB", "title found in the discography " * 6]):
            table.setItem(row, col, QTableWidgetItem(text))
    table.resize(900, 300)
    table.show()
    return table


def test_every_column_is_draggable_and_can_be_resized():
    table = _table()
    make_columns_resizable(table, wide_column=3)
    header = table.horizontalHeader()
    for section in range(3):  # the last one also stretches to the right edge
        assert header.sectionResizeMode(section) == QHeaderView.ResizeMode.Interactive
    before = header.sectionSize(1)
    header.resizeSection(1, before + 70)
    assert header.sectionSize(1) == before + 70
    table.close()


def test_no_column_but_the_wide_one_starts_wider_than_the_cap():
    table = _table()
    make_columns_resizable(table, wide_column=3)
    header = table.horizontalHeader()
    viewport = max(table.viewport().width(), 300)
    assert header.sectionSize(0) <= int(viewport * 0.56)
    assert header.sectionSize(3) >= 180
    table.close()


def test_works_for_a_tree_too():
    tree = QTreeWidget()
    tree.setHeaderLabels(["Folder", "Level", "Confidence"])
    QTreeWidgetItem(tree, ["Ultravox / Vienna (1980)", "album", "confident"])
    tree.resize(700, 200)
    tree.show()
    make_columns_resizable(tree, wide_column=0)
    header = tree.header()
    assert header.sectionResizeMode(1) == QHeaderView.ResizeMode.Interactive
    tree.close()


def test_lint_flags_fixed_modes_and_honours_the_marker(tmp_path):
    gui = tmp_path / "gui"
    gui.mkdir()
    (gui / "bad.py").write_text(
        "header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)\n"
        "header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)\n"
        "header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)\n"
        "header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)  # resizable-ok: a row header\n",
        encoding="utf-8",
    )
    problems = lint_resizable_headers(gui)
    assert len(problems) == 2 and all("bad.py" in p for p in problems)
    assert not any(":3:" in p or ":4:" in p for p in problems)


def test_lint_skips_tests_and_is_empty_for_clean_code(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t.py").write_text("h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text("make_columns_resizable(table, wide_column=1)\n", encoding="utf-8")
    assert lint_resizable_headers(tmp_path) == []


def test_redactor_commons_own_dialogs_keep_every_header_draggable():
    from pathlib import Path

    gui = Path(__file__).resolve().parent.parent / "gui"
    assert lint_resizable_headers(gui) == []

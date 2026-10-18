from PyQt6.QtCore import Qt
import pytest
from PyQt6.QtWidgets import QApplication, QTableWidget

from redactor_common.gui.preview_table import PreviewRow, PreviewTableController


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _rows():
    return [
        PreviewRow(("a", 1), "Studio", "", "Evil Angel", group="f1", default_checked=True),
        PreviewRow(("a", 2), "Title", "old title", "A very long new title " * 8, group="f1", default_checked=False),
        PreviewRow(("a", 3), "Year", "2022", "2022", group="f1", default_checked=True),
    ]


def test_apply_is_first_column_and_accepted_changes_follow_ticks(qapp):
    table = QTableWidget()
    ctrl = PreviewTableController(table, item_column_label="Field", group_column_label="File")
    ctrl.set_rows(_rows())
    assert table.horizontalHeaderItem(0).text() == "Apply"
    assert set(ctrl.accepted_changes()) == {("a", 1), ("a", 3)}
    table.item(1, 0).setCheckState(Qt.CheckState.Checked)
    assert ("a", 2) in ctrl.accepted_changes()


def test_wraps_and_colours_overwrite_only(qapp):
    table = QTableWidget()
    ctrl = PreviewTableController(table, item_column_label="Field", group_column_label="File")
    ctrl.set_rows(_rows())
    assert table.wordWrap()
    old_col, new_col = 3, 4
    assert table.item(1, old_col).background().color().name() != table.item(0, old_col).background().color().name()
    assert table.item(1, new_col).background().style() != Qt.BrushStyle.NoBrush  # green
    assert table.item(0, new_col).background().style() != Qt.BrushStyle.NoBrush  # blank fill is green too
    assert table.item(0, old_col).background().style() == Qt.BrushStyle.NoBrush  # blank current: no red
    assert table.item(2, old_col).background().style() == Qt.BrushStyle.NoBrush  # unchanged: no colour
    assert table.rowHeight(1) > table.rowHeight(2)  # long value wrapped onto more lines


def test_accepted_changes_survive_sorting(qapp):
    table = QTableWidget()
    ctrl = PreviewTableController(table, item_column_label="Field", group_column_label="File")
    ctrl.set_rows(_rows())
    table.sortByColumn(2, Qt.SortOrder.DescendingOrder)
    assert set(ctrl.accepted_changes()) == {("a", 1), ("a", 3)}


def test_review_dialog_splits_files_and_keeps_ticks(qapp):
    from redactor_common.gui.overwrite_review_dialog import OverwriteReviewDialog

    rows = [
        PreviewRow((0, "title"), "Title", "x", "y", group="one.mp4", default_checked=False),
        PreviewRow((0, "studio"), "Studio", "", "S", group="one.mp4"),
        PreviewRow((1, "studio"), "Studio", "", "S", group="two.mp4"),
    ]
    d = OverwriteReviewDialog(rows)
    assert d._file_list is not None and d._file_list.rowCount() == 2
    d._file_list.selectRow(1)
    assert d._stack.currentIndex() == 1
    d._controllers[0].set_all_checked(True)
    assert set(d.accepted_changes()) == {(0, "title"), (0, "studio"), (1, "studio")}
    d._next_overwrite()
    assert d._stack.currentIndex() == 0
    d._all_files(False)
    assert d.accepted_changes() == {}


def test_review_dialog_single_file_has_no_file_list(qapp):
    from redactor_common.gui.overwrite_review_dialog import OverwriteReviewDialog

    d = OverwriteReviewDialog([PreviewRow((0, "a"), "A", "", "v", group="f")])
    assert d._file_list is None
    assert d.accepted_changes() == {(0, "a"): "v"}

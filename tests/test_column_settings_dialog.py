"""Tools > Columns dialog: visibility checkboxes plus reordering (arrows, drag)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QHeaderView, QLabel, QPushButton, QTableWidget

from redactor_common.gui.column_order import apply_column_order, visual_column_keys
from redactor_common.gui.column_settings_dialog import (
    EARLIER_TIP, HINT_TEXT, LATER_TIP, ColumnSettingsDialog,
)

COLS = [("filename", "File"), ("path", "Path"), ("title", "Title"), ("artist", "Artist"), ("year", "Year")]
PROTECTED = frozenset({"filename"})


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def make(qapp, hidden=frozenset(), columns=COLS):
    dialog = ColumnSettingsDialog(list(columns), set(hidden), PROTECTED)
    dialog.show()
    qapp.processEvents()
    return dialog


def button_by_name(dialog, name):
    for button in dialog.findChildren(QPushButton):
        if button.accessibleName() == name:
            return button
    raise AssertionError(name)


def labels(dialog):
    return [dialog.list_widget.item(i).text() for i in range(dialog.list_widget.count())]


def test_items_in_given_order_and_hint(qapp):
    dialog = make(qapp, columns=list(reversed(COLS)))
    assert dialog.column_order() == [k for k, _l in reversed(COLS)]
    assert labels(dialog)[0] == "Year"
    assert any(w.text() == HINT_TEXT for w in dialog.findChildren(QLabel))


def test_buttons_are_glyphs_with_tooltips(qapp):
    dialog = make(qapp)
    up, down = button_by_name(dialog, EARLIER_TIP), button_by_name(dialog, LATER_TIP)
    assert up.text() == "▲" and down.text() == "▼"
    assert up.toolTip() == EARLIER_TIP and down.toolTip() == LATER_TIP
    assert up is dialog.up_button and down is dialog.down_button


def test_move_up_and_down_and_ends(qapp):
    dialog = make(qapp)
    up, down = dialog.up_button, dialog.down_button
    dialog.list_widget.setCurrentRow(0)
    assert not up.isEnabled() and down.isEnabled()
    dialog.list_widget.setCurrentRow(2)
    assert up.isEnabled() and down.isEnabled()
    up.click()
    assert dialog.column_order() == ["filename", "title", "path", "artist", "year"]
    assert dialog.list_widget.currentRow() == 1
    down.click()
    down.click()
    assert dialog.column_order() == ["filename", "path", "artist", "title", "year"]
    dialog.list_widget.setCurrentRow(4)
    assert up.isEnabled() and not down.isEnabled()
    down.click()  # no-op at the end
    assert dialog.column_order()[-1] == "year"
    for _ in range(10):
        if up.isEnabled():
            up.click()
    assert dialog.column_order()[0] == "year"
    assert not up.isEnabled() and down.isEnabled()


def test_protected_column_is_checked_not_toggleable_but_movable(qapp):
    dialog = make(qapp, hidden={"filename", "year"})
    item = dialog.list_widget.item(0)
    assert item.checkState() == Qt.CheckState.Checked
    assert not (item.flags() & Qt.ItemFlag.ItemIsUserCheckable)
    assert "filename" not in dialog.hidden_fields()
    dialog.list_widget.setCurrentRow(0)
    dialog.down_button.click()
    assert dialog.column_order()[1] == "filename"


def test_hidden_fields_and_checking(qapp):
    dialog = make(qapp, hidden={"title"})
    assert dialog.hidden_fields() == {"title"}
    dialog.list_widget.item(3).setCheckState(Qt.CheckState.Unchecked)
    dialog.list_widget.item(2).setCheckState(Qt.CheckState.Checked)
    assert dialog.hidden_fields() == {"artist"}
    # hidden state follows the row when it moves
    dialog.list_widget.setCurrentRow(3)
    dialog.up_button.click()
    assert dialog.hidden_fields() == {"artist"}
    assert dialog.column_order()[2] == "artist"


def test_backward_compatible_call_without_order_use(qapp):
    dialog = ColumnSettingsDialog(COLS, {"year"}, PROTECTED, None)
    assert dialog.hidden_fields() == {"year"}
    dialog2 = ColumnSettingsDialog(COLS, set())
    assert dialog2.hidden_fields() == set()


def _drop(qapp, dialog, source_row, target_row, lower_half=False):
    """Select source_row with a real mouse press, then deliver the drag-move
    and drop events a native drag would (the offscreen platform cannot run a
    native QDrag, so the events are delivered by hand)."""
    lw = dialog.list_widget
    viewport = lw.viewport()
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     lw.visualItemRect(lw.item(source_row)).center())
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                       lw.visualItemRect(lw.item(source_row)).center())
    assert lw.currentRow() == source_row
    model = lw.model()
    mime = model.mimeData([model.index(source_row, 0)])
    if target_row >= lw.count():
        pos = QPoint(10, viewport.height() - 2)
    else:
        rect = lw.visualItemRect(lw.item(target_row))
        pos = QPoint(rect.center().x(), rect.bottom() - 2 if lower_half else rect.top() + 2)
    buttons, mods = Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    action = Qt.DropAction.MoveAction
    lw.dragEnterEvent(QDragEnterEvent(pos, action, mime, buttons, mods))
    lw.dragMoveEvent(QDragMoveEvent(pos, action, mime, buttons, mods))
    lw.dropEvent(QDropEvent(QPointF(pos), action, mime, buttons, mods))
    qapp.processEvents()


def test_drag_and_drop_row_up(qapp):
    dialog = make(qapp, hidden={"artist"})
    _drop(qapp, dialog, 3, 1)
    assert dialog.column_order() == ["filename", "artist", "path", "title", "year"]
    assert dialog.hidden_fields() == {"artist"}  # the check state travels with the row
    assert dialog.list_widget.currentRow() == 1
    assert dialog.up_button.isEnabled() and dialog.down_button.isEnabled()


def test_drag_and_drop_row_down_and_to_end(qapp):
    dialog = make(qapp)
    _drop(qapp, dialog, 1, 3, lower_half=True)
    assert dialog.column_order() == ["filename", "title", "artist", "path", "year"]
    _drop(qapp, dialog, 0, 5)
    assert dialog.column_order() == ["title", "artist", "path", "year", "filename"]
    assert not dialog.down_button.isEnabled()
    assert dialog.list_widget.count() == 5


def test_drag_and_drop_onto_itself_changes_nothing(qapp):
    dialog = make(qapp)
    _drop(qapp, dialog, 2, 2)
    assert dialog.column_order() == [k for k, _l in COLS]


def make_table(keys):
    table = QTableWidget(2, len(keys))
    table.horizontalHeader().setSectionsMovable(True)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    return table


def test_visual_keys_and_apply_order(qapp):
    keys = [k for k, _l in COLS]
    table = make_table(keys)
    header = table.horizontalHeader()
    logical_of = {k: i for i, k in enumerate(keys)}
    header.moveSection(3, 1)  # user drags artist to the second place
    assert visual_column_keys(header, keys) == ["filename", "artist", "path", "title", "year"]
    table.setColumnHidden(2, True)
    table.setColumnWidth(4, 77)
    assert apply_column_order(header, ["year", "filename", "title", "path", "artist"], logical_of)
    assert visual_column_keys(header, keys) == ["year", "filename", "title", "path", "artist"]
    assert table.isColumnHidden(2) and table.columnWidth(4) == 77
    assert not apply_column_order(header, ["year", "filename", "title", "path", "artist"], logical_of)
    # unknown keys are ignored, unnamed ones keep their relative order afterwards
    apply_column_order(header, ["nope", "artist"], logical_of)
    assert visual_column_keys(header, keys)[0] == "artist"

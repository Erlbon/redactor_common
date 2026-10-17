"""Shared drag-to-reorder for side-panel fields: core logic, grips, a real mouse drag on a grip, keyboard
moves, persistence/restore, unknown keys, reset, tab order, and the two layout helpers."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QKeyEvent, QMouseEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QApplication, QFormLayout, QGridLayout, QLabel, QLineEdit, QVBoxLayout, QWidget,
)

from redactor_common.core.field_order import (
    decode_order, encode_order, merge_field_order, merge_visible_order, move_key,
)
from redactor_common.gui import field_order as fo
from redactor_common.gui.field_order import (
    FieldOrderController, FieldRow, reorder_form_rows, reorder_grid_rows,
)

KEYS = ["type", "title", "sort", "desc", "genre"]


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# -- core ----------------------------------------------------------------------------------------------

def test_merge_field_order_basics():
    assert merge_field_order([], KEYS) == KEYS
    assert merge_field_order(["genre", "title"], KEYS) == ["type", "genre", "title", "sort", "desc"]
    assert merge_field_order(["desc", "type"], ["type", "title", "desc"]) == ["desc", "type", "title"]


def test_merge_drops_unknown_and_places_new_keys_after_their_default_neighbour():
    saved = ["genre", "title", "gone", "type", "desc"]  # 'sort' is new (added in a later version), 'gone' removed
    merged = merge_field_order(saved, KEYS)
    assert "gone" not in merged
    assert merged == ["genre", "title", "sort", "type", "desc"]  # sort goes right after its default predecessor title
    assert merge_field_order(["title"], ["new_first", "title"]) == ["new_first", "title"]
    assert merge_field_order(["a", "a", "b"], ["a", "b"]) == ["a", "b"]


def test_move_key():
    assert move_key(KEYS, "genre", 0) == ["genre", "type", "title", "sort", "desc"]
    assert move_key(KEYS, "type", 4) == ["title", "sort", "desc", "genre", "type"]
    assert move_key(KEYS, "title", 99)[-1] == "title"
    assert move_key(KEYS, "nope", 1) == KEYS


def test_merge_visible_order_keeps_hidden_keys_in_place():
    saved = ["type", "title", "sort", "desc", "genre"]
    # only title, desc, genre visible; the user puts genre first among them
    assert merge_visible_order(saved, KEYS, ["genre", "title", "desc"]) == ["type", "genre", "sort", "title", "desc"]


def test_encode_decode():
    assert decode_order(encode_order(["a", "b"])) == ["a", "b"]
    assert decode_order("") == [] and decode_order(None) == [] and decode_order(" a, ,b ") == ["a", "b"]


# -- a form panel -----------------------------------------------------------------------------------------

class Store:
    def __init__(self):
        self.order: list[str] = []
        self.changes = 0

    def load(self):
        return list(self.order)

    def save(self, order):
        self.order = list(order)
        self.changes += 1


class FormPanel(QWidget):
    def __init__(self, store, keys=KEYS):
        super().__init__()
        self.keys = list(keys)
        self.form = QFormLayout()
        outer = QVBoxLayout(self)
        container = QWidget()
        container.setLayout(self.form)
        outer.addWidget(container)
        self.container = container
        self.rows = {}
        for key in keys:
            label, edit = QLabel(key), QLineEdit()
            edit.setObjectName(key)
            self.form.addRow(label, edit)
            self.rows[key] = (label, edit)
        self.ctl = FieldOrderController(
            container,
            get_rows=lambda: [FieldRow(k, [lbl, ed]) for k, (lbl, ed) in self.rows.items()],
            apply_order=lambda order: reorder_form_rows(self.form, self.rows, order),
            default_order=lambda: list(self.keys),
            load_order=store.load, save_order=store.save,
        )
        self.ctl.apply_saved()

    def shown_order(self):
        return [self.form.itemAt(i, QFormLayout.ItemRole.FieldRole).widget().objectName()
                for i in range(self.form.rowCount())]


def make_panel(qapp, store=None, keys=KEYS):
    store = store or Store()
    panel = FormPanel(store, keys)
    panel.resize(360, 400)
    panel.show()
    qapp.processEvents()
    panel.ctl.refresh()
    qapp.processEvents()
    return panel, store


def drag_grip(qapp, panel, key, to_key, below=False):
    """A real mouse press / move / release on the grip: through QTest for press and release, and QMouseEvents
    with the left button held for the moves (QTest.mouseMove sends none)."""
    grip = panel.ctl.grip(key)
    assert grip is not None and grip.isVisible()
    target_edit = panel.rows[to_key][1]
    target_global = target_edit.mapToGlobal(QPoint(5, target_edit.height() - 1 if below else 1))
    start_local = QPoint(grip.width() // 2, grip.height() // 2)
    QTest.mousePress(grip, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start_local)
    for global_point in (grip.mapToGlobal(start_local) + QPoint(0, 12), target_global):
        local = grip.mapFromGlobal(global_point)
        event = QMouseEvent(QEvent.Type.MouseMove, QPointF(local), QPointF(global_point),
                            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(grip, event)
    assert panel.ctl._indicator.isVisible()  # the drop-indicator line shows while dragging
    QTest.mouseRelease(grip, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                       grip.mapFromGlobal(target_global))
    qapp.processEvents()


def test_every_visible_row_has_a_grip_with_cursor_and_tooltip(qapp):
    panel, _store = make_panel(qapp)
    for key in KEYS:
        grip = panel.ctl.grip(key)
        assert grip.isVisible() and grip.toolTip() == "Drag to move this field"
        assert grip.cursor().shape() == Qt.CursorShape.SizeAllCursor
        assert grip.width() == fo.GRIP_WIDTH == 14
    tops = [panel.ctl.grip(k).geometry().top() for k in KEYS]
    assert tops == sorted(tops)


def test_real_mouse_drag_on_the_grip_moves_the_row_and_saves(qapp):
    panel, store = make_panel(qapp)
    edit = panel.rows["genre"][1]
    edit.setText("typed value")  # state must stay with the widget
    drag_grip(qapp, panel, "genre", "title")
    assert panel.shown_order() == ["type", "genre", "title", "sort", "desc"]
    assert store.order == ["type", "genre", "title", "sort", "desc"]
    assert panel.rows["genre"][1] is edit and edit.text() == "typed value"
    # grips follow their rows
    tops = [panel.ctl.grip(k).geometry().top() for k in ["type", "genre", "title", "sort", "desc"]]
    assert tops == sorted(tops)
    drag_grip(qapp, panel, "type", "desc", below=True)  # to the very end
    assert panel.shown_order() == ["genre", "title", "sort", "desc", "type"]


def test_dragging_in_an_input_does_not_move_rows_and_still_selects_text(qapp):
    panel, store = make_panel(qapp)
    edit = panel.rows["title"][1]
    edit.setText("hello world")
    QTest.mousePress(edit, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(3, 5))
    QTest.mouseRelease(edit, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(80, 80))
    assert panel.shown_order() == KEYS and store.order == []


def test_alt_up_down_moves_the_focused_row_and_keeps_focus(qapp):
    panel, store = make_panel(qapp)
    edit = panel.rows["sort"][1]
    edit.setFocus()
    qapp.processEvents()

    def press(widget, key, modifiers):
        event = QKeyEvent(QEvent.Type.KeyPress, key, modifiers)
        QApplication.sendEvent(widget, event)
        return event.isAccepted()

    assert press(edit, Qt.Key.Key_Up, Qt.KeyboardModifier.AltModifier)
    assert panel.shown_order() == ["type", "sort", "title", "desc", "genre"]
    assert store.order == ["type", "sort", "title", "desc", "genre"]
    assert press(edit, Qt.Key.Key_Down, Qt.KeyboardModifier.AltModifier)
    assert press(edit, Qt.Key.Key_Down, Qt.KeyboardModifier.AltModifier)
    assert panel.shown_order() == ["type", "title", "desc", "sort", "genre"]
    assert edit.hasFocus()
    # Ctrl+Alt (Norwegian AltGr) and plain Up/Down never move a row
    before = list(store.order)
    press(edit, Qt.Key.Key_Up, Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.ControlModifier)
    press(edit, Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier)
    assert store.order == before


def test_alt_up_at_the_top_is_consumed_without_change(qapp):
    panel, store = make_panel(qapp)
    edit = panel.rows["type"][1]
    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Up, Qt.KeyboardModifier.AltModifier)
    QApplication.sendEvent(edit, event)
    assert panel.shown_order() == KEYS and store.changes == 0


def test_tab_order_follows_the_new_visual_order(qapp):
    panel, _store = make_panel(qapp)
    panel.ctl.move("genre", 0)
    qapp.processEvents()
    order = panel.shown_order()
    editors = [panel.rows[k][1] for k in order]
    for first, second in zip(editors, editors[1:]):
        widget = first.nextInFocusChain()
        while widget is not None and widget not in editors:
            widget = widget.nextInFocusChain()
        assert widget is second


def test_restore_in_a_second_panel_and_unknown_key(qapp):
    panel, store = make_panel(qapp)
    panel.ctl.move("genre", 1)
    assert store.order == ["type", "genre", "title", "sort", "desc"]
    second, _ = make_panel(qapp, store)
    assert second.shown_order() == ["type", "genre", "title", "sort", "desc"]
    # a later version adds a field 'extra' right after 'title' in the default order
    newer, _ = make_panel(qapp, store, keys=["type", "title", "extra", "sort", "desc", "genre"])
    assert newer.shown_order() == ["type", "genre", "title", "extra", "sort", "desc"]


def test_reset_restores_the_default_order(qapp):
    panel, store = make_panel(qapp)
    panel.ctl.move("desc", 0)
    assert panel.shown_order() != KEYS
    panel.ctl.reset()
    assert panel.shown_order() == KEYS and store.order == []
    second, _ = make_panel(qapp, store)
    assert second.shown_order() == KEYS


def test_hidden_rows_keep_their_place_when_the_visible_ones_move(qapp):
    panel, store = make_panel(qapp)
    for widget in panel.rows["sort"]:
        widget.setVisible(False)  # e.g. a content-type filter or a hidden column
    qapp.processEvents()
    panel.ctl.refresh()
    assert panel.ctl.visible_keys() == ["type", "title", "desc", "genre"]
    assert panel.ctl.grip("sort").isHidden()
    panel.ctl.move("genre", 1)
    assert store.order == ["type", "genre", "sort", "title", "desc"]  # 'sort' (hidden) keeps its slot
    for widget in panel.rows["sort"]:
        widget.setVisible(True)
    qapp.processEvents()
    panel.ctl.refresh()
    assert panel.shown_order() == ["type", "genre", "sort", "title", "desc"]


def test_context_menu_has_reset(qapp, monkeypatch):
    from PyQt6.QtWidgets import QMenu
    panel, store = make_panel(qapp)
    panel.ctl.move("genre", 0)
    texts = []
    monkeypatch.setattr(QMenu, "exec", lambda self, *a: texts.extend(a_.text() for a_ in self.actions()))
    panel.ctl.show_reset_menu(QPoint(0, 0))
    assert texts == ["Reset Field Order"]


# -- grid helper ---------------------------------------------------------------------------------------------

def test_reorder_grid_rows_keeps_columns_and_row_slots(qapp):
    box = QWidget()
    grid = QGridLayout(box)
    rows = {}
    for r, key in enumerate(["a", "b", "c"]):
        cb, label, edit = QLineEdit(), QLabel(key), QLineEdit()
        grid.addWidget(cb, r, 0)
        grid.addWidget(label, r, 1)
        grid.addWidget(edit, r, 2)
        rows[key] = [cb, label, edit]

    def position(widget):
        r, c, _rs, _cs = grid.getItemPosition(grid.indexOf(widget))
        return r, c

    reorder_grid_rows(grid, rows, ["c", "a", "b"])
    assert [position(rows[k][1]) for k in ("c", "a", "b")] == [(0, 1), (1, 1), (2, 1)]
    assert [position(rows[k][2]) for k in ("c", "a", "b")] == [(0, 2), (1, 2), (2, 2)]
    assert [position(rows[k][0]) for k in ("c", "a", "b")] == [(0, 0), (1, 0), (2, 0)]


def test_reset_all_hook_replaces_the_single_container_reset(qapp):
    panel, store = make_panel(qapp)
    calls = []
    panel.ctl._reset_all = lambda: calls.append("all")
    panel.ctl.move("genre", 0)
    panel.ctl.reset()
    assert calls == ["all"] and panel.shown_order()[0] == "genre"  # the hook decides what resets
    panel.ctl.reset_this()
    assert panel.shown_order() == KEYS

"""
redactor_common/gui/field_order.py

Drag-to-reorder for the fields of a side panel, shared by all four Redactor apps.

Every visible field row gets a small grip ("vertical dots", cursor "size all", tooltip "Drag to move
this field") at its left. Press the grip, drag, release: the row moves, with a drop-indicator line
while dragging. ONLY the grip starts a drag, so typing and selecting in the input widgets are never
affected. Keyboard alternative: Alt+Up / Alt+Down in any widget of a row moves that row (never
Ctrl+Alt: on a Norwegian keyboard AltGr is Ctrl+Alt). The order is saved by field key (see
core/field_order.py) through the app's own load/save callables, and "Reset Field Order" sits in the
right-click menu of a grip and of the panel's empty area.

The facility is layout-agnostic: the app describes its rows (FieldRow: key + widgets) and says how to
lay them out in a given order (`apply_order`, usually reorder_form_rows / reorder_grid_rows below),
so values, "mixed" state and "touched" markers stay attached to the widgets, which are only moved,
never rebuilt. The grips are overlays on the container (they take no layout cell): the controller
adds GRIP_WIDTH to the left margin of the container's layout once.

Usage (a QFormLayout panel):
    self._field_order = FieldOrderController(
        self.form_container,
        get_rows=lambda: [FieldRow(key, [label, widget]) for key, (label, widget) in self._rows.items()],
        apply_order=lambda order: reorder_form_rows(self.form_layout, self._rows, order),
        default_order=lambda: list(FIELD_LABELS),
        load_order=load_saved_order, save_order=save_order,
    )
    # after every (re)build of the rows:  self._field_order.refresh()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from PyQt6.QtCore import QEvent, QObject, QPoint, QRect, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication, QFormLayout, QFrame, QGridLayout, QLabel, QMenu, QWidget

from redactor_common.core.field_order import merge_field_order, merge_visible_order, move_key

GRIP_WIDTH = 14
GRIP_GLYPH = "⋮⋮"
GRIP_TIP = "Drag to move this field"
RESET_TEXT = "Reset Field Order"


@dataclass
class FieldRow:
    """One field of the panel. `widgets` are every widget of the row in tab order (label, editor,
    buttons...); the first one that is visible gives the row its vertical position."""
    key: str
    widgets: list[QWidget] = field(default_factory=list)


class FieldGrip(QLabel):
    """The drag handle of one row. Reports press / move / release to the controller."""

    def __init__(self, key: str, controller: "FieldOrderController", parent: QWidget):
        super().__init__(GRIP_GLYPH, parent)
        self.key = key
        self._controller = controller
        self._pressed_at: QPoint | None = None
        self._dragging = False
        self.setObjectName("fieldGrip")
        self.setToolTip(GRIP_TIP)
        self.setAccessibleName(GRIP_TIP)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedWidth(GRIP_WIDTH)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda pos: controller.show_reset_menu(self.mapToGlobal(pos)))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed_at = event.globalPosition().toPoint()
            self._dragging = False
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._pressed_at is None or not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        here = event.globalPosition().toPoint()
        if not self._dragging:
            if (here - self._pressed_at).manhattanLength() < QApplication.startDragDistance():
                return
            self._dragging = True
            self._controller.drag_started(self.key)
        self._controller.drag_moved(self.key, here)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pressed_at is not None:
            was_dragging = self._dragging
            self._pressed_at = None
            self._dragging = False
            if was_dragging:
                self._controller.drag_finished(self.key, event.globalPosition().toPoint())
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class FieldOrderController(QObject):
    """Grips, drag handling, keyboard moves, persistence and reset for one panel container."""

    def __init__(
        self,
        container: QWidget,
        get_rows: Callable[[], list[FieldRow]],
        apply_order: Callable[[list[str]], None],
        default_order: Callable[[], list[str]],
        load_order: Callable[[], list[str]],
        save_order: Callable[[list[str]], None],
        on_changed: Callable[[list[str]], None] | None = None,
        reset_all: Callable[[], None] | None = None,
    ):
        super().__init__(container)
        self._container = container
        self._get_rows = get_rows
        self._apply_order = apply_order
        self._default_order = default_order
        self._load_order = load_order
        self._save_order = save_order
        self._on_changed = on_changed
        self._reset_all = reset_all  # a panel with several containers resets them all together
        self._grips: dict[str, FieldGrip] = {}
        self._margin_reserved = False
        self._drag_slot: int | None = None
        self._indicator = QFrame(container)
        self._indicator.setObjectName("fieldDropIndicator")
        self._indicator.setFixedHeight(2)
        self._indicator.setStyleSheet("background-color: palette(highlight);")
        self._indicator.hide()
        container.installEventFilter(self)
        container.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        container.customContextMenuRequested.connect(
            lambda pos: self.show_reset_menu(container.mapToGlobal(pos)))

    # -- order ---------------------------------------------------------------------------------

    def saved_order(self) -> list[str]:
        """The full order: the saved one merged with the default (new keys where the default puts them)."""
        return merge_field_order(self._load_order(), self._default_order())

    def apply_saved(self) -> None:
        """Lay the rows out in the saved order (call once after building the rows)."""
        self._apply_order(self.saved_order())
        self.refresh()

    def visible_keys(self) -> list[str]:
        return [row.key for row in self._visible_rows()]

    def move(self, key: str, target_index: int) -> bool:
        """Move `key` so that it is at `target_index` among the VISIBLE rows; saves and applies."""
        visible = self.visible_keys()
        if key not in visible:
            return False
        new_visible = move_key(visible, key, target_index)
        if new_visible == visible:
            return False
        full = merge_visible_order(self._load_order(), self._default_order(), new_visible)
        self._apply_order(full)
        self._save_order(full)
        self.refresh()
        if self._on_changed:
            self._on_changed(full)
        return True

    def reset(self) -> None:
        """Back to the default order (and forget the saved one)."""
        if self._reset_all is not None:
            self._reset_all()
            return
        self.reset_this()

    def reset_this(self) -> None:
        """Reset only this container (what `reset_all` calls on each of a panel's containers)."""
        defaults = list(self._default_order())
        self._apply_order(defaults)
        self._save_order([])
        self.refresh()
        if self._on_changed:
            self._on_changed(defaults)

    def show_reset_menu(self, global_pos: QPoint) -> None:
        menu = QMenu(self._container)
        menu.addAction(RESET_TEXT, self.reset)
        menu.exec(global_pos)

    # -- grips and geometry -----------------------------------------------------------------------

    def refresh(self) -> None:
        """Call after every (re)build or reorder of the rows: positions the grips, keeps the keyboard
        shortcut filter on every row widget, and makes the tab order follow the visual order."""
        self._reserve_margin()
        rows = self._get_rows()
        live = {row.key for row in rows}
        for key in list(self._grips):
            if key not in live:
                self._grips.pop(key).deleteLater()
        for row in rows:
            if row.key not in self._grips:
                self._grips[row.key] = FieldGrip(row.key, self, self._container)
            for widget in row.widgets:
                widget.installEventFilter(self)
                for child in widget.findChildren(QWidget):
                    child.installEventFilter(self)
        self._place_grips()
        self._fix_tab_order()

    def grip(self, key: str) -> FieldGrip | None:
        return self._grips.get(key)

    def _reserve_margin(self) -> None:
        layout = self._container.layout()
        if layout is None or self._margin_reserved:
            return
        left, top, right, bottom = layout.getContentsMargins()
        layout.setContentsMargins(left + GRIP_WIDTH, top, right, bottom)
        self._margin_reserved = True

    def _row_rect(self, row: FieldRow) -> QRect | None:
        rect: QRect | None = None
        for widget in row.widgets:
            if widget.isHidden() or widget.width() <= 0:
                continue
            top_left = widget.mapTo(self._container, QPoint(0, 0))
            r = QRect(top_left, widget.size())
            rect = r if rect is None else rect.united(r)
        return rect

    def _visible_rows(self) -> list[FieldRow]:
        rows = [(self._row_rect(row), row) for row in self._get_rows()]
        shown = [(rect, row) for rect, row in rows if rect is not None]
        shown.sort(key=lambda pair: pair[0].top())
        return [row for _rect, row in shown]

    def _place_grips(self) -> None:
        for row in self._get_rows():
            grip = self._grips.get(row.key)
            if grip is None:
                continue
            rect = self._row_rect(row)
            if rect is None:
                grip.hide()
                continue
            height = max(16, min(rect.height(), 28))
            grip.setGeometry(0, rect.top() + max(0, (rect.height() - height) // 2), GRIP_WIDTH, height)
            grip.show()
            grip.raise_()

    def _fix_tab_order(self) -> None:
        chain: list[QWidget] = []
        for row in self._visible_rows():
            chain.extend(w for w in row.widgets if w.focusPolicy() != Qt.FocusPolicy.NoFocus and not w.isHidden())
        for first, second in zip(chain, chain[1:]):
            QWidget.setTabOrder(first, second)

    # -- dragging ------------------------------------------------------------------------------------

    def drag_started(self, key: str) -> None:
        self._drag_slot = None

    def _slot_for(self, global_pos: QPoint) -> tuple[int, int]:
        """(slot, indicator_y): the insertion slot (0..n, counted among the visible rows including the
        dragged one) for the pointer, and where to draw the line."""
        y = self._container.mapFromGlobal(global_pos).y()
        rects = [self._row_rect(row) for row in self._visible_rows()]
        rects = [r for r in rects if r is not None]
        slot = sum(1 for r in rects if r.center().y() < y)
        if not rects:
            return 0, 0
        line_y = rects[slot].top() - 1 if slot < len(rects) else rects[-1].bottom() + 1
        return slot, line_y

    def drag_moved(self, key: str, global_pos: QPoint) -> None:
        slot, line_y = self._slot_for(global_pos)
        self._drag_slot = slot
        self._indicator.setGeometry(GRIP_WIDTH, line_y, max(0, self._container.width() - GRIP_WIDTH), 2)
        self._indicator.show()
        self._indicator.raise_()

    def drag_finished(self, key: str, global_pos: QPoint) -> None:
        self._indicator.hide()
        slot, _line = self._slot_for(global_pos)
        self._drag_slot = None
        visible = self.visible_keys()
        if key not in visible:
            return
        source = visible.index(key)
        target = slot - 1 if source < slot else slot
        self.move(key, target)

    # -- keyboard (Alt+Up / Alt+Down) and layout changes -------------------------------------------------

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        kind = event.type()
        # Never let a stale row (a widget the panel just deleted while rebuilding its form) or a half-torn-down
        # controller raise out of an event filter: an exception escaping a Qt virtual aborts the whole process.
        try:
            if watched is self._container:
                if kind in (QEvent.Type.Resize, QEvent.Type.LayoutRequest, QEvent.Type.Show, QEvent.Type.Move):
                    self._place_grips()
                return False
            if kind == QEvent.Type.KeyPress and isinstance(watched, QWidget):
                if (event.modifiers() == Qt.KeyboardModifier.AltModifier
                        and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down)):
                    if self._move_row_of(watched, -1 if event.key() == Qt.Key.Key_Up else 1):
                        return True
        except (RuntimeError, KeyError, AttributeError):
            # AttributeError: the Python side of a controller whose panel is being torn down can already be gone
            # while Qt still delivers a last event to its C++ side.
            return False
        return False

    def _move_row_of(self, widget: QWidget, delta: int) -> bool:
        for row in self._visible_rows():
            if any(w is widget or w.isAncestorOf(widget) for w in row.widgets):
                visible = self.visible_keys()
                index = visible.index(row.key)
                target = index + delta
                if not 0 <= target < len(visible):
                    return True  # at the end: consumed, nothing to move
                self.move(row.key, target)
                widget.setFocus()
                return True
        return False


# -- layout helpers: put already-built rows into a given order, without rebuilding anything ----------------

def reorder_form_rows(form: QFormLayout, rows: dict[str, tuple[QWidget, QWidget]], order: list[str]) -> None:
    """Re-add the (label, field) rows of a QFormLayout in `order`. Rows not named in `order` (or not in
    `rows`) keep their relative order after the named ones. Widgets are moved, not recreated."""
    named = [k for k in order if k in rows]
    named += [k for k in rows if k not in named]
    while form.rowCount():
        form.takeRow(0)
    for key in named:
        label, widget = rows[key]
        form.addRow(label, widget)


def reorder_grid_rows(grid: QGridLayout, rows: dict[str, list[QWidget]], order: list[str]) -> None:
    """Permute the rows of a QGridLayout: the row slots the fields occupy are kept, the fields take them
    in `order`. Every widget of a field keeps its column and spans."""
    named = [k for k in order if k in rows]
    named += [k for k in rows if k not in named]
    positions: dict[str, list[tuple[QWidget, int, int, int, int]]] = {}
    slots: list[int] = []
    for key in rows:
        entries = []
        for widget in rows[key]:
            index = grid.indexOf(widget)
            if index < 0:
                continue
            row, col, row_span, col_span = grid.getItemPosition(index)
            entries.append((widget, row, col, row_span, col_span))
        positions[key] = entries
        if entries:
            slots.append(min(e[1] for e in entries))
    slots.sort()
    original = {key: (min(e[1] for e in entries) if entries else 0) for key, entries in positions.items()}
    for entries in positions.values():
        for widget, *_rest in entries:
            grid.removeWidget(widget)
    for slot, key in zip(slots, [k for k in named if positions.get(k)]):
        base = original[key]
        for widget, row, col, row_span, col_span in positions[key]:
            grid.addWidget(widget, slot + (row - base), col, row_span, col_span)

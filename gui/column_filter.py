"""
redactor_common/gui/column_filter.py

Per-column filters for a QTableWidget, living in the table's column HEADER (like a spreadsheet's autofilter):
a small funnel is drawn at the right of every filterable header section; clicking it opens a popup with a
checklist of the distinct values in that column (plus a "contains..." box for text-like columns); a column
whose filter is active gets an accent-tinted header, so one glance shows which columns are filtering.

Two layers, so the logic is testable without a display:

- ColumnFilters (pure Python): {column key: allowed values and/or a "contains" text}, `matches(row_values)`,
  `active_columns()`, `clear()`. Keys are anything hashable (an app's field names, or column indexes).
- ColumnFilterHeader (a QHeaderView): paints the funnel and the active tint, swallows a click on the funnel
  (so it neither sorts nor starts a column drag; a click anywhere else on the section behaves as before, and
  the resize grip at the section edge is left alone), opens the popup, emits `filtersChanged`.

Rows hidden by a filter stay in the table (setRowHidden, the same safe approach as mp3redactor's Filter List:
sorting keeps hidden rows with their files). The app decides what "all rows" means for its own actions; the
helpers here (`apply_to_table`, `visible_rows`, `is_filtering`) just tell it which rows are shown. Selected
rows that a filter hides are deselected by `apply_to_table`, so a batch action only reaches visible rows.

Usage:
    header = ColumnFilterHeader(self.table)
    self.table.setHorizontalHeader(header)            # before setSortingEnabled / signal wiring
    header.configure(lambda col: self._column_order[col], {"filename": "text", "status": "values", ...})
    header.filtersChanged.connect(self._on_filters_changed)   # -> header.apply_to_table(), status text
    ... after every table rebuild: header.apply_to_table()
"""

from __future__ import annotations

from typing import Callable, Hashable, Iterable, Mapping, Optional

from PyQt6.QtCore import QItemSelectionModel, QPointF, QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPalette
from PyQt6.QtWidgets import (
    QHBoxLayout, QHeaderView, QLineEdit, QListWidget, QListWidgetItem, QMenu, QPushButton, QStyle,
    QStyleOptionHeader, QTableWidget, QVBoxLayout, QWidget, QWidgetAction,
)

KIND_VALUES = "values"  # checklist of the distinct values only
KIND_TEXT = "text"      # a "contains..." box above the checklist
BLANK_LABEL = "(blank)"

FUNNEL_SIZE = 14        # the funnel glyph's box
FUNNEL_MARGIN = 6       # gap to the section's right edge (keeps the resize grip clear)
FUNNEL_STRIP = FUNNEL_SIZE + FUNNEL_MARGIN + 2   # width reserved on the right of a filterable section
MIN_SECTION_FOR_FUNNEL = 48


# --------------------------------------------------------------------------- pure logic

class ColumnFilters:
    """Which rows each column lets through. A column's filter has an allowed-value set (None = every value
    allowed) and a case-insensitive "contains" text ("" = none); both must pass. A row passes when every
    filtered column passes. Values are the cells' text."""

    def __init__(self) -> None:
        self._allowed: dict[Hashable, frozenset[str]] = {}
        self._contains: dict[Hashable, str] = {}

    # -- setting
    def set_allowed(self, key: Hashable, values: Optional[Iterable[str]]) -> None:
        """Only rows whose value is in `values` pass; None removes the value filter."""
        if values is None:
            self._allowed.pop(key, None)
        else:
            self._allowed[key] = frozenset(values)

    def set_contains(self, key: Hashable, text: str) -> None:
        text = (text or "").strip()
        if text:
            self._contains[key] = text
        else:
            self._contains.pop(key, None)

    def clear(self, key: Hashable | None = None) -> None:
        """Drops one column's filter, or every filter when `key` is None."""
        if key is None:
            self._allowed.clear()
            self._contains.clear()
        else:
            self._allowed.pop(key, None)
            self._contains.pop(key, None)

    # -- reading
    def allowed(self, key: Hashable) -> Optional[frozenset[str]]:
        return self._allowed.get(key)

    def contains(self, key: Hashable) -> str:
        return self._contains.get(key, "")

    def is_active(self, key: Hashable) -> bool:
        return key in self._allowed or key in self._contains

    def active_columns(self) -> list[Hashable]:
        out = list(self._allowed)
        out.extend(k for k in self._contains if k not in self._allowed)
        return out

    def any_active(self) -> bool:
        return bool(self._allowed or self._contains)

    def matches(self, row_values: Mapping[Hashable, str]) -> bool:
        """True when the row passes every active column filter. A key missing from `row_values` counts as ""."""
        for key, allowed in self._allowed.items():
            if (row_values.get(key) or "") not in allowed:
                return False
        for key, needle in self._contains.items():
            if needle.casefold() not in (row_values.get(key) or "").casefold():
                return False
        return True

    def describe(self, label_for: Callable[[Hashable], str] = str) -> str:
        """Short text for a status line, e.g. 'Content Type, Year'."""
        return ", ".join(label_for(k) for k in self.active_columns())


def distinct_values(values: Iterable[str]) -> list[str]:
    """The checklist entries for a column: distinct, case-insensitively sorted, blank last."""
    seen = set(values)
    return sorted((v for v in seen if v), key=lambda v: (v.casefold(), v)) + ([""] if "" in seen else [])


# --------------------------------------------------------------------------- popup

class ColumnFilterPopup(QWidget):
    """The popup body: optional contains box, Select all / Clear filter, and the checklist. Every change is
    reported at once through `on_change(allowed, contains)` (allowed None = every value ticked); `on_clear`
    is called by the Clear filter button."""

    def __init__(
        self, values: list[str], allowed: Optional[Iterable[str]], contains: str, text_mode: bool,
        on_change: Callable[[Optional[set[str]], str], None], on_clear: Callable[[], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._values = list(values)
        self._on_change = on_change
        self._on_clear = on_clear
        self._building = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self.contains_edit: QLineEdit | None = None
        if text_mode:
            self.contains_edit = QLineEdit(contains)
            self.contains_edit.setPlaceholderText("contains…")
            self.contains_edit.setClearButtonEnabled(True)
            self.contains_edit.textChanged.connect(lambda _t: self._changed())
            layout.addWidget(self.contains_edit)

        buttons = QHBoxLayout()
        self.select_all_button = QPushButton("Select all")
        self.clear_button = QPushButton("Clear filter")
        for button in (self.select_all_button, self.clear_button):
            button.setFlat(True)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.select_all_button.clicked.connect(lambda _c=False: self.select_all())
        self.clear_button.clicked.connect(lambda _c=False: self._on_clear())

        self.list_widget = QListWidget()
        wanted = None if allowed is None else set(allowed)
        for value in self._values:
            item = QListWidgetItem(value or BLANK_LABEL)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setData(Qt.ItemDataRole.UserRole, value)
            ticked = wanted is None or value in wanted
            item.setCheckState(Qt.CheckState.Checked if ticked else Qt.CheckState.Unchecked)
            self.list_widget.addItem(item)
        self.list_widget.itemChanged.connect(lambda _i: self._changed())
        self.list_widget.setMinimumWidth(220)
        self.list_widget.setMaximumHeight(280)
        layout.addWidget(self.list_widget)
        self._building = False

    # -- driving (also used by the tests)
    def items(self) -> list[QListWidgetItem]:
        return [self.list_widget.item(i) for i in range(self.list_widget.count())]

    def checked_values(self) -> set[str]:
        return {it.data(Qt.ItemDataRole.UserRole) for it in self.items() if it.checkState() == Qt.CheckState.Checked}

    def set_checked_values(self, values: Iterable[str]) -> None:
        """Ticks exactly `values` (one change report at the end)."""
        wanted = set(values)
        self._building = True
        for it in self.items():
            it.setCheckState(
                Qt.CheckState.Checked if it.data(Qt.ItemDataRole.UserRole) in wanted else Qt.CheckState.Unchecked
            )
        self._building = False
        self._changed()

    def select_all(self) -> None:
        self.set_checked_values(self._values)

    def set_contains_text(self, text: str) -> None:
        if self.contains_edit is not None:
            self.contains_edit.setText(text)

    def _changed(self) -> None:
        if self._building:
            return
        ticked = self.checked_values()
        allowed = None if len(ticked) == len(self._values) else ticked
        contains = self.contains_edit.text() if self.contains_edit is not None else ""
        self._on_change(allowed, contains)


# --------------------------------------------------------------------------- header

class ColumnFilterHeader(QHeaderView):
    filtersChanged = pyqtSignal()

    def __init__(self, table: QTableWidget) -> None:
        super().__init__(Qt.Orientation.Horizontal, table)
        self._table = table
        self.filters = ColumnFilters()
        self._key_for_column: Callable[[int], Hashable] = lambda col: col
        self._kinds: dict[Hashable, str] = {}
        self._funnel_down: int | None = None
        self.setSectionsClickable(True)
        self.setHighlightSections(False)

    # -- configuration -------------------------------------------------------
    def configure(self, key_for_column: Callable[[int], Hashable], kinds: Mapping[Hashable, str]) -> None:
        """`key_for_column(logical index)` names a column (an app's field name); `kinds` maps the keys that get
        a funnel to KIND_VALUES or KIND_TEXT."""
        self._key_for_column = key_for_column
        self._kinds = dict(kinds)
        self.viewport().update()

    def key_of(self, column: int) -> Hashable | None:
        try:
            return self._key_for_column(column)
        except (IndexError, KeyError):
            return None

    def kind_of(self, column: int) -> str | None:
        key = self.key_of(column)
        return self._kinds.get(key) if key is not None else None

    def column_of(self, key: Hashable) -> int | None:
        for col in range(self.count()):
            if self.key_of(col) == key:
                return col
        return None

    def is_filterable(self, column: int) -> bool:
        return self.kind_of(column) is not None and self.sectionSize(column) >= MIN_SECTION_FOR_FUNNEL \
            and not self.isSectionHidden(column)

    def is_active(self, column: int) -> bool:
        key = self.key_of(column)
        return key is not None and self.filters.is_active(key)

    def active_keys(self) -> list[Hashable]:
        return self.filters.active_columns()

    # -- geometry ------------------------------------------------------------
    def funnel_rect(self, column: int) -> QRect | None:
        """The funnel's rect in viewport coordinates, or None when this column has no funnel."""
        if not self.is_filterable(column):
            return None
        right = self.sectionViewportPosition(column) + self.sectionSize(column)
        top = (self.height() - FUNNEL_SIZE) // 2
        return QRect(right - FUNNEL_MARGIN - FUNNEL_SIZE, top, FUNNEL_SIZE, FUNNEL_SIZE)

    def funnel_at(self, pos) -> int | None:
        """The column whose funnel is under `pos` (viewport coordinates), else None. A few pixels of slack
        around the glyph make it easy to hit."""
        col = self.logicalIndexAt(pos)
        if col < 0:
            return None
        rect = self.funnel_rect(col)
        if rect is not None and rect.adjusted(-3, -4, 2, 4).contains(pos):
            return col
        return None

    # -- painting ------------------------------------------------------------
    def paintSection(self, painter: QPainter, rect: QRect, logicalIndex: int) -> None:
        if not painter or not rect.isValid():
            return
        filterable = self.kind_of(logicalIndex) is not None and rect.width() >= MIN_SECTION_FOR_FUNNEL
        if not filterable:
            super().paintSection(painter, rect, logicalIndex)
            return
        # Text and sort arrow lay out in the part left of the funnel strip; the strip gets the same header look.
        painter.save()
        super().paintSection(painter, rect.adjusted(0, 0, -FUNNEL_STRIP, 0), logicalIndex)
        painter.restore()
        strip = QRect(rect.right() + 1 - FUNNEL_STRIP, rect.top(), FUNNEL_STRIP, rect.height())
        opt = QStyleOptionHeader()
        self.initStyleOption(opt)
        opt.rect = strip
        opt.section = logicalIndex
        opt.text = ""
        opt.icon = QIcon()
        opt.sortIndicator = QStyleOptionHeader.SortIndicator.None_
        self.style().drawControl(QStyle.ControlElement.CE_HeaderSection, opt, painter, self)

        active = self.is_active(logicalIndex)
        palette = self.palette()
        accent = palette.color(QPalette.ColorRole.Highlight)
        if active:
            tint = QColor(accent)
            tint.setAlpha(70)
            painter.fillRect(rect, tint)
            painter.fillRect(QRect(rect.left(), rect.bottom() - 2, rect.width(), 3), accent)
        funnel = QRect(rect.right() + 1 - FUNNEL_MARGIN - FUNNEL_SIZE, (rect.height() - FUNNEL_SIZE) // 2 + rect.top(),
                       FUNNEL_SIZE, FUNNEL_SIZE)
        self._paint_funnel(painter, funnel, active, accent, palette)

    @staticmethod
    def _paint_funnel(painter: QPainter, box: QRect, active: bool, accent: QColor, palette: QPalette) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        x, y, w, h = box.x(), box.y(), box.width(), box.height()
        path = QPainterPath()
        path.moveTo(QPointF(x + 1, y + 2))
        path.lineTo(QPointF(x + w - 1, y + 2))
        path.lineTo(QPointF(x + w / 2 + 1.5, y + h * 0.55))
        path.lineTo(QPointF(x + w / 2 + 1.5, y + h - 2))
        path.lineTo(QPointF(x + w / 2 - 1.5, y + h - 3.5))
        path.lineTo(QPointF(x + w / 2 - 1.5, y + h * 0.55))
        path.closeSubpath()
        if active:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(accent)
            painter.drawPath(path)
        else:
            colour = QColor(palette.color(QPalette.ColorRole.ButtonText))
            colour.setAlpha(150)
            painter.setPen(colour)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
        painter.restore()

    # -- mouse: the funnel is its own button -----------------------------------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            col = self.funnel_at(event.position().toPoint())
            if col is not None:
                self._funnel_down = col  # no base call: no section press, so no sort and no drag
                event.accept()
                return
        self._funnel_down = None
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._funnel_down is not None:
            col, self._funnel_down = self._funnel_down, None
            if event.button() == Qt.MouseButton.LeftButton and self.funnel_at(event.position().toPoint()) == col:
                self.open_popup(col)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if self.funnel_at(event.position().toPoint()) is not None:
            event.accept()  # a quick second click on the funnel is not an auto-resize
            return
        super().mouseDoubleClickEvent(event)

    # -- popup ---------------------------------------------------------------
    def column_values(self, column: int) -> list[str]:
        """The distinct cell texts of `column` over EVERY row (hidden ones too), for the checklist."""
        texts = []
        for row in range(self._table.rowCount()):
            item = self._table.item(row, column)
            texts.append(item.text() if item is not None else "")
        return distinct_values(texts)

    def build_popup(self, column: int) -> ColumnFilterPopup | None:
        key = self.key_of(column)
        kind = self.kind_of(column)
        if key is None or kind is None:
            return None
        return ColumnFilterPopup(
            self.column_values(column), self.filters.allowed(key), self.filters.contains(key), kind == KIND_TEXT,
            on_change=lambda allowed, contains, k=key: self._set_filter(k, allowed, contains),
            on_clear=lambda k=key: self._clear_from_popup(k),
            parent=self,
        )

    def open_popup(self, column: int) -> None:
        popup = self.build_popup(column)
        if popup is None:
            return
        menu = QMenu(self)
        action = QWidgetAction(menu)
        action.setDefaultWidget(popup)
        menu.addAction(action)
        self._open_menu = menu  # lets a test find it; cleared when it closes
        menu.aboutToHide.connect(lambda: setattr(self, "_open_menu", None))
        origin = self.mapToGlobal(self.funnel_rect(column).bottomLeft())
        if popup.contains_edit is not None:
            popup.contains_edit.setFocus()
        menu.exec(origin)

    def _clear_from_popup(self, key: Hashable) -> None:
        menu = getattr(self, "_open_menu", None)
        self.clear_filter(key)
        if menu is not None:
            menu.close()

    def _set_filter(self, key: Hashable, allowed: Optional[set[str]], contains: str) -> None:
        self.filters.set_allowed(key, allowed)
        self.filters.set_contains(key, contains)
        self._changed()

    # -- API for the app and the tests -------------------------------------------
    def set_value_filter(self, key: Hashable, allowed: Optional[Iterable[str]]) -> None:
        self.filters.set_allowed(key, allowed)
        self._changed()

    def set_text_filter(self, key: Hashable, text: str) -> None:
        self.filters.set_contains(key, text)
        self._changed()

    def clear_filter(self, key: Hashable) -> None:
        self.filters.clear(key)
        self._changed()

    def clear_all_filters(self) -> None:
        if self.filters.any_active():
            self.filters.clear()
            self._changed()

    def _changed(self) -> None:
        self.viewport().update()
        self.filtersChanged.emit()

    # -- applying to the table ---------------------------------------------------
    def row_values(self, row: int) -> dict[Hashable, str]:
        values: dict[Hashable, str] = {}
        for col in range(self.count()):
            key = self.key_of(col)
            if key is None or not self.filters.is_active(key):
                continue
            item = self._table.item(row, col)
            values[key] = item.text() if item is not None else ""
        return values

    def apply_to_table(self) -> tuple[int, int]:
        """Hides the rows the filters reject, shows the rest, and deselects selected rows that got hidden.
        Returns (shown, total). Re-run after every table rebuild (a rebuild shows all rows again)."""
        total = self._table.rowCount()
        shown = 0
        for row in range(total):
            visible = (not self.filters.any_active()) or self.filters.matches(self.row_values(row))
            self._table.setRowHidden(row, not visible)
            shown += visible
        selection = self._table.selectionModel()
        if selection is not None and self.filters.any_active():
            hidden_selected = [r for r in {i.row() for i in selection.selectedRows()} if self._table.isRowHidden(r)]
            blocked = selection.blockSignals(True)
            try:
                for row in hidden_selected:
                    for col in range(self._table.columnCount()):
                        selection.select(self._table.model().index(row, col), QItemSelectionModel.SelectionFlag.Deselect)
            finally:
                selection.blockSignals(blocked)
            if hidden_selected:
                self._table.itemSelectionChanged.emit()
        return shown, total

    def visible_rows(self) -> list[int]:
        return [r for r in range(self._table.rowCount()) if not self._table.isRowHidden(r)]

    def sizeHint(self) -> QSize:
        return super().sizeHint()

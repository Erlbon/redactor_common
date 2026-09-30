"""
redactor_common/gui/command_palette.py

Ctrl+K command palette: a small dialog with a filter box and a list of every
menu action ('Open Files   File   Ctrl+O'), so any command is reachable
without knowing which menu holds it. Fed by walking the window's menu bar
(menu path shown as 'Metadata ▸ Look Up') plus any hidden actions in the
skeleton's ActionRegistry.

- typing filters: every space-separated word must match (substring of the
  title/path/shortcut/keywords, or, weaker, letters in order inside the title)
- Up/Down move, Enter (or double-click) triggers, Esc closes
- disabled actions are listed greyed and cannot be triggered

    add_command_palette(window, get_action_registry(window))  # -> the Ctrl+K QAction
"""

from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import (
    QDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMenuBar,
    QVBoxLayout,
    QWidget,
)

from redactor_common.core import labels
from redactor_common.gui import standard_shortcuts
from redactor_common.gui.standard_menus import KEY_COMMAND_PALETTE, PATH_SEPARATOR, ActionRegistry


@dataclass
class Command:
    title: str  # 'Open Files' (no mnemonic marker, no ellipsis)
    path: str  # 'File' or 'Metadata ▸ Look Up'; '' for a hidden action
    shortcut: str  # native text, '' if none
    action: QAction
    keywords: str = ""

    @property
    def enabled(self) -> bool:
        return self.action.isEnabled()

    def haystack(self) -> str:
        return f"{self.title} {self.path} {self.shortcut} {self.keywords}".lower()


def _shortcut_text(action: QAction) -> str:
    return ", ".join(s.toString(QKeySequence.SequenceFormat.NativeText) for s in action.shortcuts())


def _walk(menu: QMenu, trail: list[str]):
    for act in menu.actions():
        if act.isSeparator():
            continue
        sub = act.menu()
        if sub is not None:
            yield from _walk(sub, trail + [labels.plain_label(act.text())])
        else:
            yield act, PATH_SEPARATOR.join(trail)


def collect_commands(
    window: QMainWindow, registry: ActionRegistry | None = None, exclude: QAction | None = None
) -> list[Command]:
    """Every action reachable from `window`'s menu bar, in menu order, then
    registry-only (hidden) actions. Read fresh each time the palette opens so
    dynamic text ('Apply to 3 Selected') and enabled state are current."""
    out: list[Command] = []
    seen: set[int] = set()
    menu_bar: QMenuBar = window.menuBar()
    for top in menu_bar.actions():
        menu = top.menu()
        if menu is None:
            continue
        for act, path in _walk(menu, [labels.plain_label(top.text())]):
            if act is exclude or id(act) in seen or not labels.plain_label(act.text()):
                continue
            seen.add(id(act))
            kw = act.data() if isinstance(act.data(), str) else ""
            out.append(Command(labels.plain_label(act.text()), path, _shortcut_text(act), act, kw))
    if registry is not None:
        for entry in registry.entries():
            act = entry.action
            if act is exclude or id(act) in seen or not labels.plain_label(act.text()):
                continue
            seen.add(id(act))
            kw = act.data() if isinstance(act.data(), str) else ""
            out.append(Command(labels.plain_label(act.text()), entry.path, _shortcut_text(act), act, kw))
    return out


def match_score(query: str, command: Command) -> int | None:
    """None = no match; lower is better. Each word must match: a prefix of the
    title scores best, a substring of the title next, a substring elsewhere
    (path/shortcut/keywords) next, letters-in-order inside the title last."""
    words = query.lower().split()
    if not words:
        return 0
    title = command.title.lower()
    hay = command.haystack()
    total = 0
    for w in words:
        if title.startswith(w):
            total += 0
        elif w in title:
            total += 10 + title.index(w)
        elif w in hay:
            total += 100
        else:
            pos = 0
            for ch in w:
                pos = title.find(ch, pos) + 1
                if pos == 0:
                    return None
            total += 1000
    return total


def filter_commands(query: str, commands: list[Command]) -> list[Command]:
    """Commands matching `query`, best first (menu order breaks ties)."""
    scored = [(match_score(query, c), i, c) for i, c in enumerate(commands)]
    return [c for s, i, c in sorted((t for t in scored if t[0] is not None), key=lambda t: (t[0], t[1]))]


class CommandPalette(QDialog):
    """The palette dialog. `show_palette()` refreshes the command list and
    opens it; `visible_commands()` is what the list currently shows."""

    def __init__(
        self, window: QMainWindow, registry: ActionRegistry | None = None,
        exclude: QAction | None = None, parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent or window)
        self._window = window
        self._registry = registry
        self._exclude = exclude
        self._commands: list[Command] = []
        self._shown: list[Command] = []
        self.setWindowTitle("Command Palette")
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        layout = QVBoxLayout(self)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Type a command…")
        self.filter_edit.setClearButtonEnabled(True)
        self.list = QListWidget()
        layout.addWidget(self.filter_edit)
        layout.addWidget(self.list)
        self.resize(560, 420)
        self.filter_edit.textChanged.connect(self._apply_filter)
        self.filter_edit.returnPressed.connect(self.trigger_current)
        self.list.itemActivated.connect(lambda _item: self.trigger_current())
        self.filter_edit.installEventFilter(self)

    # -- content
    def refresh(self) -> None:
        self._commands = collect_commands(self._window, self._registry, self._exclude)
        self._apply_filter(self.filter_edit.text())

    def set_filter(self, text: str) -> None:
        self.filter_edit.setText(text)  # fires textChanged -> _apply_filter

    def _apply_filter(self, text: str) -> None:
        self._shown = filter_commands(text, self._commands)
        self.list.clear()
        first_enabled = -1
        for row, cmd in enumerate(self._shown):
            label = cmd.title
            if cmd.path:
                label += f"    {cmd.path}"
            if cmd.shortcut:
                label += f"    [{cmd.shortcut}]"
            item = QListWidgetItem(label)
            if not cmd.enabled:
                # greyed and unselectable: nothing to trigger
                item.setFlags(item.flags() & ~(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable))
            elif first_enabled < 0:
                first_enabled = row
            self.list.addItem(item)
        if first_enabled >= 0:
            self.list.setCurrentRow(first_enabled)

    def visible_commands(self) -> list[Command]:
        return list(self._shown)

    # -- interaction
    def trigger_current(self) -> bool:
        """Triggers the highlighted command and closes. False (and stays open)
        when nothing is highlighted or the command is disabled."""
        row = self.list.currentRow()
        if not 0 <= row < len(self._shown):
            return False
        cmd = self._shown[row]
        if not cmd.enabled:
            return False
        self.accept()
        cmd.action.trigger()
        return True

    def show_palette(self) -> None:
        self.filter_edit.clear()
        self.refresh()
        self.open()
        self.filter_edit.setFocus()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt override)
        if obj is self.filter_edit and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                self._move(1 if key == Qt.Key.Key_Down else -1)
                return True
        return super().eventFilter(obj, event)

    def _move(self, step: int) -> None:
        """Moves the highlight to the next enabled row in `step` direction."""
        row = self.list.currentRow()
        while True:
            row += step
            if not 0 <= row < self.list.count():
                return
            if self.list.item(row).flags() & Qt.ItemFlag.ItemIsEnabled:
                self.list.setCurrentRow(row)
                return


def add_command_palette(window: QMainWindow, registry: ActionRegistry | None = None) -> QAction:
    """Installs the palette on `window` and returns the Ctrl+K QAction. Reuses
    the registry's 'command_palette' action (the View menu entry from
    standard_view_items) when there is one; otherwise creates a menu-less
    action owned by the window. The palette is kept as `window.command_palette`."""
    action = registry[KEY_COMMAND_PALETTE] if registry is not None and KEY_COMMAND_PALETTE in registry else None
    if action is None:
        action = QAction(labels.COMMAND_PALETTE, window)
        action.setMenuRole(QAction.MenuRole.NoRole)
        window.addAction(action)
    if action.shortcut().isEmpty():
        action.setShortcut(QKeySequence(standard_shortcuts.COMMAND_PALETTE))
    palette = CommandPalette(window, registry, exclude=action)
    action.triggered.connect(palette.show_palette)
    window.command_palette = palette
    return action

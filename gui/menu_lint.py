"""
redactor_common/gui/menu_lint.py

`lint_menu_bar()`: the machine check behind the menu skeleton. Each app
calls it from its own test suite against its built window (or its
StandardMenuSpec) and asserts the returned list is empty:

    def test_menu_skeleton(qapp):
        window = MainWindow()
        assert lint_menu_bar(window) == []

Checks (every problem is one human-readable string):
- heading order: File, Edit, View first; Tools, Help last; app menus between
- at most MAX_HEADINGS top-level headings
- mnemonic letters unique among the headings and within every menu/submenu
- the same keyboard shortcut is not bound to two different actions
- platform/family standard shortcuts (Ctrl+Shift+S is Save As, Ctrl+O is not
  Open Folder, F1 is not About, ...)
- canonical labels: a shared action spelled with a different mnemonic than
  core/labels.py, or under a legacy name ('Load Files…', 'Exit Program')

A trailing-ellipsis rule ('…' only where a dialog needs input) cannot be
detected from the menu and is NOT checked.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PyQt6.QtGui import QKeySequence
from PyQt6.QtWidgets import QMainWindow, QMenu, QMenuBar

from redactor_common.core import labels
from redactor_common.gui.menu_builder import MenuAction, Submenu
from redactor_common.gui.standard_menus import StandardMenuSpec

MAX_HEADINGS = 8

# Old spellings and their replacements (plain text, no mnemonic/ellipsis).
LEGACY_LABELS = {
    "load files": "Open Files",
    "load folder": "Open Folder",
    "exit program": "Exit",
    "search/replace": "Search and Replace",
    "case conversion": "Change Case",
    "auto-numbering": "Auto-Number",
    "view changelog": "Changelog",
    "save all changed": "Save All",
    "remove files": "Remove from List",
    "locate external tools": "External Tools",
    "redact recipe": "Edit Redact Recipe",
}


@dataclass
class _Node:
    """Neutral view of one menu entry: built from live menus or from a spec."""
    text: str
    shortcuts: list[str] = field(default_factory=list)
    children: list["_Node"] | None = None  # None for a plain action, list for a (sub)menu


def _seq_texts(seqs) -> list[str]:
    return [s.toString(QKeySequence.SequenceFormat.PortableText) for s in seqs if not s.isEmpty()]


def _spec_shortcuts(item: MenuAction) -> list[str]:
    seqs: list[QKeySequence] = []
    if item.shortcuts:
        seqs = [QKeySequence(s) for s in item.shortcuts]
    elif item.shortcut is not None:
        if isinstance(item.shortcut, QKeySequence.StandardKey):
            seqs = list(QKeySequence.keyBindings(item.shortcut))
        else:
            seqs = [QKeySequence(item.shortcut)]
    return _seq_texts(seqs)


def _nodes_from_items(items) -> list[_Node]:
    out: list[_Node] = []
    for item in items:
        if isinstance(item, MenuAction):
            out.append(_Node(item.text, _spec_shortcuts(item)))
        elif isinstance(item, Submenu):
            out.append(_Node(item.text, [], _nodes_from_items(item.items)))
        elif hasattr(item, "text") and hasattr(item, "shortcuts"):  # a raw QAction
            out.append(_Node(item.text(), _seq_texts(item.shortcuts())))
    return out


def _nodes_from_menu(menu: QMenu) -> list[_Node]:
    out: list[_Node] = []
    for act in menu.actions():
        if act.isSeparator():
            continue
        sub = act.menu()
        if sub is not None:
            out.append(_Node(act.text(), [], _nodes_from_menu(sub)))
        else:
            out.append(_Node(act.text(), _seq_texts(act.shortcuts())))
    return out


def _tree(target) -> list[_Node]:
    """Top-level headings (each with children) of a QMainWindow, QMenuBar or
    StandardMenuSpec."""
    if isinstance(target, QMainWindow):
        target = target.menuBar()
    if isinstance(target, QMenuBar):
        return [_Node(a.text(), [], _nodes_from_menu(a.menu()))
                for a in target.actions() if a.menu() is not None]
    if isinstance(target, StandardMenuSpec):
        return [_Node(title, [], _nodes_from_items(items)) for title, items in target.headings()]
    raise TypeError(f"lint_menu_bar needs a QMainWindow, QMenuBar or StandardMenuSpec, not {target!r}")


# Sequence -> (what the bound action's plain label must satisfy, message).
def _starts(*prefixes):
    return lambda label: label.lower().startswith(prefixes)


_SHORTCUT_RULES = {
    "Ctrl+O": (lambda l: l.lower().startswith("open") and "folder" not in l.lower(),
               "Ctrl+O is Open Files"),
    "Ctrl+Shift+O": (lambda l: l.lower().startswith("open folder"), "Ctrl+Shift+O is Open Folder"),
    "Ctrl+S": (lambda l: l.lower().startswith("save") and not l.lower().startswith("save as"),
               "Ctrl+S is Save (never Save As)"),
    "Ctrl+Shift+S": (_starts("save as"), "Ctrl+Shift+S is Save As (platform standard)"),
    "Ctrl+Shift+A": (_starts("save all"), "Ctrl+Shift+A is Save All"),
    "Ctrl+Z": (_starts("undo"), "Ctrl+Z is Undo"),
    "Ctrl+Y": (_starts("redo"), "Ctrl+Y is Redo"),
    "Ctrl+H": (_starts("search and replace"), "Ctrl+H is Search and Replace"),
    "Ctrl+K": (_starts("command palette"), "Ctrl+K is the Command Palette"),
    "Ctrl+E": (_starts("rename / export"), "Ctrl+E is Rename / Export / Move"),
    "Ctrl+Shift+E": (_starts("redact"), "Ctrl+Shift+E is Redact"),
    "Ctrl+,": (_starts("preferences"), "Ctrl+, is Preferences"),
    "Delete": (_starts("remove from list"), "Delete is Remove from List"),
    "F2": (_starts("rename file"), "F2 is Rename File"),
    "F1": (lambda l: not l.lower().startswith("about"),
           "F1 is Help contents and must not be bound to About"),
}

_CANONICAL = {
    labels.plain_label(v): v
    for n, v in vars(labels).items()
    if n.isupper() and isinstance(v, str) and not n.startswith("MENU_") and not n.endswith("_ALT")
}
_ALT = {labels.plain_label(v): v for n, v in vars(labels).items() if n.endswith("_ALT")}


def _walk(nodes: list[_Node], trail: str):
    for n in nodes:
        here = labels.plain_label(n.text)
        if n.children is None:
            yield n, trail
        else:
            yield n, trail
            yield from _walk(n.children, f"{trail} > {here}" if trail else here)


def lint_menu_bar(target, *, max_headings: int = MAX_HEADINGS, check_canonical: bool = True) -> list[str]:
    """Returns the list of skeleton violations in `target` (a QMainWindow,
    its QMenuBar, or a StandardMenuSpec); empty = conforming."""
    problems: list[str] = []
    headings = _tree(target)
    names = [labels.plain_label(h.text) for h in headings]

    # heading order / count
    if len(headings) > max_headings:
        problems.append(f"{len(headings)} top-level menus; the cap is {max_headings}")
    for pos, expected in enumerate(("File", "Edit", "View")):
        if pos >= len(names) or names[pos] != expected:
            problems.append(f"menu #{pos + 1} must be {expected}, found {names[pos] if pos < len(names) else 'nothing'}")
    if len(names) < 2 or names[-2:] != ["Tools", "Help"]:
        problems.append(f"the last two menus must be Tools, Help; found {names[-2:]}")
    if len(set(names)) != len(names):
        problems.append(f"duplicate top-level menu names: {names}")

    # mnemonics: headings, then every menu and submenu
    def check_mnemonics(where: str, nodes: list[_Node]) -> None:
        seen: dict[str, str] = {}
        for n in nodes:
            letter = labels.mnemonic_letter(n.text)
            if letter is None:
                continue
            if letter in seen:
                problems.append(
                    f"{where}: mnemonic '{letter.upper()}' used by both "
                    f"'{labels.plain_label(seen[letter])}' and '{labels.plain_label(n.text)}'")
            else:
                seen[letter] = n.text

    check_mnemonics("menu bar", headings)
    for h in headings:
        stack = [(labels.plain_label(h.text), h.children or [])]
        while stack:
            where, nodes = stack.pop()
            check_mnemonics(where, nodes)
            for n in nodes:
                if n.children is not None:
                    stack.append((f"{where} > {labels.plain_label(n.text)}", n.children))

    # shortcuts
    bound: dict[str, list[str]] = {}
    for h in headings:
        for n, trail in _walk(h.children or [], labels.plain_label(h.text)):
            if n.children is not None:
                continue
            plain = labels.plain_label(n.text)
            for seq in n.shortcuts:
                bound.setdefault(seq, []).append(f"{trail} > {plain}")
                rule = _SHORTCUT_RULES.get(seq)
                if rule and not rule[0](plain):
                    problems.append(f"{trail} > {plain} is bound to {seq}: {rule[1]}")
            if check_canonical:
                low = plain.lower()
                if low in LEGACY_LABELS:
                    problems.append(f"{trail} > {plain}: legacy label, use '{LEGACY_LABELS[low]}'")
                canon = _CANONICAL.get(plain) or _ALT.get(plain)
                if canon and n.text not in (_CANONICAL.get(plain), _ALT.get(plain)):
                    problems.append(f"{trail} > {plain}: label is '{n.text}', canonical is '{canon}'")
    for seq, where in bound.items():
        if len(where) > 1:
            problems.append(f"shortcut {seq} bound more than once: {', '.join(where)}")
    return problems

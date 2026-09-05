from dataclasses import dataclass

from redactor_common.core.undo import UndoManager


@dataclass
class _Item:
    value: str


def _snapshot(item: _Item) -> str:
    return item.value


def _restore(item: _Item, snapshot: str) -> None:
    item.value = snapshot


def test_cannot_undo_empty_stack():
    manager = UndoManager()
    assert manager.can_undo() is False
    assert manager.peek_label() is None
    assert manager.undo(_restore) == []


def test_push_then_undo_restores_value():
    manager = UndoManager()
    item = _Item("original")
    manager.push("Edit", [item], _snapshot)
    item.value = "changed"

    assert manager.can_undo() is True
    assert manager.peek_label() == "Edit"
    affected = manager.undo(_restore)

    assert affected == [item]
    assert item.value == "original"
    assert manager.can_undo() is False


def test_push_snapshots_multiple_items_independently():
    manager = UndoManager()
    a, b = _Item("a1"), _Item("b1")
    manager.push("Bulk edit", [a, b], _snapshot)
    a.value = "a2"
    b.value = "b2"

    manager.undo(_restore)
    assert a.value == "a1"
    assert b.value == "b1"


def test_multiple_pushes_undo_in_lifo_order():
    manager = UndoManager()
    item = _Item("v0")
    manager.push("first", [item], _snapshot)
    item.value = "v1"
    manager.push("second", [item], _snapshot)
    item.value = "v2"

    assert manager.peek_label() == "second"
    manager.undo(_restore)
    assert item.value == "v1"
    assert manager.peek_label() == "first"
    manager.undo(_restore)
    assert item.value == "v0"
    assert manager.can_undo() is False


def test_max_entries_drops_oldest():
    manager = UndoManager(max_entries=2)
    item = _Item("v0")
    for i in range(1, 4):
        manager.push(f"edit{i}", [item], _snapshot)
        item.value = f"v{i}"
    # Only the last 2 pushes survive: edit2 (snapshot "v1") and edit3 (snapshot "v2")
    assert manager.peek_label() == "edit3"
    manager.undo(_restore)
    assert item.value == "v2"
    assert manager.peek_label() == "edit2"
    manager.undo(_restore)
    assert item.value == "v1"
    assert manager.can_undo() is False  # edit1 was dropped, never reachable


def test_clear_drops_everything():
    manager = UndoManager()
    item = _Item("v0")
    manager.push("edit", [item], _snapshot)
    manager.clear()
    assert manager.can_undo() is False
    assert manager.peek_label() is None

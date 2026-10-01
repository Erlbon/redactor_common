"""Tests for core/duplicates.py (group model, sorting, dismissal stores) and
gui/duplicates_dialog.py (headless: QT_QPA_PLATFORM=offscreen). Message boxes
are faked and nothing reaches the real Recycle Bin or file manager."""

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from redactor_common.core import duplicates as dup  # noqa: E402
from redactor_common.core.duplicates import (  # noqa: E402
    DuplicateGroup,
    DuplicateMember,
    InMemoryDismissStore,
    JsonDismissStore,
)
from redactor_common.core.trash import TrashError  # noqa: E402
from redactor_common.gui import duplicates_dialog as dd  # noqa: E402
from redactor_common.gui.duplicates_dialog import DuplicatesDialog, run_find_duplicates  # noqa: E402

_app = QApplication.instance() or QApplication([])

COLUMNS = [("name", "File"), ("folder", "Folder"), ("size", "Size")]


def member(name, fp=None, folder="/lib"):
    path = f"{folder}/{name}"
    return DuplicateMember(
        item=name, path=path, fields={"name": name, "folder": folder, "size": "1 MB"},
        fingerprint=name if fp is None else fp,
    )


def group(key, tier, *names, reason=None):
    return DuplicateGroup(key, tier, reason or f"reason {key}", [member(n) for n in names])


# --- core ---------------------------------------------------------------


def test_tier_labels_and_strength_order():
    assert dup.tier_label(dup.TIER_IDENTICAL) == "Identical"
    assert dup.tier_label("martian") == "martian"
    ranks = [dup.tier_strength(t) for t in (dup.TIER_IDENTICAL, dup.TIER_STRONG, dup.TIER_POSSIBLE, dup.TIER_WEAK)]
    assert ranks == sorted(ranks) and len(set(ranks)) == 4
    assert dup.tier_strength("martian") > dup.tier_strength(dup.TIER_WEAK)


def test_sort_groups_by_strength_then_size():
    weak_big = group("wb", dup.TIER_WEAK, "a", "b", "c", "d")
    strong_small = group("ss", dup.TIER_STRONG, "e", "f")
    strong_big = group("sb", dup.TIER_STRONG, "g", "h", "i")
    ident = group("id", dup.TIER_IDENTICAL, "j", "k")
    order = [g.key for g in dup.sort_groups([weak_big, strong_small, strong_big, ident])]
    assert order == ["id", "sb", "ss", "wb"]


def test_dismissal_key_is_order_independent_and_set_specific():
    assert dup.dismissal_key(["a", "b"]) == dup.dismissal_key(["b", "a"])
    assert dup.dismissal_key(["a", "b"]) != dup.dismissal_key(["a", "b", "c"])
    assert dup.dismissal_key(["a", "b"]) != dup.dismissal_key(["a", "c"])
    assert dup.dismissal_key(["ab", "c"]) != dup.dismissal_key(["a", "bc"])


def test_dismissal_survives_rename_and_reappears_with_third_copy():
    store = InMemoryDismissStore()
    g1 = DuplicateGroup("k", dup.TIER_STRONG, "r", [member("a.epub", "FP1"), member("b.epub", "FP2")])
    store.dismiss(g1.identities)
    renamed = DuplicateGroup("k", dup.TIER_STRONG, "r", [
        member("moved/new-name.epub", "FP2", folder="/elsewhere"), member("a.epub", "FP1")])
    assert store.is_dismissed(renamed.identities)
    third = DuplicateGroup("k", dup.TIER_STRONG, "r", renamed.members + [member("c.epub", "FP3")])
    assert not store.is_dismissed(third.identities)


def test_member_without_fingerprint_falls_back_to_path():
    m = DuplicateMember(item=1, path="/x/a.mp3")
    assert m.identity == "/x/a.mp3"


def test_in_memory_store_basics():
    store = InMemoryDismissStore()
    store.dismiss(["a", "b"])
    store.dismiss(["b", "a"])
    assert store.count() == 1
    store.undismiss(["a", "b"])
    assert store.count() == 0 and not store.is_dismissed(["a", "b"])
    store.dismiss(["a", "b"])
    store.undismiss_all()
    assert store.count() == 0


def test_json_store_round_trip(tmp_path):
    path = tmp_path / "sub" / "dismissed.json"
    store = JsonDismissStore(str(path))
    assert store.count() == 0
    store.dismiss(["a", "b"])
    store.dismiss(["c", "d"])
    again = JsonDismissStore(str(path))
    assert again.count() == 2
    assert again.is_dismissed(["b", "a"]) and again.is_dismissed(["c", "d"])
    again.undismiss(["a", "b"])
    assert JsonDismissStore(str(path)).count() == 1
    again.undismiss_all()
    assert JsonDismissStore(str(path)).count() == 0
    assert json.loads(path.read_text(encoding="utf-8"))["dismissed"] == []
    assert [p.name for p in path.parent.iterdir()] == ["dismissed.json"]   # no temp litter


def test_json_store_atomic_replace_and_failed_write_keeps_memory(tmp_path, monkeypatch):
    path = tmp_path / "d.json"
    store = JsonDismissStore(str(path))
    store.dismiss(["a", "b"])
    before = path.read_text(encoding="utf-8")
    calls = []
    real = dup.replace_with_retry

    def spy(src, dst, **kw):
        calls.append((os.path.dirname(src), dst))
        real(src, dst, **kw)

    monkeypatch.setattr(dup, "replace_with_retry", spy)
    store.dismiss(["c", "d"])
    assert calls and calls[0][1] == str(path)   # went through the temp-then-replace path

    def boom(src, dst, **kw):
        raise PermissionError("locked")

    monkeypatch.setattr(dup, "replace_with_retry", boom)
    store.dismiss(["e", "f"])
    assert store.last_error and store.is_dismissed(["e", "f"])   # still works this session
    assert path.read_text(encoding="utf-8") != before            # earlier write intact, not truncated
    assert len(json.loads(path.read_text(encoding="utf-8"))["dismissed"]) == 2
    assert [p.name for p in tmp_path.iterdir()] == ["d.json"]    # temp cleaned up


@pytest.mark.parametrize("content", ["", "not json", "[1, 2]", '{"dismissed": "x"}', '{"dismissed": [1, null, ""]}', b"\xff\xfe\x00"])
def test_json_store_tolerates_corrupt_file(tmp_path, content):
    path = tmp_path / "d.json"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    store = JsonDismissStore(str(path))
    assert store.count() == 0
    store.dismiss(["a", "b"])   # and recovers by rewriting
    assert JsonDismissStore(str(path)).count() == 1


def test_json_store_caps_size_dropping_oldest(tmp_path):
    store = JsonDismissStore(str(tmp_path / "d.json"), max_entries=3)
    for i in range(5):
        store.dismiss([f"x{i}", "y"])
    assert store.count() == 3
    assert not store.is_dismissed(["x0", "y"]) and not store.is_dismissed(["x1", "y"])
    assert store.is_dismissed(["x4", "y"])
    assert JsonDismissStore(str(tmp_path / "d.json"), max_entries=3).count() == 3
    # a hand-edited, oversized file is trimmed on load too
    big = tmp_path / "big.json"
    big.write_text(json.dumps({"dismissed": [f"k{i}" for i in range(10)]}), encoding="utf-8")
    assert JsonDismissStore(str(big), max_entries=4).count() == 4


# --- dialog -------------------------------------------------------------


class Boxes:
    """Fake QMessageBox statics; records calls and answers `answer`."""

    def __init__(self, monkeypatch, answer=QMessageBox.StandardButton.Yes):
        self.answer = answer
        self.questions, self.warnings, self.infos = [], [], []
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: (self.questions.append(a), self.answer)[1])
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: self.warnings.append(a))
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: self.infos.append(a))


def make(groups, **kw):
    kw.setdefault("trash", lambda p: None)
    return DuplicatesDialog(groups, COLUMNS, **kw)


def member_rows(dlg):
    rows = []
    for g in range(dlg.tree.topLevelItemCount()):
        top = dlg.tree.topLevelItem(g)
        rows.extend(top.child(c) for c in range(top.childCount()))
    return rows


def select(dlg, *names):
    dlg.tree.clearSelection()
    for row in member_rows(dlg):
        if row.text(0) in names:
            row.setSelected(True)


def test_dialog_shows_groups_tier_reason_and_columns_nothing_selected():
    groups = [group("w", dup.TIER_WEAK, "w1.mp3", "w2.mp3", reason="similar title"),
              group("i", dup.TIER_IDENTICAL, "i1.mp3", "i2.mp3", "i3.mp3", reason="same bytes")]
    dlg = make(groups)
    assert dlg.tree.topLevelItemCount() == 2
    first = dlg.tree.topLevelItem(0)   # strongest first
    assert "Identical" in first.text(0) and "same bytes" in first.text(0) and "3 files" in first.text(0)
    assert "Weak match" in dlg.tree.topLevelItem(1).text(0)
    assert [dlg.tree.headerItem().text(c) for c in range(3)] == ["File", "Folder", "Size"]
    row = first.child(0)
    assert (row.text(0), row.text(1), row.text(2)) == ("i1.mp3", "/lib", "1 MB")
    assert dlg.tree.selectedItems() == [] and dlg.selected_files() == []
    assert not dlg.reveal_button.isEnabled() and not dlg.trash_button.isEnabled()
    for r in member_rows(dlg):   # nothing ticked: no checkboxes either
        assert r.data(0, dd.Qt.ItemDataRole.CheckStateRole) is None


def test_dialog_header_states_policy_and_does_not_mutate_input():
    groups = [group("a", dup.TIER_STRONG, "a", "b")]
    dlg = make(groups, intro_text="Same ISBN.")
    text = dlg.summary_label.text()
    assert "not always mistakes" in text and "Same ISBN." in text and "nothing is" in text
    assert dlg.windowTitle() == "Find Duplicates"
    assert len(groups[0].members) == 2


def test_dialog_empty_state():
    dlg = make([])
    assert dlg.empty_label.text() == dd.EMPTY_TEXT and not dlg.empty_label.isHidden()
    assert dlg.tree.isHidden()
    assert "not always mistakes" in dlg.summary_label.text()


def test_buttons_reflect_options():
    dlg = make([group("a", dup.TIER_STRONG, "a", "b")], allow_trash=False)
    assert dlg.trash_button.isHidden() and dlg.select_button.isHidden() and dlg.dismiss_button.isHidden()
    dlg = make([group("a", dup.TIER_STRONG, "a", "b")], dismiss_store=InMemoryDismissStore(),
               on_select_in_list=lambda items: None)
    assert not dlg.trash_button.isHidden() and not dlg.select_button.isHidden() and not dlg.dismiss_button.isHidden()
    assert dlg.reveal_button.text() == "Reveal in Folder" and dlg.open_button.text() == "Open"
    assert dlg.select_button.text() == "Select These in the List"
    assert dlg.trash_button.text() == "Move Selected to Recycle Bin..."


def test_select_in_list_callback_gets_selected_items(monkeypatch):
    got = []
    dlg = make([group("a", dup.TIER_STRONG, "a", "b", "c")], on_select_in_list=got.append)
    select(dlg, "a", "c")
    dlg.select_button.click()
    assert got == [["a", "c"]] and dlg.to_select == ["a", "c"]
    assert dlg.result() == QDialog.DialogCode.Accepted


def test_reveal_and_open_use_selected_paths(monkeypatch):
    revealed, opened = [], []
    monkeypatch.setattr(dd, "reveal_in_file_manager", revealed.append)
    monkeypatch.setattr(dd, "open_with_default_app", lambda p: opened.append(p) or True)
    dlg = make([group("a", dup.TIER_STRONG, "a", "b")])
    select(dlg, "b")
    dlg.reveal_button.click()
    dlg.open_button.click()
    assert revealed == ["/lib/b"] and opened == ["/lib/b"]


def test_trash_confirm_defaults_to_no_and_declining_touches_nothing(monkeypatch):
    boxes = Boxes(monkeypatch, answer=QMessageBox.StandardButton.No)
    trashed = []
    dlg = make([group("a", dup.TIER_STRONG, "a", "b", "c")], trash=trashed.append)
    select(dlg, "a")
    dlg.trash_button.click()
    assert len(boxes.questions) == 1
    args = boxes.questions[0]
    assert args[4] == QMessageBox.StandardButton.No          # default button
    assert "/lib/a" in args[2] and "/lib/b" not in args[2]    # lists the files
    assert trashed == [] and dlg.trashed == [] and len(member_rows(dlg)) == 3


def test_trash_moves_only_selected_and_drops_lone_remainder(monkeypatch):
    Boxes(monkeypatch)
    trashed, notified = [], []
    dlg = make([group("a", dup.TIER_STRONG, "a", "b"), group("z", dup.TIER_WEAK, "x", "y", "w")],
               trash=trashed.append, on_trashed=notified.append)
    select(dlg, "a", "x")
    dlg.trash_button.click()
    assert sorted(trashed) == ["/lib/a", "/lib/x"]
    assert sorted(dlg.trashed) == ["a", "x"] and sorted(notified[0]) == ["a", "x"]
    # group "a" is down to one file -> gone; group "z" keeps y and w
    assert [r.text(0) for r in member_rows(dlg)] == ["y", "w"]
    assert dlg.tree.topLevelItemCount() == 1


def test_trash_refuses_whole_group_and_explains(monkeypatch):
    boxes = Boxes(monkeypatch)
    trashed = []
    dlg = make([group("a", dup.TIER_STRONG, "a", "b", reason="same ISBN")], trash=trashed.append)
    select(dlg, "a", "b")
    dlg.trash_button.click()
    assert trashed == [] and boxes.questions == []   # refused before even asking
    assert len(boxes.warnings) == 1 and "at least one" in boxes.warnings[0][2] and "same ISBN" in boxes.warnings[0][2]
    assert len(member_rows(dlg)) == 2


def test_whole_group_check_counts_the_same_file_in_two_groups(monkeypatch):
    boxes = Boxes(monkeypatch)
    g1 = DuplicateGroup("1", dup.TIER_STRONG, "r1", [member("a"), member("b")])
    g2 = DuplicateGroup("2", dup.TIER_WEAK, "r2", [member("b"), member("c")])
    trashed = []
    dlg = make([g1, g2], trash=trashed.append)
    select(dlg, "b")   # b is in both; the other member of each stays
    dlg.trash_button.click()
    assert trashed == ["/lib/b"]
    dlg2 = make([DuplicateGroup("1", dup.TIER_STRONG, "r1", [member("a"), member("b")]),
                 DuplicateGroup("2", dup.TIER_WEAK, "r2", [member("a"), member("b")])], trash=trashed.append)
    select(dlg2, "a", "b")
    dlg2.trash_button.click()
    assert boxes.warnings and trashed == ["/lib/b"]


def test_trash_per_file_error_isolation(monkeypatch):
    boxes = Boxes(monkeypatch)
    trashed = []

    def fake_trash(path):
        if path.endswith("/b"):
            raise TrashError("couldn't move it to the Recycle Bin: nope")
        if path.endswith("/c"):
            raise OSError("disk gone")
        trashed.append(path)

    dlg = make([group("a", dup.TIER_STRONG, "a", "b", "c", "d")], trash=fake_trash)
    select(dlg, "a", "b", "c")
    dlg.trash_button.click()
    assert trashed == ["/lib/a"] and dlg.trashed == ["a"]
    assert sorted(r.text(0) for r in member_rows(dlg)) == ["b", "c", "d"]   # failures stay listed
    assert len(boxes.warnings) == 1
    msg = boxes.warnings[0][2]
    assert "/lib/b" in msg and "nope" in msg and "/lib/c" in msg and "disk gone" in msg and "/lib/a" not in msg


def test_trash_button_absent_and_inert_when_not_allowed(monkeypatch):
    boxes = Boxes(monkeypatch)
    trashed = []
    dlg = make([group("a", dup.TIER_STRONG, "a", "b")], allow_trash=False, trash=trashed.append)
    select(dlg, "a")
    dlg._trash_selected()
    assert trashed == [] and boxes.questions == []


def test_dismiss_hides_and_unhides(monkeypatch):
    store = InMemoryDismissStore()
    dlg = make([group("a", dup.TIER_STRONG, "a", "b"), group("c", dup.TIER_WEAK, "c", "d")], dismiss_store=store)
    assert dlg.show_hidden_check.isHidden()
    select(dlg, "a")
    assert dlg.dismiss_button.isEnabled() and not dlg.restore_button.isEnabled()
    dlg.dismiss_button.click()
    assert store.count() == 1 and dlg.tree.topLevelItemCount() == 1
    assert not dlg.show_hidden_check.isHidden() and dlg.show_hidden_check.text() == "Show 1 hidden group"
    dlg.show_hidden_check.setChecked(True)
    assert dlg.tree.topLevelItemCount() == 2
    hidden_top = dlg.tree.topLevelItem(0)
    assert "hidden" in hidden_top.text(0)
    hidden_top.setSelected(True)   # a group row counts as selecting its group
    assert dlg.restore_button.isEnabled() and not dlg.dismiss_button.isEnabled()
    dlg.restore_button.click()
    assert store.count() == 0 and dlg.show_hidden_check.isHidden() is False or dlg.show_hidden_check.isChecked()
    dlg.show_hidden_check.setChecked(False)
    assert dlg.tree.topLevelItemCount() == 2 and dlg.show_hidden_check.isHidden()


def test_previously_dismissed_group_starts_hidden_and_all_hidden_state():
    g = group("a", dup.TIER_STRONG, "a", "b")
    store = InMemoryDismissStore()
    store.dismiss(g.identities)
    dlg = make([g], dismiss_store=store)
    assert dlg.tree.topLevelItemCount() == 0 and dlg.tree.isHidden()
    assert "1 group hidden" in dlg.empty_label.text()
    assert dlg.show_hidden_check.text() == "Show 1 hidden group"


def test_dismissed_group_reappears_when_a_copy_is_added():
    store = InMemoryDismissStore()
    store.dismiss(group("a", dup.TIER_STRONG, "a", "b").identities)
    dlg = make([group("a", dup.TIER_STRONG, "a", "b", "c")], dismiss_store=store)
    assert dlg.tree.topLevelItemCount() == 1


def test_store_without_undismiss_hides_restore_button():
    class Minimal(dup.DismissStore):
        def __init__(self):
            self.keys = set()

        def is_dismissed(self, fps):
            return dup.dismissal_key(fps) in self.keys

        def dismiss(self, fps):
            self.keys.add(dup.dismissal_key(fps))

        def undismiss_all(self):
            self.keys.clear()

        def count(self):
            return len(self.keys)

    dlg = make([group("a", dup.TIER_STRONG, "a", "b")], dismiss_store=Minimal())
    assert dlg.restore_button.isHidden() and not dlg.dismiss_button.isHidden()


# --- run_find_duplicates ------------------------------------------------


def test_run_find_duplicates_opens_dialog_with_found_groups(monkeypatch):
    shown = []
    monkeypatch.setattr(DuplicatesDialog, "exec", lambda self: shown.append(self) or 0)
    seen = {}

    def find_fn(items, progress, cancelled):
        seen["items"] = list(items)
        seen["cancelled"] = cancelled()
        for i in range(len(items)):
            progress(i + 1, len(items), f"item {i}")
        return [group("a", dup.TIER_STRONG, "a", "b"), group("lone", dup.TIER_WEAK, "x")]

    dlg = run_find_duplicates(None, ["a", "b", "x"], find_fn, COLUMNS, title="Dupes", intro_text="hi")
    assert dlg is shown[0] and dlg.windowTitle() == "Dupes"
    assert seen == {"items": ["a", "b", "x"], "cancelled": False}
    assert dlg.tree.topLevelItemCount() == 1   # single-member groups are not groups


def test_run_find_duplicates_none_found_and_failure(monkeypatch):
    boxes = Boxes(monkeypatch)
    monkeypatch.setattr(DuplicatesDialog, "exec", lambda self: pytest.fail("no dialog expected"))
    assert run_find_duplicates(None, ["a"], lambda i, p, c: [], COLUMNS, none_found_message="Nothing.") is None
    assert boxes.infos and boxes.infos[0][2] == "Nothing."

    def broken(items, progress, cancelled):
        raise RuntimeError("scan blew up")

    assert run_find_duplicates(None, ["a"], broken, COLUMNS) is None
    assert "scan blew up" in boxes.warnings[0][2]


def test_run_find_duplicates_with_no_items_and_non_cancellable(monkeypatch):
    Boxes(monkeypatch)
    monkeypatch.setattr(DuplicatesDialog, "exec", lambda self: 0)
    assert run_find_duplicates(None, [], lambda i, p, c: [], COLUMNS, cancellable=False) is None

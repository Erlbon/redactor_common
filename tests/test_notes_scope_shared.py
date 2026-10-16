"""The shared 'which folder?' question of Create Notes: the merge of the choices (core/notes_scope.py) and the dialog
(gui/notes_scope_dialog.py) driven by its widgets with fake describe / count functions."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

from redactor_common.core.notes_scope import MAX_CHOICES, merge_choices, opened_caption, opened_folder  # noqa: E402
from redactor_common.gui.notes_scope_dialog import NotesScopeDialog  # noqa: E402

_app = QApplication.instance() or QApplication([])
_PARENTS: list = []


def keep() -> QWidget:
    widget = QWidget()
    _PARENTS.append(widget)
    return widget


def folders(tmp_path, *names):
    out = []
    for name in names:
        (tmp_path / name).mkdir(exist_ok=True)
        out.append(str(tmp_path / name))
    return out


# -- the merge --------------------------------------------------------------------------------------------------------

def test_missing_folders_are_left_out_and_the_same_folder_is_merged_once(tmp_path):
    a, b = folders(tmp_path, "A", "B")
    found = merge_choices([(a, "the folder you opened", False), (str(tmp_path / "gone"), "last time", False),
                           (a.upper() if os.name == "nt" else a, "the library folder", False), (b, "one level up", False)])
    assert [c.path for c in found][0] == a and len(found) == 2
    assert found[0].caption == "the folder you opened; the library folder"


def test_the_first_choice_is_ticked_when_none_was_asked_for(tmp_path):
    a, b = folders(tmp_path, "A", "B")
    assert [c.default for c in merge_choices([(a, "x", False), (b, "y", False)])] == [True, False]
    assert [c.default for c in merge_choices([(a, "x", False), (b, "y", True)])] == [False, True]


def test_too_many_choices_keep_the_ticked_ones(tmp_path):
    names = folders(tmp_path, *[f"F{i}" for i in range(MAX_CHOICES + 3)])
    candidates = [(p, "c", p == names[-1]) for p in names]
    found = merge_choices(candidates)
    assert len(found) == MAX_CHOICES and found[-1].path == names[-1] and found[-1].default


def test_the_opened_folder_is_the_last_directory_when_every_loaded_file_is_in_it(tmp_path):
    a = folders(tmp_path, "A")[0]
    (Path(a) / "sub").mkdir()
    files = [str(Path(a) / "one.mkv"), str(Path(a) / "sub" / "two.mkv")]
    assert opened_folder(files, a) == (a, 2) and opened_folder([], a) == (a, 0)
    assert opened_folder(files, str(tmp_path / "other"))[0] == a  # the common folder when last_directory does not fit
    assert opened_folder([], "") == ("", 0)
    assert "all 2 loaded files" in opened_caption(2) and opened_caption(0) == "the folder you opened"


# -- the dialog -------------------------------------------------------------------------------------------------------

KIND_CHECKS = (("series", "Series notes"), ("movie", "Film notes"))


def make_dialog(tmp_path, names=("A", "B"), kinds="series,movie", include=True, **kw):
    paths = folders(tmp_path, *names)
    choices = merge_choices([(p, f"folder {Path(p).name}", False) for p in paths])
    included = []

    def parse(text):
        words = [w for w in str(text or "").split(",") if w] if not isinstance(text, (list, tuple)) else list(text)
        return tuple(w for w in ("series", "movie") if w in words) or ("series", "movie")

    dialog = NotesScopeDialog(
        keep(), choices, include, kind_checks=KIND_CHECKS, kinds=kinds, parse_kinds=parse,
        kinds_text=lambda ks: ",".join(parse(ks)), describe=kw.pop("describe", lambda p: ("info", Path(p).name)),
        caption_of=lambda info, name, ks: f"would get notes ({ks}): {info[1]}",
        count=kw.pop("count", lambda folder, include: (3, 4)),
        hint=lambda counts, ks: f"{counts[0] + counts[1]} folders for {ks}" if counts[0] + counts[1] > 5 else "",
        on_include_changed=included.append, **kw)
    dialog.show()
    dialog.included = included
    return dialog, paths


def wait_for(condition, ms=3000):
    waited = 0
    while not condition() and waited < ms:
        QTest.qWait(20)
        waited += 20
    return condition()


def test_the_first_folder_is_ticked_and_continue_returns_it_with_the_kinds(tmp_path):
    d, paths = make_dialog(tmp_path)
    assert d.chosen_folder() == paths[0] and d.continue_button.isEnabled() and d.continue_button.isDefault()
    d.kind_boxes["movie"].setChecked(False)
    QTest.mouseClick(d.continue_button, Qt.MouseButton.LeftButton)
    assert (d.folder, d.kinds, d.outdated, d.result()) == (paths[0], "series", False, d.DialogCode.Accepted)
    d.close()


def test_what_each_folder_would_get_is_shown_under_it_and_follows_the_ticked_kinds(tmp_path):
    d, paths = make_dialog(tmp_path)
    assert wait_for(lambda: "would get notes" in d.captions[0].text())
    assert d.captions[0].text() == "Folder A  -  would get notes (series,movie): A"
    d.kind_boxes["movie"].setChecked(False)
    assert wait_for(lambda: "(series)" in d.captions[0].text())
    d.close()


def test_with_nothing_ticked_in_what_to_create_continue_is_off(tmp_path):
    d, _paths = make_dialog(tmp_path)
    for box in d.kind_boxes.values():
        box.setChecked(False)
    assert not d.continue_button.isEnabled() and d.chosen_kinds() == ""
    d.close()


def test_a_big_scope_shows_the_hint_and_a_small_one_does_not(tmp_path):
    d, _p = make_dialog(tmp_path, count=lambda folder, include: (4, 4))
    assert wait_for(lambda: d.counts == (4, 4)) and d.hint_label.isVisibleTo(d) and "8 folders" in d.hint_label.text()
    d.close()
    d2, _p = make_dialog(tmp_path, count=lambda folder, include: (1, 1))
    assert wait_for(lambda: d2.counts == (1, 1)) and not d2.hint_label.isVisibleTo(d2)
    d2.close()


def test_include_subfolders_tells_the_caller_and_the_older_notes_box_starts_off(tmp_path):
    d, _p = make_dialog(tmp_path, include=True)
    assert d.include_note.text() == "Everything below the folder is looked at." and not d.outdated_box.isChecked()
    d.include_box.setChecked(False)
    assert d.included == [False] and d.include_note.text() == "Only this folder is looked at."
    d.outdated_box.setChecked(True)
    QTest.mouseClick(d.continue_button, Qt.MouseButton.LeftButton)
    assert d.outdated is True
    d.close()


def test_a_failing_describe_leaves_the_caption_alone(tmp_path):
    def broken(path):
        raise OSError("unreadable")

    d, _p = make_dialog(tmp_path, describe=broken)
    QTest.qWait(150)
    assert d.captions[0].text() == "Folder A" and d.continue_button.isEnabled()
    d.close()


def test_another_folder_through_browse_becomes_the_chosen_one(tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QFileDialog

    other = folders(tmp_path, "Other")[0]
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: other))
    d, _p = make_dialog(tmp_path)
    QTest.mouseClick(d.browse_button, Qt.MouseButton.LeftButton)
    assert d.chosen_folder() == other and d.other_radio.isChecked() and d.other_label.text().startswith(other)
    d.close()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: ""))
    d2, paths = make_dialog(tmp_path)
    d2.other_radio.setChecked(True)  # cancelled browse: back to the first choice
    assert d2.chosen_folder() == paths[0]
    d2.close()


def test_with_no_choices_the_dialog_asks_for_a_folder_at_once(tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QFileDialog

    asked = []
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: asked.append(1) or ""))
    d = NotesScopeDialog(keep(), [], True, kind_checks=KIND_CHECKS, kinds="series", parse_kinds=lambda t: ("series",),
                         kinds_text=lambda ks: "series", describe=lambda p: None, caption_of=lambda *a: "",
                         count=lambda *a: None, hint=lambda *a: "")
    d.show()
    QTest.qWait(100)
    assert asked and not d.continue_button.isEnabled()
    d.close()

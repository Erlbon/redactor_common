"""The review page: updates of older notes (asked first, the Recycle Bin), Write all remaining, the summary with the lookup and
history notes, and closing with ticked notes unwritten. Real widgets, real notes written into tmp_path."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from redactor_common.core import trash  # noqa: E402
from redactor_common.core.notes import NoteFrontMatter, note_status, render_note  # noqa: E402
from redactor_common.core.notes_model import Confidence, Proposal, write_proposal  # noqa: E402
from redactor_common.core.notes_review import VIEW_LIST, VIEW_ONE, WRITTEN, ReviewSession  # noqa: E402
from redactor_common.gui.notes_review_dialog import NotesReviewDialog  # noqa: E402
from tests.test_notes_shared import FakeTarget  # noqa: E402

_app = QApplication.instance() or QApplication([])
NEW = "---\ntitle: {name}\n---\n\n# {name}\n\nNew text.\n"


class Boxes:
    def __init__(self):
        self.said, self.questions, self.answer = [], [], QMessageBox.StandardButton.Yes


@pytest.fixture
def boxes(monkeypatch):
    b = Boxes()
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: b.said.append(a[2]) or QMessageBox.StandardButton.Ok))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: b.said.append(a[2]) or QMessageBox.StandardButton.Ok))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: b.questions.append(a[2]) or b.answer))
    return b


def make(tmp_path, name, update=False):
    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    state = None
    if update:
        old = render_note(NoteFrontMatter(title=name, generator="app 2026-01-01#01", note_format=1), f"# {name}\n\nOld text.")
        (folder / "series.md").write_bytes(old.encode("utf-8"))
        state = note_status(folder / "series.md")
    return Proposal(FakeTarget(folder, "series", "series"), Confidence.CONFIDENT, NEW.format(name=name), title=name, old_state=state)


def page(proposals, view=VIEW_LIST, **kw):
    d = NotesReviewDialog(ReviewSession(proposals, write_fn=write_proposal), view, rebuild=lambda p, c: p, **kw)
    d.show()
    return d


@pytest.fixture
def recycle(monkeypatch):
    binned = []
    monkeypatch.setattr(trash, "trash_with_retries", lambda *a, **k: lambda path: (binned.append(path), os.remove(path)))
    return binned


def test_an_update_is_not_preticked_asks_first_and_sends_the_old_note_to_the_bin(tmp_path, boxes, recycle):
    p = make(tmp_path, "Old", update=True)
    d = page([p])
    assert p.is_update and not d.session.items[0].ticked  # an update is never pre-ticked
    d.tree.topLevelItem(0).setCheckState(0, Qt.CheckState.Checked)
    boxes.answer = QMessageBox.StandardButton.No
    QTest.mouseClick(d.write_ticked_button, Qt.MouseButton.LeftButton)
    assert "Old text." in (tmp_path / "Old" / "series.md").read_text(encoding="utf-8") and recycle == []
    assert "Replace 1 note made by an older version?" in boxes.questions[-1] and "Recycle Bin" in boxes.questions[-1]
    boxes.answer = QMessageBox.StandardButton.Yes
    QTest.mouseClick(d.write_ticked_button, Qt.MouseButton.LeftButton)
    assert recycle == [str(tmp_path / "Old" / "series.md")]
    assert "New text." in (tmp_path / "Old" / "series.md").read_text(encoding="utf-8")
    d.close()


def test_the_one_at_a_time_button_says_update_for_an_update(tmp_path, boxes):
    d = page([make(tmp_path, "Old", update=True)], view=VIEW_ONE)
    assert d.write_button.text() == "Update note" and d.one_panel.tabs.isTabVisible(d.one_panel.diff_tab)
    d.close()


def test_write_all_remaining_writes_new_notes_but_never_an_unticked_update(tmp_path, boxes, recycle):
    new, old = make(tmp_path, "New"), make(tmp_path, "Old", update=True)
    d = page([new, old], view=VIEW_ONE)
    assert d.write_all_button.text() == "Write all remaining confident (2)"
    QTest.mouseClick(d.write_all_button, Qt.MouseButton.LeftButton)
    assert (tmp_path / "New" / "series.md").is_file() and "Old text." in (tmp_path / "Old" / "series.md").read_text(encoding="utf-8")
    assert recycle == [] and boxes.said and "Written: 1" in boxes.said[-1]
    d.close()


def test_the_summary_carries_the_lookup_and_history_notes(tmp_path, boxes):
    d = page([make(tmp_path, "A")], view=VIEW_ONE, lookup_note="Looked up 1 folder in 0:00:02 (4 requests, 1 from the cache).",
             history_note="Recorded in Tools > History (it is switched on).")
    QTest.mouseClick(d.write_button, Qt.MouseButton.LeftButton)
    assert "Looked up 1 folder" in boxes.said[-1] and "Recorded in Tools > History" in boxes.said[-1]
    assert d.session.items[0].state == WRITTEN
    d.close()


def test_the_history_note_is_left_out_when_nothing_was_written(tmp_path, boxes):
    d = page([make(tmp_path, "A")], view=VIEW_ONE, history_note="Recorded in Tools > History (it is switched on).")
    QTest.mouseClick(d.skip_button, Qt.MouseButton.LeftButton)
    assert "Recorded in Tools > History" not in boxes.said[-1]
    d.close()


def test_closing_with_ticked_notes_asks_and_no_keeps_the_page_open(tmp_path, boxes):
    d = page([make(tmp_path, "A"), make(tmp_path, "B")])
    boxes.answer = QMessageBox.StandardButton.No
    d.reject()
    assert d.isVisible() and "2 ticked notes have not been written" in boxes.questions[-1]
    boxes.answer = QMessageBox.StandardButton.Yes
    d.reject()
    assert not d.isVisible() and not (tmp_path / "A" / "series.md").exists()


def test_a_note_that_could_not_be_written_says_why_at_once(tmp_path, boxes):
    d = page([make(tmp_path, "A")], view=VIEW_ONE)
    (tmp_path / "A" / "about.md").write_text("my own note\n", encoding="utf-8")  # appears after the scan, other name
    QTest.mouseClick(d.write_button, Qt.MouseButton.LeftButton)
    assert any("The note was not written." in m and "already exists" in m for m in boxes.said)
    assert (tmp_path / "A" / "about.md").read_text(encoding="utf-8") == "my own note\n"
    d.close()


def test_the_size_is_reported_when_the_page_closes(tmp_path, boxes):
    sizes = []
    d = page([make(tmp_path, "A")], on_size=lambda w, h: sizes.append((w, h)))
    d.done(0)
    assert len(sizes) == 1 and sizes[0][0] > 300

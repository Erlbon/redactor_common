"""The shared review page driven by its real widgets (offscreen): ticks, Write, Skip, candidate pick and rebuild, the
list and one-at-a-time views. The write function writes real notes into tmp_path through write_proposal."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from redactor_common.core.notes_model import Candidate, Confidence, Proposal, write_proposal  # noqa: E402
from redactor_common.core.notes_review import VIEW_LIST, VIEW_ONE, WRITTEN, ReviewSession  # noqa: E402
from redactor_common.gui.notes_review_dialog import NotesReviewDialog  # noqa: E402
from tests.test_notes_shared import FakeTarget  # noqa: E402

NOTE = "---\ntitle: {name}\n---\n\n# {name}\n\nSome text.\n"


_app = QApplication.instance() or QApplication([])  # held for the life of the process, like the other GUI tests


@pytest.fixture
def app():
    return _app


@pytest.fixture(autouse=True)
def quiet_boxes(monkeypatch):
    """The summary and confirmation boxes are modal: answer them (yes) and remember what they said."""
    said = []
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: said.append(a[2]) or QMessageBox.StandardButton.Ok))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: said.append(a[2]) or QMessageBox.StandardButton.Ok))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    return said


def make(tmp_path, name, confident=True, candidates=()):
    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    return Proposal(FakeTarget(folder, "series", "series"), Confidence.CONFIDENT if confident else Confidence.UNCERTAIN,
                    NOTE.format(name=name) if confident else "", title=name, reasons=[f"{name} matched"],
                    candidates=list(candidates))


def dialog(app, proposals, view=VIEW_LIST, rebuild=None):
    d = NotesReviewDialog(ReviewSession(proposals, write_fn=write_proposal), view,
                          rebuild=rebuild or (lambda p, c: p), banner="Looked up 3 folders")
    d.show()
    return d


def test_list_view_writes_only_the_ticked_confident_notes(app, tmp_path):
    d = dialog(app, [make(tmp_path, "A"), make(tmp_path, "B"), make(tmp_path, "C", confident=False)])
    assert d.stack.currentIndex() == 1 and d.tree.topLevelItemCount() == 3
    assert d.write_ticked_button.text() == "Write 2 ticked notes"
    d.tree.topLevelItem(1).setCheckState(0, Qt.CheckState.Unchecked)  # untick B
    assert d.write_ticked_button.text() == "Write 1 ticked note"
    QTest.mouseClick(d.write_ticked_button, Qt.MouseButton.LeftButton)
    assert (tmp_path / "A" / "series.md").is_file()
    assert not (tmp_path / "B" / "series.md").exists() and not (tmp_path / "C" / "series.md").exists()
    d.close()


def test_an_uncertain_row_cannot_be_ticked_from_the_list(app, tmp_path):
    d = dialog(app, [make(tmp_path, "A", confident=False)])
    row = d.tree.topLevelItem(0)
    assert not (row.flags() & Qt.ItemFlag.ItemIsUserCheckable)
    assert d.session.set_ticked(0, True) is False
    d.close()


def test_one_at_a_time_write_skip_and_the_summary(app, tmp_path, quiet_boxes):
    d = dialog(app, [make(tmp_path, "A"), make(tmp_path, "B")], view=VIEW_ONE)
    assert d.write_all_button.text() == "Write all remaining confident (2)" and d.write_button.text() == "Write note"
    assert d.stack.currentIndex() == 0 and d.header_label.text() == "Create notes  1 of 2"
    QTest.mouseClick(d.write_button, Qt.MouseButton.LeftButton)
    assert (tmp_path / "A" / "series.md").is_file() and d.header_label.text() == "Create notes  2 of 2"
    QTest.mouseClick(d.skip_button, Qt.MouseButton.LeftButton)
    assert not (tmp_path / "B" / "series.md").exists()
    assert quiet_boxes and "Written: 1" in quiet_boxes[-1] and "Skipped: 1" in quiet_boxes[-1]
    assert d.result() == d.DialogCode.Accepted and not d.isVisible()  # nothing left to decide: the page closed itself
    d.close()


def test_picking_a_candidate_rebuilds_the_proposal_and_makes_it_writable(app, tmp_path):
    candidate = Candidate("tmdb", "1396", "Breaking Bad", "2008", hits=["Season 1"], score=0.9)
    uncertain = make(tmp_path, "Breaking Bad", confident=False, candidates=[candidate])
    seen = []

    def rebuild(proposal, chosen):
        seen.append(chosen.id)
        proposal.confidence, proposal.note_text = Confidence.CONFIDENT, NOTE.format(name="Breaking Bad")
        return proposal

    d = dialog(app, [uncertain], view=VIEW_ONE, rebuild=rebuild)
    panel = d.one_panel
    assert panel.candidate_box.isVisibleTo(d) and panel.candidate_table.rowCount() == 1
    assert not d.write_button.isEnabled()
    QTest.mouseClick(panel.candidate_button, Qt.MouseButton.LeftButton)
    assert seen == ["1396"] and d.write_button.isEnabled()
    QTest.mouseClick(d.write_button, Qt.MouseButton.LeftButton)
    assert d.session.items[0].state == WRITTEN and (tmp_path / "Breaking Bad" / "series.md").is_file()
    d.close()


def test_the_view_can_be_switched_and_keeps_the_place(app, tmp_path):
    d = dialog(app, [make(tmp_path, "A"), make(tmp_path, "B")], view=VIEW_ONE)
    d.index = 1
    QTest.mouseClick(d.switch_button, Qt.MouseButton.LeftButton)
    assert d.view == VIEW_LIST and d.tree.currentItem().text(0).endswith("B")
    QTest.mouseClick(d.switch_button, Qt.MouseButton.LeftButton)
    assert d.view == VIEW_ONE and d.index == 1
    d.close()


def test_a_note_that_appeared_meanwhile_is_reported_not_overwritten(app, tmp_path, quiet_boxes):
    d = dialog(app, [make(tmp_path, "A")], view=VIEW_ONE)
    (tmp_path / "A" / "ARTIST.MD").write_text("my own note\n", encoding="utf-8")
    QTest.mouseClick(d.write_button, Qt.MouseButton.LeftButton)
    assert (tmp_path / "A" / "ARTIST.MD").read_text(encoding="utf-8") == "my own note\n"
    assert any("already exists" in m for m in quiet_boxes)
    d.close()


def test_the_columns_can_be_dragged(app, tmp_path):
    from PyQt6.QtWidgets import QHeaderView

    d = dialog(app, [make(tmp_path, "A")])
    assert d.tree.header().sectionResizeMode(0) == QHeaderView.ResizeMode.Interactive
    assert d.list_panel.candidate_table.horizontalHeader().sectionResizeMode(0) == QHeaderView.ResizeMode.Interactive
    d.close()

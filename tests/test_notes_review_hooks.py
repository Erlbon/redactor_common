"""The review page's extras (ReviewHooks), each on and off: the level choice (built at once, Skip waits for its button), the
minimal local note, Look up by link (checked before anything is fetched, an engine refusal said under the box), the genres
box, the Local files tab, Tick best guesses, why a candidate cannot be used, the follow-up run, the troubleshooting log, and
the list rows updated IN PLACE after a pick (same row objects, same scroll position). A page without hooks shows none of
them. Real widgets; the 'engine' is a stub that records its calls."""

import os
from dataclasses import dataclass, field

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QLabel, QMessageBox  # noqa: E402

from redactor_common.core.notes_model import Candidate, Confidence, Proposal, write_proposal  # noqa: E402
from redactor_common.core.notes_review import SKIPPED, VIEW_LIST, VIEW_ONE, ReviewSession  # noqa: E402
from redactor_common.gui.notes_review_dialog import NOT_TICKABLE_HINT, NotesReviewDialog, ReviewHooks  # noqa: E402
from tests.test_notes_shared import FakeTarget  # noqa: E402

_app = QApplication.instance() or QApplication([])
TEXT = "---\ntitle: {name}\n---\n\n# {name}\n\nText.\n"


@dataclass
class LevelProposal(Proposal):
    level: str = ""
    suggested_levels: list = field(default_factory=list)
    genres: list = field(default_factory=list)


@pytest.fixture(autouse=True)
def boxes(monkeypatch):
    said = []
    for name in ("information", "warning"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda *a, **k: said.append(a[2]) or QMessageBox.StandardButton.Ok))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: said.append(a[2]) or QMessageBox.StandardButton.Yes))
    return said


NOTE_OF = {"artist": "artist", "album": "album", "genre": "about"}  # a genre folder's note is about.md


def uncertain(tmp_path, name, kind="artist", **kw):
    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    return LevelProposal(FakeTarget(folder, kind, NOTE_OF[kind]), Confidence.UNCERTAIN, "", title=name,
                         candidates=[Candidate("mb", "1", f"{name} (band)", "1980", [], 0.6)], **kw)


def confident(tmp_path, name, kind="artist"):
    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    return LevelProposal(FakeTarget(folder, kind, NOTE_OF[kind]), Confidence.CONFIDENT, TEXT.format(name=name), title=name)


class Engine:
    """rebuild(proposal, candidate[, level, genres=, link=]): a confident note, and every call recorded."""

    def __init__(self, refuse_link=False):
        self.calls, self.refuse_link = [], refuse_link

    def rebuild(self, proposal, candidate, level="", **extra):
        self.calls.append((proposal.folder.name, candidate.name if candidate else None, level, extra))
        if self.refuse_link and "link" in extra:
            raise ValueError("That page is about a film, not a band.")
        return LevelProposal(proposal.target, Confidence.CONFIDENT, TEXT.format(name=proposal.folder.name),
                             title=proposal.title)


def page(proposals, view=VIEW_LIST, hooks=None, engine=None):
    engine = engine or Engine()
    d = NotesReviewDialog(ReviewSession(proposals, write_fn=write_proposal), view, rebuild=engine.rebuild, hooks=hooks)
    d.show()
    return d, engine


MUSIC = dict(level_labels={"artist": "Artist folder", "album": "Album folder", "skip": "Skip this folder"},
             local_level="local", local_label="Create a minimal note from the tags", can_make_local=lambda p: True,
             link_placeholder=lambda p: "Paste a link", genres_of=lambda p: p.genres if p.kind == "genre" else None)


def test_a_page_without_hooks_shows_none_of_the_extras(tmp_path):
    d, _ = page([uncertain(tmp_path, "A", suggested_levels=["album", "skip"])])
    panel = d.list_panel
    assert not any(w.isVisibleTo(d) for w in (panel.level_box, panel.local_box, panel.link_box, panel.genres_box,
                                              d.tick_guesses_button, d.followup_button))
    assert panel.evidence_view is None and panel.candidate_box.isVisibleTo(d)


def test_a_level_is_built_at_once_and_skip_waits_for_its_button(tmp_path):
    d, engine = page([uncertain(tmp_path, "A", suggested_levels=["album", "skip"])], hooks=ReviewHooks(**MUSIC))
    panel = d.list_panel
    assert panel.level_box.isVisibleTo(d) and panel.level_combo.count() == 3  # Choose..., Album folder, Skip this folder
    panel.level_combo.setCurrentIndex(2)
    panel._level_activated(2)
    assert engine.calls == [] and panel.level_button.isVisibleTo(d)  # Skip: nothing yet
    panel._level_activated(1)
    assert engine.calls == [("A", None, "album", {})] and d.session.items[0].ticked


def test_skip_from_the_level_choice_marks_the_row_skipped(tmp_path):
    d, _ = page([uncertain(tmp_path, "A", suggested_levels=["album", "skip"]), confident(tmp_path, "B")], hooks=ReviewHooks(**MUSIC))
    d._choose_level("skip")
    assert d.session.items[0].state == SKIPPED


def test_the_local_note_is_offered_only_where_nothing_was_found_online(tmp_path):
    d, engine = page([uncertain(tmp_path, "A")], hooks=ReviewHooks(**MUSIC))
    assert d.list_panel.local_box.isVisibleTo(d) and d.list_panel.local_button.text() == "Create a minimal note from the tags"
    d.list_panel.local_button.click()
    assert engine.calls[-1][2] == "local" and not d.list_panel.local_box.isVisibleTo(d)  # now confident: no button


def test_a_link_is_checked_first_and_an_engine_refusal_is_said_under_the_box(tmp_path):
    hooks = ReviewHooks(**{**MUSIC, "check_link": lambda p, text: "" if text.startswith("https://") else "Not a link."})
    d, engine = page([uncertain(tmp_path, "A")], hooks=hooks, engine=Engine(refuse_link=True))
    panel = d.list_panel
    panel.link_edit.setText("hello")
    panel._link_clicked()
    assert panel.link_error.text() == "Not a link." and engine.calls == []  # nothing fetched
    panel.link_edit.setText("https://en.wikipedia.org/wiki/Heat_(film)")
    panel._link_clicked()
    assert engine.calls[-1][3] == {"link": "https://en.wikipedia.org/wiki/Heat_(film)"}
    assert panel.link_error.text() == "That page is about a film, not a band." and not d.session.items[0].ticked


def test_a_typed_link_belongs_to_its_row(tmp_path):
    d, _ = page([uncertain(tmp_path, "A"), uncertain(tmp_path, "B")], hooks=ReviewHooks(**MUSIC))
    d.list_panel.link_edit.setText("https://musicbrainz.org/artist/x")
    d._select_tree_index(1)
    assert d.list_panel.link_edit.text() == ""


def test_the_genres_box_looks_the_folder_up_again_with_the_edited_genres(tmp_path):
    d, engine = page([uncertain(tmp_path, "Ska", kind="genre", genres=["Ska"])], hooks=ReviewHooks(**MUSIC))
    panel = d.list_panel
    assert panel.genres_box.isVisibleTo(d) and panel.genres_edit.text() == "Ska"
    panel.genres_edit.setText("Ska, Ska punk")
    panel._genres_clicked()
    assert engine.calls[-1][3] == {"genres": ["Ska", "Ska punk"]}


class FakeEvidence(QLabel):
    def __init__(self):
        super().__init__()
        self.shown, self._pending = [], None

    def show_folder(self, folder, kind):
        self._pending = (str(folder), kind)
        self.shown.append(folder.name)


def test_the_local_files_tab_opens_by_itself_on_a_row_with_no_draft(tmp_path):
    hooks = ReviewHooks(evidence_view=FakeEvidence, evidence_applies=lambda p: p.kind == "album")
    d, _ = page([uncertain(tmp_path, "A", kind="album"), confident(tmp_path, "B", kind="album")], hooks=hooks)
    panel = d.list_panel
    assert panel.tabs.currentIndex() == panel.evidence_tab and panel.evidence_view.shown == ["A"]
    d._select_tree_index(1)
    assert panel.tabs.currentIndex() == 0  # a row with a draft goes back to the preview


def test_tick_best_guesses_rebuilds_the_uncertain_rows_with_a_guess_and_ticks_them(tmp_path):
    rows = [uncertain(tmp_path, "A"), uncertain(tmp_path, "B"), confident(tmp_path, "C")]
    rows[1].candidates = []
    hooks = ReviewHooks(best_guess=lambda p: p.candidates[0] if p.candidates else None)
    d, engine = page(rows, hooks=hooks)
    assert d.tick_guesses_button.isVisibleTo(d) and d.guess_indices() == [0]
    d._tick_best_guesses()
    assert [c[:2] for c in engine.calls] == [("A", "A (band)")] and d.session.items[0].ticked and not d.session.items[1].ticked


def test_why_a_candidate_cannot_be_used_is_said(tmp_path):
    hooks = ReviewHooks(why_not=lambda p, c: "Discogs token not set.")
    d = NotesReviewDialog(ReviewSession([uncertain(tmp_path, "A")], write_fn=write_proposal), VIEW_LIST,
                          rebuild=lambda p, c: p, can_choose=lambda p, c: False, hooks=hooks)
    assert d.list_panel.candidate_note.text() == "Discogs token not set." and not d.list_panel.candidate_button.isEnabled()


def test_a_pick_updates_the_row_in_place_and_keeps_the_scroll_position(tmp_path):
    rows = [confident(tmp_path, f"Band {i:02d}") for i in range(60)] + [uncertain(tmp_path, "Zed")]
    d, _ = page(rows, hooks=ReviewHooks(**MUSIC))
    d.resize(900, 400)
    QApplication.processEvents()
    d._select_tree_index(60)
    QApplication.processEvents()
    row_before = d.tree.currentItem()
    scroll = d.tree.verticalScrollBar().value()
    assert scroll > 0 and d.row_hint.text().endswith(NOT_TICKABLE_HINT)
    d._choose_candidate(rows[60].candidates[0])
    assert d.tree.currentItem() is row_before and d.tree.verticalScrollBar().value() == scroll
    assert d.tree.topLevelItemCount() == 61 and d.session.items[60].ticked


def test_the_follow_up_is_offered_for_the_written_rows_of_its_kind(tmp_path, boxes):
    hooks = ReviewHooks(followup_kind="artist", followup_label="Create the album notes for these artists too",
                        followup_question=lambda n: f"Create the album notes for {n} artist(s) too?")
    d, _ = page([confident(tmp_path, "A"), confident(tmp_path, "B", kind="album")], view=VIEW_ONE, hooks=hooks)
    d.write_current()  # A (artist)
    assert d.followup_button.isVisibleTo(d)
    d.write_current()  # B, the last: the summary, then the question
    assert any("Create the album notes for 1 artist(s) too?" in s for s in boxes)
    assert d.followup == [str(tmp_path / "A")]


def test_the_log_gets_counts_and_folder_names_only(tmp_path):
    lines = []
    d, _ = page([uncertain(tmp_path, "A"), confident(tmp_path, "B")], hooks=ReviewHooks(log=lambda step, **f: lines.append((step, f))))
    d._select_tree_index(1)
    step, fields = lines[-1]
    assert step == "current" and fields["touched"] == ["B"] and fields["rows"] == 2

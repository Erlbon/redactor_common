"""run_create_notes (gui/notes_run.py): scan under a progress dialog, look up under a cancellable one, open the review page.
The real progress machinery runs (offscreen); the review page's exec() and the message boxes are replaced."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import dataclass, field  # noqa: E402

import pytest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox, QWidget  # noqa: E402

from redactor_common.core.notes_model import Confidence, Proposal, build_each  # noqa: E402
from redactor_common.core.notes_phases import PhaseSet, PhaseTimer  # noqa: E402
from redactor_common.core.notes_cache import RequestStats  # noqa: E402
from redactor_common.gui import notes_run  # noqa: E402
from redactor_common.gui.notes_review_dialog import NotesReviewDialog  # noqa: E402
from tests.test_notes_shared import FakeTarget  # noqa: E402

_PARENTS: list = []


def keep() -> QWidget:
    """A parent widget that outlives the test body (a temporary parent takes its children with it)."""
    widget = QWidget()
    _PARENTS.append(widget)
    return widget


_app = QApplication.instance() or QApplication([])  # held for the life of the process, like the other GUI tests


@pytest.fixture
def app():
    return _app


@dataclass
class Found:
    targets: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    missing: list = field(default_factory=list)


class Engine:
    def __init__(self):
        self.phases = PhaseTimer(PhaseSet({"scan": "scan", "tmdb": "TMDB"}, {"scan": "folders", "tmdb": "requests"}))
        self.stats = RequestStats(requests=3, from_cache=1)
        self.rebuilt = []

    def build_proposal(self, target):
        return Proposal(target, Confidence.CONFIDENT, "---\ntitle: x\n---\n\nBody\n", title=target.folder.name)

    def rebuild(self, proposal, candidate=None):
        self.rebuilt.append(candidate)
        return proposal

    def can_choose(self, proposal, candidate):
        return True


def build(targets, progress=None, cancel=None, on_proposal=None, engine=None):
    return build_each(targets, engine.build_proposal, progress, cancel, on_proposal)


class Said(list):
    """The texts the message boxes showed; `.shown` are the review pages that were opened."""

    shown: list


@pytest.fixture
def boxes(monkeypatch):
    said = Said()
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: said.append(a[2]) or QMessageBox.StandardButton.Ok))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: said.append(a[2]) or QMessageBox.StandardButton.Ok))
    shown = []
    monkeypatch.setattr(NotesReviewDialog, "exec", lambda self: shown.append(self) or 0)
    said.shown = shown
    return said


def run(parent, found, engine=None, **kw):
    engine = engine or Engine()
    return notes_run.run_create_notes(parent, title="Create Notes", scan=lambda: found, engine=engine, build=build, **kw), engine


def test_the_folders_are_looked_up_and_the_review_page_opens_with_the_banner(app, tmp_path, boxes):
    found = Found(targets=[FakeTarget(tmp_path / n) for n in ("A", "B", "C")], skipped=["x"])
    dialog, engine = run(keep(), found, intro_notes=["Series and films."])
    assert dialog is boxes.shown[0] and len(dialog.session.items) == 3
    banner = dialog.banner_label.text()
    assert "Series and films." in banner and "1 folder(s) already have a note" in banner
    assert "Looked up 3 folders" in banner and "3 requests, 1 from the cache" in banner
    assert engine.phases.count("scan") == 3


def test_nothing_to_do_says_why_and_opens_no_page(app, tmp_path, boxes):
    found = Found(skipped=["a", "b"], missing=["Z:/gone"])
    dialog, _ = run(keep(), found, nothing_found="No series or films without a note were found.")
    assert dialog is None and not boxes.shown
    assert "No series or films" in boxes[0] and "2 folder(s) already have a note" in boxes[0] and "Z:/gone" in boxes[0]


def test_a_scan_that_fails_is_a_message_not_a_crash(app, boxes):
    def broken():
        raise OSError("the share is gone")

    dialog = notes_run.run_create_notes(keep(), title="Create Notes", scan=broken, engine=Engine(), build=build)
    assert dialog is None and "the share is gone" in boxes[0]


def test_a_failure_in_the_lookup_keeps_what_was_built_and_says_so(app, tmp_path, boxes):
    found = Found(targets=[FakeTarget(tmp_path / n) for n in ("A", "B", "C")])
    engine = Engine()

    def breaking_build(targets, progress=None, cancel=None, on_proposal=None, engine=None):
        on_proposal(engine.build_proposal(targets[0]))
        raise RuntimeError("TMDB fell over")

    dialog = notes_run.run_create_notes(keep(), title="Create Notes", scan=lambda: found, engine=engine, build=breaking_build)
    assert len(dialog.session.items) == 1 and "The lookup stopped early: TMDB fell over" in dialog.banner_label.text()


def test_nothing_built_says_so(app, tmp_path, boxes):
    found = Found(targets=[FakeTarget(tmp_path / "A")])

    def empty_build(*a, **k):
        raise RuntimeError("no network at all")

    dialog = notes_run.run_create_notes(keep(), title="Create Notes", scan=lambda: found, engine=Engine(), build=empty_build)
    assert dialog is None and "Nothing could be looked up" in boxes[0] and "no network at all" in boxes[0]


def test_unreachable_services_are_named_in_the_banner(app, tmp_path, boxes):
    found = Found(targets=[FakeTarget(tmp_path / "A")])

    class Down(Engine):
        def build_proposal(self, target):
            p = super().build_proposal(target)
            p.problems = ["Could not reach TMDB: no network"]
            return p

    dialog, _ = run(keep(), found, engine=Down())
    assert "The internet services could not be reached: Could not reach TMDB: no network" in dialog.banner_label.text()


def test_the_remembered_size_round_trips():
    assert notes_run.parse_size("1100x700") == (1100, 700)
    assert notes_run.parse_size("10x10") is None and notes_run.parse_size("nonsense") is None and notes_run.parse_size("") is None

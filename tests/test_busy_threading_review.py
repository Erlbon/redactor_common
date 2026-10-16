"""Regression tests for the 2026-10-16 independent review, 'shared progress dialogs and threading' findings.
Real offscreen widgets, real QProgressDialog, real clicks. Every test here FAILED on the code before the fix."""

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import QEvent, QCoreApplication, Qt, QTimer  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QLineEdit, QMenu, QMessageBox, QProgressDialog, QPushButton, QVBoxLayout, QWidget,
)

from redactor_common.core.notes_model import Candidate, Confidence, Proposal, write_proposal  # noqa: E402
from redactor_common.core.notes_review import VIEW_LIST, ReviewSession  # noqa: E402
from redactor_common.gui import progress, work_progress  # noqa: E402
from redactor_common.gui.notes_review_dialog import NotesReviewDialog  # noqa: E402
from redactor_common.gui.progress import run_busy  # noqa: E402
from tests.test_notes_shared import FakeTarget  # noqa: E402

_app = QApplication.instance() or QApplication([])
_KEEP: list = []


def window_with_button():
    """A top-level window with a text field (to check the focus comes back) and a button that starts a run_busy."""
    win = QWidget()
    layout = QVBoxLayout(win)
    edit = QLineEdit()
    button = QPushButton("Go")
    layout.addWidget(edit)
    layout.addWidget(button)
    win.edit, win.button = edit, button
    win.show()
    win.activateWindow()
    _KEEP.append(win)
    return win


def drain():
    """Process events and the queued deleteLater()s (they only run from a top-level loop, never inside exec())."""
    for _ in range(3):
        _app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def dialogs_of(parent, cls=QProgressDialog):
    return parent.findChildren(cls)


def visible_dialog(parent):
    return next((d for d in dialogs_of(parent) if d.isVisible()), None)


def press_cancel(dialog):
    dialog.findChild(QPushButton).click()  # the real Cancel button


# -- 1. the dialog appears after ~300 ms and the window is blocked from the start ---------------------------------------

def test_busy_dialog_appears_at_the_intended_delay_not_after_four_seconds():
    win = window_with_button()
    seen = []
    started = time.perf_counter()

    def poll():
        if visible_dialog(win) is not None and not seen:
            seen.append(time.perf_counter() - started)

    timer = QTimer()
    timer.timeout.connect(poll)
    timer.start(20)
    result = run_busy(win, "Working...", lambda: (time.sleep(3.0), "ok")[1])
    timer.stop()
    assert result == "ok"
    assert seen, "the progress dialog never became visible during a 3 s call"
    assert 0.25 <= seen[0] <= 0.8, f"dialog first visible after {seen[0]:.2f} s"


def test_a_click_during_the_call_does_not_start_a_second_call_and_the_window_comes_back():
    win = window_with_button()
    calls = []
    gone = {}

    def slow():
        calls.append(1)
        time.sleep(0.8)
        return "ok"

    win.button.clicked.connect(lambda: run_busy(win, "Working...", slow))
    win.button.setFocus()
    # clicks at 100 ms (before the dialog is shown) and 500 ms (after it is shown), plus a key press and Enter
    QTimer.singleShot(100, lambda: (gone.setdefault("enabled_during", win.isEnabled()),
                                    QTest.mouseClick(win.button, Qt.MouseButton.LeftButton)))
    QTimer.singleShot(150, lambda: QTest.keyClick(win.button, Qt.Key.Key_Space))
    QTimer.singleShot(500, lambda: QTest.mouseClick(win.button, Qt.MouseButton.LeftButton))
    QTest.mouseClick(win.button, Qt.MouseButton.LeftButton)
    drain()
    assert len(calls) == 1, f"{len(calls)} calls were started"
    assert gone["enabled_during"] is False
    assert win.isEnabled()
    assert win.focusWidget() is win.button  # the click gave the button the focus; it is back after the call
    assert QApplication.overrideCursor() is None


def test_the_window_is_enabled_again_after_an_exception_in_the_call():
    win = window_with_button()

    def boom():
        time.sleep(0.3)
        raise ValueError("bad")

    during = []
    QTimer.singleShot(100, lambda: during.append((win.isEnabled(), QApplication.overrideCursor() is not None)))
    with pytest.raises(ValueError):
        run_busy(win, "Working...", boom)
    assert during == [(False, True)]  # blocked, busy cursor, from the start
    assert win.isEnabled() and QApplication.overrideCursor() is None


def test_a_window_the_caller_had_disabled_stays_disabled():
    """videoredactor's lookup disables its owner itself: the previous state comes back, not blindly True."""
    win = window_with_button()
    win.setEnabled(False)
    during = []
    QTimer.singleShot(100, lambda: during.append(win.isEnabled()))
    assert run_busy(win, "Working...", lambda: (time.sleep(0.3), 5)[1]) == 5
    assert during == [False] and not win.isEnabled()
    win.setEnabled(True)


def test_the_cancel_button_of_the_busy_dialog_still_works_while_the_window_is_disabled():
    win = window_with_button()
    QTimer.singleShot(500, lambda: press_cancel(visible_dialog(win)))
    assert run_busy(win, "Working...", lambda: time.sleep(3), cancellable=True, on_cancel="stopped") == "stopped"
    assert win.isEnabled()


def test_a_quick_call_never_shows_the_disabled_look():
    """The window is disabled from the first moment, but repaints are held back: a short call must not flash grey."""
    from PyQt6.QtCore import QObject

    win = window_with_button()
    drain()
    painted_disabled = []

    class Watch(QObject):
        def eventFilter(self, obj, event):
            if event.type() == QEvent.Type.Paint and not obj.isEnabled():
                painted_disabled.append(obj)
            return False

    watch = Watch()
    win.button.installEventFilter(watch)
    for _ in range(5):
        run_busy(win, "Working...", lambda: time.sleep(0.1))
        drain()
    assert painted_disabled == [] and win.updatesEnabled() and win.isEnabled()


# -- 2. Cancel keeps the counted dialog up until the worker has returned ---------------------------------------------------

def _slow_stop_adapter(parent):
    """progress.run_work_with_progress: a worker that needs 1.2 s to stop after Cancel."""
    def work(progress_cb, cancel):
        for _ in range(500):
            if cancel.is_set():
                time.sleep(1.2)
                return "stopped"
            time.sleep(0.01)
        return "ran"
    return progress.run_work_with_progress(parent, "Working...", work, 10)


def _slow_stop_real(parent):
    def work(p):
        for _ in range(500):
            if p.should_cancel():
                time.sleep(1.2)
                return "stopped"
            time.sleep(0.01)
        return "ran"
    return work_progress.run_work_with_progress(parent, 10, "Working...", work)[0]


@pytest.mark.parametrize("runner", [_slow_stop_adapter, _slow_stop_real], ids=["progress", "work_progress"])
def test_cancel_keeps_the_dialog_visible_and_modal_until_the_worker_returns(runner):
    win = window_with_button()
    probe = {}

    def cancel():
        press_cancel(visible_dialog(win))

    def check():
        d = next(iter(dialogs_of(win)), None)
        probe["visible"] = d is not None and d.isVisible()
        probe["text"] = d.labelText() if d is not None else ""
        probe["modal"] = d is not None and d.windowModality() == Qt.WindowModality.WindowModal
        probe["enabled"] = win.isEnabled()

    QTimer.singleShot(500, cancel)
    QTimer.singleShot(1100, check)  # the worker is still sleeping its 1.2 s
    assert runner(win) == "stopped"
    assert probe["visible"], "the dialog was hidden while the worker still ran"
    assert probe["text"].startswith("Cancelling")
    assert probe["modal"] and probe["enabled"] is False
    assert win.isEnabled()
    drain()
    assert dialogs_of(win) == []


# -- 7. the two run_work_with_progress behave the same ---------------------------------------------------------------------

def test_both_run_work_with_progress_functions_give_the_same_result_cancel_and_exception():
    win = window_with_button()

    def body_old(progress_cb, cancel):
        progress_cb(1, 3)
        return "result"

    def body_new(p):
        p.report(1, 3)
        return "result"

    assert progress.run_work_with_progress(win, "x", body_old, 3) == "result"
    assert work_progress.run_work_with_progress(win, 3, "x", body_new) == ("result", False)

    def boom_old(progress_cb, cancel):
        raise ValueError("boom")

    def boom_new(p):
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        progress.run_work_with_progress(win, "x", boom_old, 3)
    with pytest.raises(ValueError, match="boom"):
        work_progress.run_work_with_progress(win, 3, "x", boom_new)

    outcomes = {}

    def cancel_old(progress_cb, cancel):
        while not cancel.wait(0.01):
            pass
        return "cancelled"

    def cancel_new(p):
        while not p.should_cancel():
            time.sleep(0.01)
        return "cancelled"

    QTimer.singleShot(500, lambda: press_cancel(visible_dialog(win)))
    outcomes["old"] = progress.run_work_with_progress(win, "x", cancel_old, 3)
    QTimer.singleShot(500, lambda: press_cancel(visible_dialog(win)))
    outcomes["new"] = work_progress.run_work_with_progress(win, 3, "x", cancel_new)
    assert outcomes == {"old": "cancelled", "new": ("cancelled", True)}
    # the older signature is a thin adapter over the one implementation
    assert "work_progress" in progress.run_work_with_progress.__doc__


# -- 3. the selection moves during a rebuild: the result lands on the row it was started for --------------------------------

NOTE = "---\ntitle: {name}\n---\n\n# {name}\n\nSome text.\n"


def make(tmp_path, name, candidates=()):
    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    return Proposal(FakeTarget(folder, "series", "series"), Confidence.UNCERTAIN, "", title=name, reasons=[f"{name} matched"],
                    candidates=list(candidates))


@pytest.fixture(autouse=True)
def quiet_boxes(monkeypatch):
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))


def test_review_page_rebuild_lands_on_the_row_it_was_started_for_even_if_the_selection_moves(tmp_path):
    cand = Candidate("tmdb", "1", "Show A", "2008", hits=[], score=0.9)
    a, b = make(tmp_path, "A", [cand]), make(tmp_path, "B", [cand])
    calls = []

    def rebuild(proposal, chosen):
        calls.append(proposal.title)
        time.sleep(0.6)
        return Proposal(proposal.target, Confidence.CONFIDENT, NOTE.format(name="REBUILT"), title="REBUILT")

    d = NotesReviewDialog(ReviewSession([a, b], write_fn=write_proposal), VIEW_LIST, rebuild=rebuild)
    d.show()
    _KEEP.append(d)
    item_b = d.session.items[1]
    d.tree.setCurrentItem(d.tree.topLevelItem(0))
    # the selection moves to row B while the rebuild of row A runs (programmatically: it is what the race looked like)
    QTimer.singleShot(200, lambda: d.tree.setCurrentItem(d.tree.topLevelItem(1)))
    QTest.mouseClick(d.list_panel.candidate_button, Qt.MouseButton.LeftButton)
    assert calls == ["A"]
    assert d.session.items[0].proposal.title == "REBUILT"
    assert d.session.items[1] is item_b and item_b.proposal is b and b.title == "B" and b.note_text == ""
    assert [i.proposal.title for i in d.session.items].count("REBUILT") == 1
    d.close()


def test_review_page_a_second_choice_while_the_first_rebuild_runs_is_ignored(tmp_path):
    cand = Candidate("tmdb", "1", "Show A", "2008", hits=[], score=0.9)
    calls = []

    def rebuild(proposal, chosen):
        calls.append(1)
        time.sleep(0.5)
        return Proposal(proposal.target, Confidence.CONFIDENT, NOTE.format(name="X"), title="X")

    d = NotesReviewDialog(ReviewSession([make(tmp_path, "A", [cand])], write_fn=write_proposal), VIEW_LIST, rebuild=rebuild)
    d.show()
    _KEEP.append(d)
    QTimer.singleShot(100, lambda: d._choose_candidate(cand))  # what a double click / Enter would do
    d._choose_candidate(cand)
    assert calls == [1]
    d.close()


def test_the_item_that_vanished_during_the_rebuild_drops_the_late_result(tmp_path):
    cand = Candidate("tmdb", "1", "Show A", "2008", hits=[], score=0.9)
    a, b = make(tmp_path, "A", [cand]), make(tmp_path, "B", [cand])

    def rebuild(proposal, chosen):
        time.sleep(0.3)
        return Proposal(proposal.target, Confidence.CONFIDENT, NOTE.format(name="X"), title="X")

    d = NotesReviewDialog(ReviewSession([a, b], write_fn=write_proposal), VIEW_LIST, rebuild=rebuild)
    d.show()
    _KEEP.append(d)
    d.tree.setCurrentItem(d.tree.topLevelItem(0))
    QTimer.singleShot(100, lambda: d.session.items.reverse())  # the list under the row was reordered
    d._choose_candidate(cand)
    assert [i.proposal.title for i in d.session.items] == ["B", "A"]  # nothing was overwritten
    assert "dropped" in d.status_text
    d.close()


# -- 5. the filter toggle keeps a current row ------------------------------------------------------------------------------

def test_filter_toggle_leaves_a_current_row(tmp_path):
    confident = Proposal(FakeTarget(tmp_path / "C", "series", "series"), Confidence.CONFIDENT, NOTE.format(name="C"), title="C")
    (tmp_path / "C").mkdir()
    d = NotesReviewDialog(ReviewSession([confident, make(tmp_path, "A"), make(tmp_path, "B")], write_fn=write_proposal),
                          VIEW_LIST, rebuild=lambda p, c: p)
    d.show()
    _KEEP.append(d)
    d.filter_check.setChecked(True)
    assert d.tree.topLevelItemCount() == 2 and d.tree.currentItem() is not None
    assert d.list_panel.path_label.text() != "Nothing selected"
    d.filter_check.setChecked(False)
    assert d.tree.topLevelItemCount() == 3 and d.tree.currentItem() is not None
    d.close()


# -- 6. no widgets left behind -----------------------------------------------------------------------------------------------

def test_no_progress_dialogs_pile_up_on_the_parent():
    win = window_with_button()
    for _ in range(20):
        run_busy(win, "Working...", lambda: 1)
    for _ in range(5):
        progress.run_work_with_progress(win, "x", lambda p, c: 1, 1)
        work_progress.run_work_with_progress(win, 1, "x", lambda p: 1)
        with progress.ProgressReporter(win, 5, "x") as reporter:
            reporter.set_value(2)
        progress.run_with_progress(win, range(4), lambda item, i: None, "x")
    drain()
    assert dialogs_of(win) == []


def test_no_menus_pile_up_on_the_column_filter_header():
    from tests.test_column_filter import ROWS, _table

    table, header = _table(_app, ROWS)
    for _ in range(20):
        QTimer.singleShot(30, lambda: header._open_menu.close())
        header.open_popup(1)
    drain()
    assert header.findChildren(QMenu) == []


def test_the_create_notes_review_dialog_and_scope_dialog_are_deleted_after_use(tmp_path, monkeypatch):
    from dataclasses import dataclass, field

    from redactor_common.core.notes_model import build_each
    from redactor_common.gui import notes_run
    from redactor_common.gui.notes_scope_dialog import NotesScopeDialog, ask_scope
    from tests.test_notes_run import Engine, Found, build

    parent = QWidget()
    _KEEP.append(parent)
    monkeypatch.setattr(NotesReviewDialog, "exec", lambda self: 0)
    for _ in range(3):
        found = Found(targets=[FakeTarget(tmp_path / n) for n in ("A", "B")])
        notes_run.run_create_notes(parent, title="Create Notes", scan=lambda: found, engine=Engine(), build=build)
    from PyQt6.QtWidgets import QDialog, QFileDialog
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: ""))
    monkeypatch.setattr(NotesScopeDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    for _ in range(3):
        ask_scope(parent, [], True, kind_checks=(), kinds="series", parse_kinds=lambda t: ("series",),
                  kinds_text=lambda ks: "series", describe=lambda p: None, caption_of=lambda *a: "",
                  count=lambda *a: None, hint=lambda *a: "")
    drain()
    assert parent.findChildren(NotesReviewDialog) == [] and parent.findChildren(NotesScopeDialog) == []
    assert dataclass and field and build_each  # (imports kept for readability of the failure above)

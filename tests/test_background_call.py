"""Tests for gui/background_call.py and LookupDialogBase running its
searches off the GUI thread (2026-09-28: every lookup request used to
freeze the whole window)."""

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import QObject, QTimer, pyqtSignal  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from redactor_common.gui.background_call import BackgroundCancelled, call_in_background  # noqa: E402
from redactor_common.gui.lookup_dialog import LookupDialogBase, LookupResult  # noqa: E402

_app = QApplication.instance() or QApplication([])


def test_returns_the_result_and_runs_on_another_thread():
    main = threading.get_ident()
    result = call_in_background(lambda a, b: (a + b, threading.get_ident()), 2, 3)
    assert result[0] == 5
    assert result[1] != main


def test_exceptions_are_re_raised_on_the_caller():
    def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        call_in_background(boom)


def test_gui_events_keep_running_while_waiting():
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(10)
    call_in_background(time.sleep, 0.3)
    timer.stop()
    assert len(ticks) >= 5  # the event loop kept turning -- nothing froze


class _Cancel(QObject):
    fired = pyqtSignal()


def test_cancel_returns_at_once_without_waiting_for_the_call():
    cancel = _Cancel()
    QTimer.singleShot(50, cancel.fired.emit)
    started = time.perf_counter()
    with pytest.raises(BackgroundCancelled):
        call_in_background(time.sleep, 3, cancel_signal=cancel.fired)
    assert time.perf_counter() - started < 1.5


def test_a_call_that_finishes_instantly_still_works():
    assert call_in_background(lambda: "fast") == "fast"


# ---------------------------------------------------------------------------
# LookupDialogBase
# ---------------------------------------------------------------------------

class _Dialog(LookupDialogBase):
    def __init__(self, items, search_one, **kwargs):
        super().__init__(
            items, None,
            window_title="t", info_text="i", search_label="Searching…",
            item_label=str, search_one=search_one,
            query_fields=[("q", "Q")], **kwargs,
        )


def test_dialog_searches_off_the_gui_thread_and_has_results_when_constructed():
    main = threading.get_ident()
    threads = []

    def search_one(item, _override):
        threads.append(threading.get_ident())
        return LookupResult(fields={"title": item.upper()}, used_query={"q": item})

    dialog = _Dialog(["a", "b"], search_one)
    assert all(t != main for t in threads)
    assert dialog.accepted_metadata() == {0: {"title": "A"}, 1: {"title": "B"}}


def test_search_this_item_runs_off_the_gui_thread():
    main = threading.get_ident()
    calls = []

    def search_one(item, override):
        calls.append((threading.get_ident(), dict(override)))
        return LookupResult(fields={"title": override.get("q", item)}, used_query={"q": override.get("q", item)})

    dialog = _Dialog(["a"], search_one)
    dialog.table.selectRow(0)
    dialog._query_edits["q"].setText("corrected")
    dialog._search_current_row()

    assert calls[-1] == (calls[-1][0], {"q": "corrected"}) and calls[-1][0] != main
    assert dialog.accepted_metadata() == {0: {"title": "corrected"}}


def test_search_exception_still_surfaces():
    def search_one(_item, _override):
        raise RuntimeError("bug in a subclass")

    with pytest.raises(RuntimeError, match="bug in a subclass"):
        _Dialog(["a"], search_one)

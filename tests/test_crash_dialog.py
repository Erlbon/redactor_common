"""The shared visible error box: one non-stacking dialog, counts further errors, shows worker-thread errors, keeps the
crash log entry, ignores KeyboardInterrupt/SystemExit, rate-limits identical tracebacks, never raises."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import threading
import time

import pytest
from PyQt6.QtWidgets import QApplication, QWidget

from redactor_common.core import crash_log
from redactor_common.gui import crash_dialog


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def reporter(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(crash_dialog, "_reporter", None)
    rep = crash_dialog.install(tmp_path / "crash.log")
    yield rep
    if rep.dialog is not None:
        rep.dialog.close()
    qapp.processEvents()


def boom(message="boom"):
    try:
        raise ValueError(message)
    except ValueError as exc:
        return type(exc), exc, exc.__traceback__


def pump(qapp, until, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end and not until():
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()


def test_one_dialog_with_message_traceback_and_log_path(qapp, reporter, tmp_path):
    reporter.report(*boom("disk says no"), context="Save All stopped because of an unexpected error")
    qapp.processEvents()
    dialog = reporter.dialog
    assert dialog is not None and dialog.windowTitle() == "Something went wrong"
    assert dialog.summary_label.text() == "Save All stopped because of an unexpected error: ValueError: disk says no"
    assert "Traceback" in dialog.details_text() and "ValueError: disk says no" in dialog.details_text()
    assert dialog.details_edit.isHidden()  # collapsed until Details is opened
    dialog.details_toggle.setChecked(True)
    assert not dialog.details_edit.isHidden()
    assert str(tmp_path / "crash.log") in dialog.log_label.text()
    assert {b.text() for b in dialog.findChildren(type(dialog.copy_button))} >= {"Copy Details", "Open Crash Log Folder"}
    assert reporter.dialogs_opened == 1


def test_a_second_error_is_counted_in_the_open_dialog_not_stacked(qapp, reporter):
    reporter.report(*boom("first"))
    qapp.processEvents()
    first = reporter.dialog
    reporter.report(*boom("second"))
    reporter.report(*boom("third"))
    qapp.processEvents()
    assert reporter.dialog is first and reporter.dialogs_opened == 1
    assert first.count == 3
    assert not first.count_label.isHidden() and "2 more" in first.count_label.text()
    assert "second" in first.details_text() and "third" in first.details_text()


def test_a_worker_thread_error_is_shown_on_the_gui_thread(qapp, reporter):
    seen = {}

    def work():
        try:
            raise RuntimeError("from a worker")
        except RuntimeError as exc:
            reporter.report(type(exc), exc, exc.__traceback__)

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()
    pump(qapp, lambda: reporter.dialog is not None)
    assert reporter.dialog is not None and "from a worker" in reporter.dialog.summary_label.text()
    assert reporter.dialog.thread() is qapp.thread()
    seen["ok"] = True


def test_keyboard_interrupt_and_system_exit_show_nothing(qapp, reporter):
    for exc in (KeyboardInterrupt(), SystemExit(1)):
        try:
            raise exc
        except BaseException as caught:  # noqa: BLE001
            reporter.report(type(caught), caught, caught.__traceback__)
    qapp.processEvents()
    assert reporter.dialog is None and reporter.dialogs_opened == 0


def test_the_same_traceback_again_right_after_closing_is_only_counted(qapp, reporter):
    def same():
        try:
            raise ValueError("same")
        except ValueError as exc:
            return type(exc), exc, exc.__traceback__

    reporter.report(*same())
    qapp.processEvents()
    reporter.dialog.close()
    qapp.processEvents()
    assert reporter.dialog is None
    reporter.report(*same())
    qapp.processEvents()
    assert reporter.dialog is None and reporter.suppressed == 1 and reporter.dialogs_opened == 1
    reporter.report(*boom("different"))  # another error is not rate limited
    qapp.processEvents()
    assert reporter.dialog is not None and reporter.dialogs_opened == 2


def test_the_crash_log_still_gets_the_entry_through_the_hook(qapp, reporter, tmp_path, monkeypatch):
    log = tmp_path / "hooked.log"
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)  # restored afterwards
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    crash_log.install(log, also_call=reporter.callback, enable_faulthandler=False)
    sys.excepthook(*boom("through the hook"))
    qapp.processEvents()
    assert "through the hook" in log.read_text(encoding="utf-8")
    assert reporter.dialog is not None and "through the hook" in reporter.dialog.details_text()


def test_an_exception_in_a_slot_of_a_widget_produces_one_dialog(qapp, reporter, tmp_path, monkeypatch):
    log = tmp_path / "slot.log"
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    crash_log.install(log, also_call=reporter.callback, enable_faulthandler=False)
    widget = QWidget()

    class Button(QWidget):
        pass

    from PyQt6.QtCore import QTimer

    def bad_slot():
        raise ZeroDivisionError("slot blew up")

    QTimer.singleShot(0, bad_slot)
    pump(qapp, lambda: reporter.dialog is not None)
    assert reporter.dialogs_opened == 1 and "slot blew up" in reporter.dialog.details_text()
    assert "slot blew up" in log.read_text(encoding="utf-8")
    assert widget is not None


def test_a_failure_while_building_the_dialog_falls_back_to_the_log_only(qapp, reporter, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("cannot build")

    monkeypatch.setattr(crash_dialog, "CrashDialog", broken)
    reporter.report(*boom("x"))
    qapp.processEvents()  # does not raise
    assert reporter.dialog is None


def test_report_exception_writes_the_log_and_shows_the_box(qapp, reporter, tmp_path):
    try:
        raise OSError("caught by the app")
    except OSError as exc:
        crash_dialog.report_exception(exc, "Save All stopped because of an unexpected error")
    qapp.processEvents()
    assert "caught by the app" in (tmp_path / "crash.log").read_text(encoding="utf-8")
    assert reporter.dialog is not None and reporter.dialog.summary_label.text().startswith("Save All stopped")


def test_copy_and_open_folder_buttons(qapp, reporter, tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(crash_dialog, "open_with_default_app", lambda p: opened.append(p) or True)
    reporter.report(*boom("copy me"))
    qapp.processEvents()
    reporter.dialog.copy_button.click()
    assert "copy me" in QApplication.clipboard().text()
    reporter.dialog.open_button.click()
    assert opened == [str(tmp_path)]

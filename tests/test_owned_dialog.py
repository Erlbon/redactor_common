"""
A dialog an app opens must not stay in memory after it is closed (soak tests, 2026-10-24: every dialog made with the main window as its
parent stayed alive with all its widgets until the program closed), and what the caller reads from the dialog right after exec() returns
must still be there. These tests use the REAL exec(), closed by a timer from inside it: a stand-in for exec() once hid that Qt deletes a
delete-on-close dialog the moment exec() returns.
"""
import importlib
import inspect
import pkgutil

import pytest

pytest.importorskip("PyQt6")

from PyQt6 import sip  # noqa: E402
from PyQt6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication, QDialog, QLabel, QLineEdit, QMainWindow, QPushButton, QVBoxLayout, QWidget  # noqa: E402

import redactor_common.gui as gui_package  # noqa: E402
from redactor_common.gui.owned_dialog import OwnedDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def settle() -> None:
    for _ in range(4):
        QApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def in_the_event_loop(scenario) -> None:
    """Runs `scenario()` from inside a running event loop, the way an app's own handlers are run."""
    outer = QEventLoop()
    failure = []

    def go() -> None:
        try:
            scenario()
        except BaseException as error:  # noqa: BLE001 -- handed to the test below
            failure.append(error)
        finally:
            outer.quit()
    QTimer.singleShot(0, go)
    outer.exec()
    if failure:
        raise failure[0]


def test_every_dialog_class_of_the_package_has_the_shared_base(qapp):
    """A new dialog made straight from QDialog would stay in memory for good once it had been opened: this fails until it uses OwnedDialog."""
    plain = []
    for info in pkgutil.iter_modules(gui_package.__path__):
        try:
            module = importlib.import_module(f"redactor_common.gui.{info.name}")
        except ImportError:                                  # an optional dependency (multimedia, mutagen...) is not installed here
            continue
        for name, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ == module.__name__ and issubclass(cls, QDialog) and cls is not OwnedDialog and not issubclass(cls, OwnedDialog):
                plain.append(f"{module.__name__}.{name}")
    assert plain == []


def test_what_the_caller_reads_after_a_real_exec_is_still_there_and_the_dialog_goes_afterwards(qapp):
    class Ask(OwnedDialog):
        def __init__(self, parent):
            super().__init__(parent)
            self.edit = QLineEdit("typed", self)

    window = QMainWindow()
    dlg = Ask(window)

    def scenario() -> None:
        QTimer.singleShot(0, dlg.accept)
        assert dlg.exec() == QDialog.DialogCode.Accepted
        assert dlg.edit.text() == "typed" and not sip.isdeleted(dlg)     # the widget the caller reads must still exist
        inner = QEventLoop()                                             # a nested loop (a follow-up dialog) must not delete it early
        QTimer.singleShot(0, inner.quit)
        inner.exec()
        QApplication.processEvents()
        assert dlg.edit.text() == "typed" and not sip.isdeleted(dlg)
    in_the_event_loop(scenario)
    settle()
    assert sip.isdeleted(dlg)                                            # and once control is back in the caller's own loop, it is gone
    window.deleteLater()


def test_a_dialog_shown_without_exec_is_deleted_when_it_is_closed(qapp):
    class Probe(OwnedDialog):
        pass
    window = QMainWindow()
    dlg = Probe(window)
    dlg.show()
    dlg.accept()
    assert not sip.isdeleted(dlg)                            # not at once
    settle()
    assert sip.isdeleted(dlg)
    window.deleteLater()


def test_a_caller_that_also_calls_delete_later_is_harmless(qapp):
    window = QMainWindow()
    dlg = OwnedDialog(window)

    def scenario() -> None:
        QTimer.singleShot(0, dlg.reject)
        dlg.exec()
        dlg.deleteLater()                                    # the pattern several callers already had
    in_the_event_loop(scenario)
    settle()
    assert sip.isdeleted(dlg)
    window.deleteLater()


def test_opening_and_closing_a_big_dialog_again_and_again_through_a_real_exec_leaves_nothing_behind(qapp):
    class Big(OwnedDialog):
        def __init__(self, parent):
            super().__init__(parent)
            box = QVBoxLayout(self)
            for i in range(40):
                box.addWidget(QLabel(f"row {i}"))
                button = QPushButton("x")
                button.clicked.connect(lambda _c=False, n=i: self.setWindowTitle(str(n)))   # a lambda that captures the dialog, as the real ones do
                box.addWidget(button)

    window = QMainWindow()
    window.show()

    def open_and_close(accept: bool) -> None:
        d = Big(window)
        QTimer.singleShot(0, d.accept if accept else d.reject)
        d.exec()

    for _ in range(3):
        open_and_close(False)
    settle()
    start = len(window.findChildren(QWidget))
    for n in range(25):
        open_and_close(n % 2 == 0)
    settle()
    assert len(window.findChildren(QWidget)) == start
    window.deleteLater()

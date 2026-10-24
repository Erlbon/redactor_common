"""
A dialog an app opens must not stay in memory after it is closed (soak tests, 2026-10-24: every dialog made with the main window as its
parent stayed alive with all its widgets until the program closed). Every dialog class of the package has the shared base, and the
base deletes a dialog once it has been closed.
"""
import importlib
import inspect
import pkgutil

import pytest

pytest.importorskip("PyQt6")

from PyQt6 import sip  # noqa: E402
from PyQt6.QtCore import QCoreApplication, QEvent, Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QDialog, QLabel, QMainWindow, QPushButton, QVBoxLayout, QWidget  # noqa: E402

import redactor_common.gui as gui_package  # noqa: E402
from redactor_common.gui.owned_dialog import OwnedDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def settle() -> None:
    for _ in range(4):
        QApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


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


def test_a_closed_dialog_is_deleted_once_control_is_back_in_the_event_loop(qapp):
    class Probe(OwnedDialog):
        pass
    window = QMainWindow()
    dlg = Probe(window)
    assert dlg.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dlg.show()
    dlg.accept()
    assert not sip.isdeleted(dlg)                            # not at once: what the caller reads right after exec() is still there
    settle()
    assert sip.isdeleted(dlg)
    window.deleteLater()


def test_opening_and_closing_a_big_dialog_again_and_again_leaves_nothing_behind(qapp):
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
    for _ in range(3):
        d = Big(window)
        d.show()
        d.reject()
    settle()
    start = len(window.findChildren(QWidget))
    for _ in range(25):
        d = Big(window)
        d.show()
        d.reject()
        del d
    settle()
    assert len(window.findChildren(QWidget)) == start
    window.deleteLater()

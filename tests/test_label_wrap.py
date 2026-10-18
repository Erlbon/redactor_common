"""gui/label_wrap: labels wider than 80% of the screen word-wrap; the guard and the lint (2026-10-18)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path  # noqa: E402

from PyQt6.QtCore import QEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget  # noqa: E402

from redactor_common.gui import label_wrap  # noqa: E402
from redactor_common.gui.label_wrap import enforce_label_wrap, install_label_wrap_guard, lint_label_wrap  # noqa: E402

_app = QApplication.instance() or QApplication([])

LONG = "word " * 400


def test_long_label_gets_wrapped():
    label = QLabel(LONG)
    assert not label.wordWrap()
    assert enforce_label_wrap(label) is True
    assert label.wordWrap()


def test_short_label_untouched():
    label = QLabel("Short")
    assert enforce_label_wrap(label) is False
    assert not label.wordWrap()


def test_already_wrapped_untouched():
    label = QLabel(LONG)
    label.setWordWrap(True)
    assert enforce_label_wrap(label) is False
    assert label.wordWrap()


def test_guard_installed_once_and_wraps_on_show():
    first = install_label_wrap_guard(_app)
    second = install_label_wrap_guard(_app)
    assert second is False
    assert first in (True, False)  # an earlier test may have installed it
    box = QWidget()
    layout = QVBoxLayout(box)
    label = QLabel(LONG)
    layout.addWidget(label)
    box.show()
    _app.processEvents()
    assert label.wordWrap()
    box.close()


def test_guard_survives_a_deleted_widget():
    install_label_wrap_guard(_app)
    label = QLabel(LONG)
    label.deleteLater()
    guard = label_wrap._LabelWrapGuard(_app)
    _app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    assert guard.eventFilter(label, QEvent(QEvent.Type.Show)) is False  # must not raise


def test_lint_flags_bad_sample_and_passes_clean_one(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text('layout.addWidget(QLabel("' + "x" * 150 + '"))\n', encoding="utf-8")
    problems = lint_label_wrap(tmp_path)
    assert len(problems) == 1 and "bad.py:1" in problems[0]

    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "ok.py").write_text(
        'layout.addWidget(QLabel("short"))\n'
        'a = QLabel("' + "x" * 150 + '")\na.setWordWrap(True)\n'
        'layout.addWidget(QLabel("' + "y" * 150 + '"))  # wrap-ok: test\n',
        encoding="utf-8",
    )
    assert lint_label_wrap(clean) == []


def test_redactor_commons_own_code_passes_the_label_lint():
    assert lint_label_wrap(Path(__file__).resolve().parent.parent) == []

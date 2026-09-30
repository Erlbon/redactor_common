"""Offscreen smoke test for gui/secret_field.py."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from redactor_common.core import secret_store as ss  # noqa: E402
from redactor_common.gui.secret_field import SecretField  # noqa: E402
from test_secret_store import SECRET, FakeBackend  # noqa: E402

_app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _backend(tmp_path):
    ss.set_backend(FakeBackend())
    ss.set_fallback_dir(tmp_path)
    yield
    ss.set_backend(None)
    ss.set_fallback_dir(None)


def test_field_keep_replace_remove_and_no_leak():
    ss.set_secret("app", "k", SECRET)
    field = SecretField("app", "k")
    assert field.edit.text() == ""
    assert SECRET not in field.edit.placeholderText() + field.source_label.text() + repr(field)
    assert "Stored in" in field.source_label.text()
    assert field.apply() is False  # empty = keep
    assert ss.get_secret("app", "k") == SECRET

    field.edit.setText("new-one")
    assert field.apply() is True
    assert ss.get_secret("app", "k") == "new-one" and field.edit.text() == ""

    field.remove_button.click()
    assert field.removal_pending() and ss.get_secret("app", "k") == "new-one"  # deferred
    assert field.apply() is True
    assert ss.get_secret("app", "k") == ""
    assert not field.remove_button.isEnabled()

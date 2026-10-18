import pytest
from PyQt6.QtWidgets import QApplication

from redactor_common.core import conversion_policy as cp
from redactor_common.gui.conversion_policy_widget import ConversionPolicyWidget


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class Store:
    def __init__(self):
        self.data = {}

    def get(self, section, key, default):
        return self.data.get((section, key), default)

    def put(self, section, key, value):
        self.data[(section, key)] = value


def test_defaults_are_returned_until_the_policy_has_been_saved_once():
    store = Store()
    legacy = cp.ConversionPolicy(on_open=cp.ON_OPEN_NEVER, originals=cp.ORIGINALS_RECYCLE, recent_minutes=3)
    assert cp.load_policy(store.get, legacy) == legacy  # what the app did before this page: nothing changes silently


def test_a_saved_policy_round_trips():
    store = Store()
    policy = cp.ConversionPolicy(on_open=cp.ON_OPEN_AUTO, originals=cp.ORIGINALS_RECYCLE, quarantine_folder="",
                                 undo_log_folder="D:/logs", recent_minutes=0, dry_run_first=False)
    cp.save_policy(policy, store.put)
    assert cp.load_policy(store.get, cp.ConversionPolicy()) == policy


def test_unreadable_values_fall_back_to_the_defaults():
    store = Store()
    cp.save_policy(cp.ConversionPolicy(), store.put)
    store.data[(cp.SECTION, "on_open")] = "sometimes"
    store.data[(cp.SECTION, "recent_minutes")] = "lots"
    loaded = cp.load_policy(store.get, cp.ConversionPolicy(on_open=cp.ON_OPEN_NEVER, recent_minutes=7))
    assert loaded.on_open == cp.ON_OPEN_NEVER and loaded.recent_minutes == 7


def test_the_widget_shows_a_policy_and_returns_the_edited_one(qapp):
    policy = cp.ConversionPolicy(on_open=cp.ON_OPEN_AUTO, originals=cp.ORIGINALS_QUARANTINE, quarantine_folder="Q",
                                 recent_minutes=5, dry_run_first=False)
    widget = ConversionPolicyWidget(policy, never_text="x")
    assert widget.policy() == policy
    widget._recycle.setChecked(True)
    widget._on_open[cp.ON_OPEN_NEVER].setChecked(True)
    widget._dry_run.setChecked(True)
    edited = widget.policy()
    assert (edited.on_open, edited.originals, edited.dry_run_first) == (cp.ON_OPEN_NEVER, cp.ORIGINALS_RECYCLE, True)
    assert not widget._quarantine_edit.isEnabled()  # the folders only matter for the quarantine

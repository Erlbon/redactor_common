"""gui/update_controller.py: Help > Check for Updates, the daily check, the Update Available dialog and the restart,
driven against a fake GitHub (no network)."""

import hashlib
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMainWindow, QMessageBox  # noqa: E402

from redactor_common.core import labels, selfupdate  # noqa: E402
from redactor_common.gui import update_controller as uc  # noqa: E402
from redactor_common.gui.standard_menus import (  # noqa: E402
    StandardMenuSpec, build_standard_menu_bar, get_action_registry, standard_help_items,
)
from redactor_common.gui.menu_lint import lint_menu_bar  # noqa: E402

_app = QApplication.instance() or QApplication([])

PAYLOAD = b"the new exe" * 1000


def answer(tag):
    return json.dumps({
        "tag_name": tag, "html_url": f"https://github.com/Erlbon/cbzredactor/releases/tag/{tag}", "body": "* new things",
        "assets": [{"name": "cbzredactor.exe", "size": len(PAYLOAD),
                    "digest": "sha256:" + hashlib.sha256(PAYLOAD).hexdigest(),
                    "browser_download_url": f"https://github.com/Erlbon/cbzredactor/releases/download/{tag}/cbzredactor.exe"}],
    }).encode()


@pytest.fixture
def boxes(monkeypatch):
    seen = []

    def box(kind, answer=QMessageBox.StandardButton.Ok):
        def show(*args, **_kw):
            seen.append((kind, args[2]))
            return answer
        return staticmethod(show)

    monkeypatch.setattr(QMessageBox, "information", box("info"))
    monkeypatch.setattr(QMessageBox, "warning", box("warning"))
    monkeypatch.setattr(QMessageBox, "question", box("question", QMessageBox.StandardButton.No))
    return seen


@pytest.fixture
def dialogs(monkeypatch):
    """Every UpdateDialog answers with `dialogs.answer` instead of waiting for a click."""
    class Script:
        answer = uc.LATER
        shown = []

    script = Script()
    script.shown = []

    def fake_exec(self):
        script.shown.append(self)
        self.choice = script.answer
        return 1

    monkeypatch.setattr(uc.UpdateDialog, "exec", fake_exec)
    return script


def controller(tmp_path, tag="v2026-11-02-03", version="2026-10-24#01", frozen=True, platform="win32"):
    window = QMainWindow()
    exe = tmp_path / "cbzredactor.exe"
    exe.write_bytes(b"old exe")
    fetch = lambda _request: answer(tag)  # noqa: E731
    return uc.UpdateController(window, "The CBZ Redactor", version, "https://github.com/Erlbon/cbzredactor",
                               tmp_path / "cbzredactor_updates.json", fetch=fetch, platform=platform,
                               frozen=frozen, exe=exe)


def test_help_menu_has_check_for_updates_and_lints_clean():
    window = QMainWindow()
    called = []
    spec = StandardMenuSpec(help=standard_help_items("App", None, None, None, updates_slot=lambda: called.append(1)))
    build_standard_menu_bar(window, spec)
    action = get_action_registry(window)["check_for_updates"]
    assert action.text() == labels.CHECK_FOR_UPDATES and action.isEnabled()
    action.trigger()
    assert called == [1]
    assert lint_menu_bar(window) == []


def test_without_a_slot_the_item_is_disabled_not_hidden():
    window = QMainWindow()
    build_standard_menu_bar(window, StandardMenuSpec(help=standard_help_items("App", None, None, None)))
    assert not get_action_registry(window)["check_for_updates"].isEnabled()


def test_check_now_says_so_when_up_to_date(tmp_path, boxes, dialogs):
    c = controller(tmp_path, tag="v2026-10-24-01", version="2026-10-24#01")
    c.check_now()
    assert boxes == [("info", "You have the newest version of The CBZ Redactor (2026-10-24#01).")]
    assert dialogs.shown == [] and c.settings.last_check > 0


def test_check_now_reports_a_failure(tmp_path, boxes, dialogs):
    c = controller(tmp_path)

    def offline(_r):
        raise OSError("no network")

    c._fetch = offline
    c.check_now()
    assert boxes[0][0] == "warning" and "Could not reach GitHub" in boxes[0][1]


def test_check_now_offers_a_newer_version_and_skip_is_remembered(tmp_path, boxes, dialogs):
    c = controller(tmp_path)
    dialogs.answer = uc.SKIP
    c.check_now()
    (dialog,) = dialogs.shown
    assert c.settings.skipped == "v2026-11-02-03"


def test_update_now_downloads_verifies_swaps_and_asks_to_restart(tmp_path, boxes, dialogs, monkeypatch):
    c = controller(tmp_path)
    monkeypatch.setattr(selfupdate.urllib.request, "build_opener", lambda *_a: _Opener(PAYLOAD))
    dialogs.answer = uc.UPDATE
    c.check_now()
    exe = tmp_path / "cbzredactor.exe"
    assert exe.read_bytes() == PAYLOAD
    assert (tmp_path / "cbzredactor.exe.old").read_bytes() == b"old exe"
    assert boxes[-1][0] == "question" and "2026-11-02#03 is installed" in boxes[-1][1]
    assert not c.restart_pending                 # answered No: the new version starts next time


def test_a_bad_download_keeps_the_running_exe(tmp_path, boxes, dialogs, monkeypatch):
    c = controller(tmp_path)
    monkeypatch.setattr(selfupdate.urllib.request, "build_opener", lambda *_a: _Opener(b"tampered" * 10))
    dialogs.answer = uc.UPDATE
    c.check_now()
    assert (tmp_path / "cbzredactor.exe").read_bytes() == b"old exe"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cbzredactor.exe", "cbzredactor_updates.json"]
    assert boxes[-1][0] == "warning"


def test_from_source_the_dialog_offers_the_download_page_instead(tmp_path, boxes, dialogs, monkeypatch):
    opened = []
    monkeypatch.setattr(uc.QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url.toString())))
    c = controller(tmp_path, frozen=False)
    dialogs.answer = uc.OPEN_PAGE
    c.check_now()
    (dialog,) = dialogs.shown
    assert "Download Page" in dialog.main_button.text()
    assert opened == ["https://github.com/Erlbon/cbzredactor/releases/tag/v2026-11-02-03"]
    assert (tmp_path / "cbzredactor.exe").read_bytes() == b"old exe"


def test_the_real_dialog_offers_update_now_when_it_can(tmp_path):
    r = selfupdate.release_from_json(json.loads(answer("v2026-11-02-03")), "cbzredactor", "win32")
    dialog = uc.UpdateDialog("App", "2026-10-24#01", r, True)
    assert dialog.main_button.text() == "&Update Now"
    dialog.main_button.click()
    assert dialog.choice == uc.UPDATE


def test_auto_check_does_nothing_unless_switched_on(tmp_path):
    c = controller(tmp_path)
    assert c.start_auto_check(0) is False
    c.settings.update(check_for_updates=True)
    assert c.start_auto_check(0) is True
    c.settings.update(last_check=__import__("time").time())
    assert c.start_auto_check(0) is False


def test_auto_check_offers_once_and_respects_skip(tmp_path, dialogs):
    c = controller(tmp_path)
    found = []
    c._auto.found.disconnect()
    c._auto.found.connect(found.append)
    c._auto_work()
    _app.processEvents()
    assert [r.tag for r in found] == ["v2026-11-02-03"]
    c.settings.update(skipped="v2026-11-02-03")
    c._auto_work()
    _app.processEvents()
    assert len(found) == 1


def test_auto_check_is_silent_on_failure(tmp_path, boxes):
    c = controller(tmp_path)

    def offline(_r):
        raise OSError("down")

    c._fetch = offline
    c._auto_work()
    _app.processEvents()
    assert boxes == [] and c.settings.last_check == 0.0


def test_restart_starts_the_new_exe_only_after_the_window_really_closed(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(uc.subprocess, "Popen", lambda argv, **kw: started.append((argv, kw["env"])))
    monkeypatch.setattr(QApplication, "quit", staticmethod(lambda: None))
    c = controller(tmp_path)

    c.window.closeEvent = lambda event: event.ignore()      # "you have unsaved changes" -> Cancel
    c.restart()
    assert not c.restart_pending
    c._start_new_exe()
    assert started == []

    c.window.closeEvent = lambda event: event.accept()
    c.restart()
    assert c.restart_pending
    c._start_new_exe()
    assert started[0][0] == [str(tmp_path / "cbzredactor.exe")]
    assert started[0][1]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


class _Opener:
    def __init__(self, data):
        self.data = data

    def open(self, request, timeout=None):
        import io
        assert request.full_url.startswith("https://github.com/Erlbon/cbzredactor/releases/download/")
        return io.BytesIO(self.data)

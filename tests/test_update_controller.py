"""gui/update_controller.py: Help > Check for Updates, the daily check, the Update Available dialog and the restart,
driven against a fake GitHub (no network) whose releases carry an update manifest (docs/update-framework.md)."""

import hashlib
import io
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMainWindow, QMessageBox  # noqa: E402

from redactor_common.core import labels, selfupdate, update_manifest  # noqa: E402
from redactor_common.core.update_manifest import WINDOWS_EXE, WINDOWS_INSTALLER, WINDOWS_ZIP  # noqa: E402
from redactor_common.gui import update_controller as uc  # noqa: E402
from redactor_common.gui.standard_menus import (  # noqa: E402
    StandardMenuSpec, build_standard_menu_bar, get_action_registry, standard_help_items,
)
from redactor_common.gui.menu_lint import lint_menu_bar  # noqa: E402

_app = QApplication.instance() or QApplication([])

PAYLOAD = b"the new exe" * 1000
SETUP = b"the new installer" * 1000
NAMES = {WINDOWS_EXE: "cbzredactor.exe", WINDOWS_INSTALLER: "CBZ-Redactor-2026-11-02-03-setup.exe"}
DATA = {WINDOWS_EXE: PAYLOAD, WINDOWS_INSTALLER: SETUP}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def github(tag, manifest=True):
    """A fake fetch: the release API answer for `tag` (exe, installer and, unless `manifest` is False, the manifest)
    and the manifest's own bytes."""
    base = f"https://github.com/Erlbon/cbzredactor/releases/download/{tag}/"
    files = {base + NAMES[c]: DATA[c] for c in NAMES}
    body = json.dumps({"schema": 1, "app": "cbzredactor", "tag": tag, "version": selfupdate.display_version(tag),
                       "min_client": 1, "packages": {c: {"file": NAMES[c], "size": len(DATA[c]),
                                                         "sha256": sha(DATA[c])} for c in NAMES}}).encode()
    if manifest:
        files[base + update_manifest.MANIFEST_NAME] = body
    answer = json.dumps({
        "tag_name": tag, "html_url": f"https://github.com/Erlbon/cbzredactor/releases/tag/{tag}", "body": "* new things",
        "assets": [{"name": url.rsplit("/", 1)[1], "size": len(data), "digest": "sha256:" + sha(data),
                    "browser_download_url": url} for url, data in files.items()],
    }).encode()

    def fetch(request):
        url = getattr(request, "full_url", request)
        return answer if url.startswith("https://api.github.com/") else files[url]
    return fetch


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


def controller(tmp_path, tag="v2026-11-02-03", version="2026-10-24#01", frozen=True, channel=WINDOWS_EXE,
               manifest=True, **kw):
    window = QMainWindow()
    exe = tmp_path / "cbzredactor.exe"
    exe.write_bytes(b"old exe")
    return uc.UpdateController(window, "The CBZ Redactor", version, "https://github.com/Erlbon/cbzredactor",
                               tmp_path / "cbzredactor_updates.json", fetch=github(tag, manifest), frozen=frozen,
                               exe=exe, channel=channel, **kw)


def release(tag="v2026-11-02-03"):
    return selfupdate.Release(tag, selfupdate.parse_version(tag), "* new things",
                              f"https://github.com/Erlbon/cbzredactor/releases/tag/{tag}")


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


def test_the_channel_comes_from_the_file_beside_the_exe(tmp_path):
    update_manifest.write_channel(tmp_path, WINDOWS_INSTALLER)
    c = controller(tmp_path, channel=None)
    assert c.channel == WINDOWS_INSTALLER
    assert controller(tmp_path, channel=None, frozen=False).channel == update_manifest.SOURCE


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
    assert dialog.main_button.text() == "&Update Now" and dialog.reason_label is None
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


@pytest.mark.parametrize("kw, says", [
    ({"frozen": False, "channel": None}, "git pull"),
    ({"channel": WINDOWS_ZIP}, "zip"),
    ({"channel": ""}, "cannot tell how it was installed"),
    ({"manifest": False}, "update-manifest.json"),
])
def test_a_copy_that_cannot_update_says_why_next_to_the_download_page(tmp_path, boxes, dialogs, monkeypatch, kw, says):
    opened = []
    monkeypatch.setattr(uc.QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url.toString())))
    c = controller(tmp_path, **kw)
    dialogs.answer = uc.OPEN_PAGE
    c.check_now()
    (dialog,) = dialogs.shown
    assert "Download Page" in dialog.main_button.text()
    assert says in dialog.reason_label.text()
    dialog.copy_button.click()
    copied = QApplication.clipboard().text()
    assert "The CBZ Redactor 2026-10-24#01" in copied and "Release: v2026-11-02-03" in copied and says in copied
    assert opened == ["https://github.com/Erlbon/cbzredactor/releases/tag/v2026-11-02-03"]
    assert (tmp_path / "cbzredactor.exe").read_bytes() == b"old exe"


def test_the_real_dialog_offers_update_now_when_it_can(tmp_path):
    dialog = uc.UpdateDialog("App", "2026-10-24#01", release(), True)
    assert dialog.main_button.text() == "&Update Now" and dialog.copy_button is None
    dialog.main_button.click()
    assert dialog.choice == uc.UPDATE


def test_the_automatic_offer_names_where_the_app_keeps_the_switch(tmp_path, dialogs):
    def hint_of(dialog):
        return [w.text() for w in dialog.findChildren(uc.QLabel) if "daily check" in w.text()]
    assert hint_of(uc.UpdateDialog("App", "2026-10-24#01", release(), True, automatic=True)) == [
        "The daily check can be switched off in Preferences."]
    c = controller(tmp_path)
    c.switch_place = "the Help menu"
    c.offer(c.decide(), automatic=True)
    (dialog,) = dialogs.shown
    assert hint_of(dialog) == ["The daily check can be switched off in the Help menu."]


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
    assert [d.release.tag for d in found] == ["v2026-11-02-03"]
    c.settings.update(skipped="v2026-11-02-03")
    c._auto_work()
    _app.processEvents()
    assert len(found) == 1


def test_auto_check_also_offers_what_this_copy_cannot_install_with_the_reason(tmp_path):
    c = controller(tmp_path, channel=WINDOWS_ZIP)
    found = []
    c._auto.found.disconnect()
    c._auto.found.connect(found.append)
    c._auto_work()
    _app.processEvents()
    assert [d.action for d in found] == [selfupdate.PAGE]


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
    monkeypatch.setattr(QApplication, "quit", staticmethod(lambda: None))
    c = controller(tmp_path, launch=lambda argv, **kw: started.append((argv, kw["env"])))

    c.window.closeEvent = lambda event: event.ignore()      # "you have unsaved changes" -> Cancel
    assert c.restart() is False
    assert not c.restart_pending
    c._start_new_exe()
    assert started == []

    c.window.closeEvent = lambda event: event.accept()
    assert c.restart() is True
    assert c.restart_pending
    c._start_new_exe()
    assert started[0][0] == [str(tmp_path / "cbzredactor.exe")]
    assert started[0][1]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


class _Opener:
    def __init__(self, data):
        self.data = data

    def open(self, request, timeout=None):
        assert request.full_url.startswith("https://github.com/Erlbon/cbzredactor/releases/download/")
        return io.BytesIO(self.data)


# an installed copy (channel windows-installer) updates through the installer its release's manifest names ---------------


def test_an_installed_copy_downloads_the_named_installer_and_runs_it_at_quit(tmp_path, boxes, dialogs, monkeypatch):
    monkeypatch.setattr(selfupdate.tempfile, "gettempdir", lambda: str(tmp_path))
    started = []
    quits = []

    def quit_app():                     # the app's own File > Exit (Accipio: the X only hides it)
        quits.append(1)
        c._start_new_exe()              # aboutToQuit
        return True

    c = controller(tmp_path, channel=WINDOWS_INSTALLER, quit_app=quit_app,
                   launch=lambda argv, **kw: started.append(argv))
    monkeypatch.setattr(selfupdate.urllib.request, "build_opener", lambda *_a: _Opener(SETUP))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    dialogs.answer = uc.UPDATE
    c.check_now()
    setup = tmp_path / "redactor-updates" / NAMES[WINDOWS_INSTALLER]
    assert setup.read_bytes() == SETUP
    assert (tmp_path / "cbzredactor.exe").read_bytes() == b"old exe"       # the exe is the installer's to replace
    assert quits == [1]
    assert started == [[str(setup), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/relaunch=yes"]]


def test_an_installed_copy_that_stays_open_starts_no_installer(tmp_path, boxes, dialogs, monkeypatch):
    monkeypatch.setattr(selfupdate.tempfile, "gettempdir", lambda: str(tmp_path))
    started = []
    c = controller(tmp_path, channel=WINDOWS_INSTALLER, quit_app=lambda: False,
                   launch=lambda argv, **kw: started.append(argv))
    monkeypatch.setattr(selfupdate.urllib.request, "build_opener", lambda *_a: _Opener(SETUP))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    assert c.update_to(c.decide()) is False
    c._start_new_exe()
    assert started == []

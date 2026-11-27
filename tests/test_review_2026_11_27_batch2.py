"""Review of 2026-11-27, batch 2 (release and update): cards 16-27."""

import datetime
import hashlib
import http.client
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import QUrl  # noqa: E402
from PyQt6.QtGui import QTextDocument  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from redactor_common import release  # noqa: E402
from redactor_common.core import selfupdate, version_bump  # noqa: E402
from redactor_common.core.selfupdate import Release, UpdateError, UpdateSettings  # noqa: E402

from test_release import PYPROJECT, FakeRunner, repo  # noqa: E402

_app = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parent.parent


# --- 16: an existing tag must point at HEAD ---------------------------------------------------------------------------

class TaggedRunner(FakeRunner):
    """HEAD is abc123; the tag exists here (`local`) and/or on origin (`remote`, peeled to `remote`)."""

    def __init__(self, local="", remote=""):
        super().__init__()
        self.local, self.remote = local, remote

    def __call__(self, argv, cwd=None, check=True, env=None, capture=True):
        if argv[:3] == ["git", "tag", "-l"]:
            self.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, argv[3] if self.local else "", "")
        if argv[:2] == ["git", "rev-parse"] and argv[2].startswith("refs/tags/"):
            self.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, self.local, "")
        if argv[:2] == ["git", "ls-remote"]:
            self.calls.append(list(argv))
            tag = argv[4][len("refs/tags/"):]
            out = (f"tagobject\trefs/tags/{tag}\n{self.remote}\trefs/tags/{tag}^{{}}\n") if self.remote else ""
            return subprocess.CompletedProcess(argv, 0, out, "")
        return super().__call__(argv, cwd, check, env, capture)


def test_16_a_tag_on_another_commit_stops_the_release(tmp_path):
    cfg = repo(tmp_path)
    release.check_tag_is_head(cfg, cfg.tag, TaggedRunner())                      # no tag yet
    release.check_tag_is_head(cfg, cfg.tag, TaggedRunner(local="abc123", remote="abc123"))
    with pytest.raises(release.ReleaseError, match="not at HEAD"):
        release.check_tag_is_head(cfg, cfg.tag, TaggedRunner(local="0ld0ld"))
    with pytest.raises(release.ReleaseError, match="on origin points at"):
        release.check_tag_is_head(cfg, cfg.tag, TaggedRunner(remote="0ld0ld"))   # the peeled commit counts


def test_16_publish_checks_the_tag_before_it_builds(tmp_path, monkeypatch):
    cfg = repo(tmp_path)
    monkeypatch.setattr(release, "preflight", lambda *a: None)
    runner = TaggedRunner(local="0ld0ld")
    with pytest.raises(release.ReleaseError, match="not at HEAD"):
        release.publish(cfg, runner=runner)
    assert not any(call[:1] == ["cmd"] for call in runner.calls)               # nothing was built


# --- 17: on POSIX the gate stops the program's whole process group ---------------------------------------------------

def test_17_posix_gate_programs_get_a_session_and_are_stopped_as_a_group(tmp_path, monkeypatch):
    cfg = repo(tmp_path, PYPROJECT + '\n[tool.redactor-release.gate]\nrun = ["--run"]\nquit = ["--quit"]\n')
    monkeypatch.setattr(release, "RUN_SETTLE", 0)
    monkeypatch.setattr(release.sys, "platform", "linux")
    seen, killed = {}, []

    class Proc:
        pid = 4242
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            killed.append("kill")

    monkeypatch.setattr(release.subprocess, "Popen", lambda argv, **kw: seen.update(kw) or Proc())
    monkeypatch.setattr(release.os, "killpg", lambda pid, sig: killed.append(("killpg", pid, sig)), raising=False)
    monkeypatch.setattr(release.signal, "SIGKILL", 9, raising=False)
    gate = release.Gate(cfg, cfg.tag, tmp_path / "scratch")
    gate.start_running(tmp_path / "app")
    assert seen.get("start_new_session") is True
    gate.stop_started()
    assert killed == [("killpg", 4242, 9)]


# --- 18: a cut-off download is "could not download", not a crash ------------------------------------------------------

PAYLOAD = b"new exe " * 5000


def _release(data=PAYLOAD):
    tag = "v2026-11-02-03"
    return Release(tag, selfupdate.parse_version(tag), "", "https://github.com/Erlbon/cbzredactor/releases/tag/" + tag,
                   "cbzredactor.exe", f"https://github.com/Erlbon/cbzredactor/releases/download/{tag}/cbzredactor.exe",
                   len(data), hashlib.sha256(data).hexdigest())


class _CutOff:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, _n):
        raise http.client.IncompleteRead(b"partial", 100)


class _CutOffOpener:
    def open(self, request, timeout=None):
        return _CutOff()


def test_18_an_incomplete_read_is_an_update_error(tmp_path):
    with pytest.raises(UpdateError, match="Could not download"):
        selfupdate.download(_release(), tmp_path / "new.exe", opener=_CutOffOpener())
    assert list(tmp_path.iterdir()) == []


# --- 19: a folder of its own, checked again before it runs, old ones cleared -----------------------------------------

def test_19_each_installer_download_gets_a_fresh_folder(tmp_path):
    first = selfupdate.installer_download_path(_release(), tmp_path)
    second = selfupdate.installer_download_path(_release(), tmp_path)
    assert first.parent != second.parent and first.name == second.name == "cbzredactor.exe"
    assert first.parent.parent == tmp_path / "redactor-updates"


def test_19_verify_download_refuses_a_file_that_changed(tmp_path):
    path = tmp_path / "setup.exe"
    path.write_bytes(PAYLOAD)
    selfupdate.verify_download(path, _release())
    path.write_bytes(PAYLOAD[:-1] + b"X")
    with pytest.raises(UpdateError, match="changed on disk"):
        selfupdate.verify_download(path, _release())
    path.unlink()
    with pytest.raises(UpdateError, match="cannot be read"):
        selfupdate.verify_download(path, _release())


def test_19_old_installer_folders_are_removed_at_start(tmp_path):
    parent = tmp_path / "redactor-updates"
    old, new = parent / "update-old", parent / "update-new"
    for folder in (old, new):
        folder.mkdir(parents=True)
        (folder / "setup.exe").write_bytes(b"x")
    day_ago = time.time() - 2 * 24 * 3600
    os.utime(old, (day_ago, day_ago))
    selfupdate.remove_old_installers(tmp_path)
    assert not old.exists() and (new / "setup.exe").exists()
    selfupdate.remove_old_installers(tmp_path / "nothing-here")             # no folder: quiet


# --- 20: --skip-build refuses a stale build ---------------------------------------------------------------------------

def test_20_skip_build_refuses_packages_older_than_the_version(tmp_path):
    cfg = repo(tmp_path)
    folder = cfg.root / "dist" / "App"
    folder.mkdir(parents=True)
    exe, setup = folder / "App.exe", cfg.root / "dist" / "App-2026-11-07-02-setup.exe"
    exe.write_bytes(b"MZ")
    setup.write_bytes(b"setup")
    before = cfg.version_file.stat().st_mtime - 60
    os.utime(setup, (before, before))
    with pytest.raises(release.ReleaseError, match="built before the version was bumped"):
        release.check_built_after_version(cfg)
    later = cfg.version_file.stat().st_mtime + 60
    os.utime(setup, (later, later))
    os.utime(exe, (before, before))                                         # the folder build's exe is checked too
    with pytest.raises(release.ReleaseError, match="App.exe is older"):
        release.check_built_after_version(cfg)
    os.utime(exe, (later, later))
    release.check_built_after_version(cfg)                                  # the zip is made later: not checked
    exe.unlink()
    with pytest.raises(release.ReleaseError, match="Missing release file"):
        release.check_built_after_version(cfg)


# --- 21: the notes gather every section since the last release ---------------------------------------------------------

def test_21_notes_cover_every_section_since_the_previous_release(tmp_path):
    cfg = repo(tmp_path)
    notes = release.release_notes(cfg, "")
    assert "## 2026-11-07#02" in notes and "## 2026-11-07#01" in notes and "- old" in notes
    assert release.release_notes(cfg, "v2026-11-07-01") == "- one\n- two"


def test_21_no_section_for_the_version_stops_the_release(tmp_path):
    cfg = repo(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## 2026-11-07#01 -- Old\n\n- old\n", encoding="utf-8")
    with pytest.raises(release.ReleaseError, match="no section for 2026-11-07#02"):
        release.release_notes(cfg, "v2026-11-07-01")
    (tmp_path / "CHANGELOG.md").unlink()
    with pytest.raises(release.ReleaseError, match="cannot be read"):
        release.release_notes(cfg)


def test_21_publish_takes_the_latest_published_release_as_the_floor(tmp_path):
    cfg = repo(tmp_path)
    assert release.latest_published(cfg, cfg.tag, FakeRunner({"latest": {"tag_name": "v2026-11-07-01"}})) \
        == "v2026-11-07-01"
    assert release.latest_published(cfg, cfg.tag, FakeRunner({"latest": {"tag_name": cfg.tag}})) == ""
    assert release.latest_published(cfg, cfg.tag, FakeRunner()) == ""     # none, or GitHub not reachable


# --- 22: --tag must be a release tag -----------------------------------------------------------------------------------

@pytest.mark.parametrize("tag", ["x\\..\\..", "../x", "", "v2026-11-07-02/..", "2026-11-07#02"])
def test_22_gate_and_promote_refuse_a_tag_that_is_not_a_release_tag(tmp_path, monkeypatch, tag):
    monkeypatch.setenv("REDACTOR_GATE_DIR", str(tmp_path / "gate"))
    cfg = repo(tmp_path)
    runner = FakeRunner()
    with pytest.raises(release.ReleaseError, match="not a release tag"):
        release.gate(cfg, tag, runner=runner)
    with pytest.raises(release.ReleaseError, match="not a release tag"):
        release.promote(cfg, tag, runner=runner)
    with pytest.raises(release.ReleaseError, match="not a release tag"):
        release.gate(cfg, "v2026-11-07-02", previous=tag or "x", runner=runner)
    assert runner.calls == [] and not (tmp_path / "gate").exists()


# --- 23: the relaunch after an update runs as the user --------------------------------------------------------------

def test_23_the_relaunch_runs_as_the_original_user():
    text = (ROOT / "installer" / "update-contract.iss").read_text(encoding="utf-8")
    run_line = next(line for line in text.splitlines() if line.startswith("Filename:"))
    assert "runasoriginaluser" in run_line and "UpdateContractRelaunch" in run_line


# --- 24: notes open only https links and load no outside image ---------------------------------------------------------

def test_24_only_https_links_open(monkeypatch):
    from redactor_common.gui import about_dialog
    opened = []
    monkeypatch.setattr(about_dialog.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    browser = about_dialog._markdown_browser("[a](https://example.com)")
    assert not browser.openExternalLinks() and not browser.openLinks()
    for link in ("file:///C:/Windows/System32/calc.exe", "ms-settings:", "http://example.com", "//server/share/x",
                 "https://example.com/notes"):
        browser.anchorClicked.emit(QUrl(link))
    assert opened == ["https://example.com/notes"]


def test_24_images_from_outside_are_not_loaded(tmp_path):
    from redactor_common.gui import about_dialog
    browser = about_dialog._markdown_browser("text")
    image = QTextDocument.ResourceType.ImageResource.value
    picture = tmp_path / "p.png"
    picture.write_bytes(b"\x89PNG")
    assert browser.loadResource(image, QUrl.fromLocalFile(str(picture))) is None
    assert browser.loadResource(image, QUrl("file://server/share/p.png")) is None


# --- 25: the version never goes backwards -----------------------------------------------------------------------------

def test_25_a_stored_date_after_today_keeps_its_date_and_counts_on():
    today = datetime.date(2026, 11, 27)
    assert version_bump.next_version("2026-12-01", 5, today) == "2026-12-01#06"
    assert version_bump.next_version("2026-11-27", 5, today) == "2026-11-27#06"
    assert version_bump.next_version("2026-11-26", 5, today) == "2026-11-27#01"


# --- 26: settings writes are locked and never crash the caller ------------------------------------------------------

def test_26_a_settings_file_that_cannot_be_written_is_reported_not_raised(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the folder should be", encoding="utf-8")
    settings = UpdateSettings(blocker / "app_updates.json")
    assert settings.update(last_check=1.0) is False
    good = UpdateSettings(tmp_path / "app_updates.json")
    assert good.update(last_check=1.0) is True and good.last_check == 1.0


def test_26_two_threads_writing_keep_both_values(tmp_path):
    import threading
    settings = UpdateSettings(tmp_path / "app_updates.json")
    errors = []

    def write(key):
        for i in range(40):
            try:
                settings.update(**{key: i})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
    threads = [threading.Thread(target=write, args=(k,)) for k in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = settings._load()
    assert errors == [] and data.get("a") == 39 and data.get("b") == 39


# --- 27: the pre-commit hook checks what is staged ---------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("sh") is None or shutil.which("git") is None, reason="needs sh and git")
def test_27_the_hook_checks_the_staged_file_not_the_working_copy(tmp_path):
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, text=True)
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    name = "f\u00e5 il.py"                                  # a space and a non-ASCII letter
    path = tmp_path / name
    path.write_text("def broken(:\n", encoding="utf-8")
    git("add", name)
    path.write_text("ok = 1\n", encoding="utf-8")         # the working copy parses, the staged one does not
    hook = ROOT / ".githooks" / "pre-commit"
    done = subprocess.run(["sh", str(hook)], cwd=tmp_path, capture_output=True, text=True)
    assert done.returncode == 1 and "syntax error" in done.stderr
    git("add", name)
    done = subprocess.run(["sh", str(hook)], cwd=tmp_path, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr

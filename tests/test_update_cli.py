"""core/update_cli.py: --version, --check-update and --update, which every app answers before it opens a window
(the release gate drives an old copy with them), and REDACTOR_CONFIG_DIR (core/app_paths.py)."""

import hashlib
import io
import json

import pytest

from redactor_common.core import app_paths, selfupdate, update_cli, update_manifest
from redactor_common.core.update_manifest import WINDOWS_EXE, WINDOWS_INSTALLER

NEW = b"the new build" * 2000
TAG = "v2026-11-07-02"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def github(channel=WINDOWS_INSTALLER, name="App-2026-11-07-02-setup.exe"):
    base = f"https://github.com/Erlbon/app/releases/download/{TAG}/"
    manifest = json.dumps({"schema": 1, "app": "app", "tag": TAG, "version": "2026-11-07#02", "min_client": 1,
                           "packages": {channel: {"file": name, "size": len(NEW), "sha256": sha(NEW)}}}).encode()
    files = {base + name: NEW, base + update_manifest.MANIFEST_NAME: manifest}
    answer = json.dumps({"tag_name": TAG, "html_url": f"https://github.com/Erlbon/app/releases/tag/{TAG}", "body": "",
                         "assets": [{"name": u.rsplit("/", 1)[1], "size": len(d), "digest": "sha256:" + sha(d),
                                     "browser_download_url": u} for u, d in files.items()]}).encode()
    seen = []

    def fetch(request):
        url = getattr(request, "full_url", request)
        seen.append(url)
        return answer if url.startswith("https://api.github.com/") else files[url]
    fetch.seen = seen
    return fetch


class Opener:
    def open(self, request, timeout=None):
        return io.BytesIO(NEW)


def run(argv, tmp_path, channel=WINDOWS_INSTALLER, fetch=None, **kw):
    exe = tmp_path / "App.exe"
    exe.write_bytes(b"old build")
    update_manifest.write_channel(tmp_path, channel)
    out = io.StringIO()
    code = update_cli.handle(["App.exe"] + argv, "App", "2026-11-06#13", "https://github.com/Erlbon/app",
                             exe=exe, frozen=True, fetch=fetch or github(channel), out=out, opener=Opener(), **kw)
    return code, out.getvalue()


def test_ordinary_starts_are_left_to_the_app(tmp_path):
    assert run([], tmp_path)[0] is None
    assert run(["--minimized"], tmp_path)[0] is None


def test_version(tmp_path):
    assert run(["--version"], tmp_path) == (0, "2026-11-06#13\n")


def test_check_update_prints_the_decision_and_changes_nothing(tmp_path):
    fetch = github()
    code, text = run(["--check-update", "--release-tag", TAG], tmp_path, fetch=fetch)
    data = json.loads(text)
    assert code == 0
    assert data["action"] == "install" and data["file"] == "App-2026-11-07-02-setup.exe"
    assert data["channel"] == WINDOWS_INSTALLER and data["release"] == TAG and data["current"] == "2026-11-06#13"
    assert fetch.seen[0].endswith(f"/releases/tags/{TAG}")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["App.exe", update_manifest.CHANNEL_FILE]


def test_the_result_can_go_to_a_file(tmp_path):
    """A windowed exe has no console to print to: the gate reads --result-file."""
    result = tmp_path / "out" / "result.json"
    code, text = run(["--check-update", "--result-file", str(result)], tmp_path)
    assert code == 0 and json.loads(result.read_text(encoding="utf-8"))["action"] == "install"


def test_a_github_failure_is_exit_2_with_the_reason(tmp_path):
    def offline(_r):
        raise OSError("no network")
    code, text = run(["--check-update"], tmp_path, fetch=offline)
    assert code == 2 and json.loads(text)["action"] == "error" and "GitHub" in json.loads(text)["reason"]


def test_update_starts_the_installer_without_relaunch_and_reports_it(tmp_path, monkeypatch):
    monkeypatch.setattr(selfupdate.tempfile, "gettempdir", lambda: str(tmp_path / "temp"))
    started = []

    class Process:
        pid = 4321
    code, text = run(["--update", "--release-tag", TAG], tmp_path,
                     launch=lambda argv: started.append(argv) or Process())
    data = json.loads(text)
    setup = tmp_path / "temp" / "redactor-updates" / "App-2026-11-07-02-setup.exe"
    assert code == 0 and data["action"] == "started" and data["pid"] == 4321
    assert setup.read_bytes() == NEW
    assert started == [[str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]]


def test_update_swaps_an_exe_copy(tmp_path):
    code, text = run(["--update"], tmp_path, channel=WINDOWS_EXE, fetch=github(WINDOWS_EXE, "app.exe"))
    assert code == 0 and json.loads(text)["action"] == "installed"
    assert (tmp_path / "App.exe").read_bytes() == NEW and (tmp_path / "App.exe.old").read_bytes() == b"old build"


def test_update_that_cannot_install_says_why_with_exit_3(tmp_path):
    code, text = run(["--update"], tmp_path, channel=update_manifest.WINDOWS_ZIP)
    assert code == 3 and json.loads(text)["action"] == "page" and "zip" in json.loads(text)["reason"]
    assert (tmp_path / "App.exe").read_bytes() == b"old build"


def test_a_missing_or_bad_release_tag_value_is_exit_2(tmp_path):
    assert run(["--check-update", "--release-tag"], tmp_path)[0] == 2
    assert run(["--check-update", "--release-tag", "latest"], tmp_path)[0] == 2


# --- REDACTOR_CONFIG_DIR -----------------------------------------------------------------------------------------------


def test_config_override_moves_every_settings_place(tmp_path, monkeypatch):
    monkeypatch.delenv(app_paths.CONFIG_ENV, raising=False)
    assert app_paths.config_override() is None
    monkeypatch.setenv(app_paths.CONFIG_ENV, str(tmp_path / "profile"))
    assert app_paths.config_override() == tmp_path / "profile"
    assert app_paths.base_dir(tmp_path / "project") == tmp_path / "profile"
    assert app_paths.settings_ini_path("app", tmp_path / "project") == tmp_path / "profile" / "app_settings.ini"
    assert (tmp_path / "profile").is_dir()
    monkeypatch.setenv(app_paths.CONFIG_ENV, "relative\\path")
    with pytest.raises(ValueError, match="absolute"):
        app_paths.config_override()

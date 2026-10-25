"""core/selfupdate.py: the version check, the verified download, the rename swap and the settings file."""

import hashlib
import io
import json
import threading
import urllib.error

import pytest

from redactor_common.core import selfupdate
from redactor_common.core.preferences import MemoryBackend
from redactor_common.core.selfupdate import Release, UpdateError, UpdateSettings

PAYLOAD = b"new exe " * 5000
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


def api_answer(tag="v2026-11-02-03", digest="sha256:" + DIGEST, size=len(PAYLOAD), name="cbzredactor.exe"):
    return {
        "tag_name": tag, "html_url": f"https://github.com/Erlbon/cbzredactor/releases/tag/{tag}", "body": "## Notes",
        "assets": [
            {"name": "cbzredactor-linux-x86_64.tar.gz", "size": 9, "digest": "sha256:" + "0" * 64,
             "browser_download_url": "https://github.com/Erlbon/cbzredactor/releases/download/x/linux.tar.gz"},
            {"name": name, "size": size, "digest": digest,
             "browser_download_url": f"https://github.com/Erlbon/cbzredactor/releases/download/{tag}/{name}"},
        ],
    }


def release(**kw):
    return selfupdate.release_from_json(api_answer(**kw), "cbzredactor", "win32")


class FakeOpener:
    def __init__(self, data=PAYLOAD, error=None):
        self.data, self.error, self.urls = data, error, []

    def open(self, request, timeout=None):
        self.urls.append(request.full_url)
        if self.error:
            raise self.error
        return io.BytesIO(self.data)


# --- versions ----------------------------------------------------------------------------------------------------------


def test_parse_version_reads_app_versions_and_tags_alike():
    assert selfupdate.parse_version("2026-10-24#01") == (2026, 10, 24, 1)
    assert selfupdate.parse_version("v2026-10-24-01") == selfupdate.parse_version("2026-10-24#01")
    assert selfupdate.parse_version("v2026-10-24-12") > selfupdate.parse_version("2026-10-24#09")
    assert selfupdate.parse_version("v2026-11-01-01") > selfupdate.parse_version("2026-10-31#99")
    for bad in ("", None, "1.2.3", "v2026-10-24", "latest", "v2026-10-24-01-rc1"):
        assert selfupdate.parse_version(bad) is None


def test_display_version():
    assert selfupdate.display_version("v2026-11-02-03") == "2026-11-02#03"
    assert selfupdate.display_version("odd") == "odd"


def test_repo_from_url_accepts_the_apps_urls_and_rejects_anything_else():
    assert selfupdate.repo_from_url("https://github.com/Erlbon/cbzredactor") == "Erlbon/cbzredactor"
    assert selfupdate.repo_from_url("https://github.com/erlbon/mp3redactor/") == "erlbon/mp3redactor"
    assert selfupdate.repo_from_url("https://github.com/Erlbon/redactor_common.git") == "Erlbon/redactor_common"
    for bad in ("https://evil.example/Erlbon/x", "https://github.com/Erlbon", "https://github.com/a/b/../c", ""):
        with pytest.raises(ValueError):
            selfupdate.repo_from_url(bad)


def test_asset_name_per_platform():
    assert selfupdate.asset_name("cbzredactor", "win32") == "cbzredactor.exe"
    assert selfupdate.asset_name("cbzredactor", "linux") == "cbzredactor-linux-x86_64.tar.gz"


# --- reading the API answer ----------------------------------------------------------------------------------------------


def test_release_from_json_picks_this_platforms_asset_and_digest():
    r = release()
    assert r.tag == "v2026-11-02-03" and r.display == "2026-11-02#03"
    assert r.asset_name == "cbzredactor.exe" and r.asset_size == len(PAYLOAD) and r.sha256 == DIGEST
    assert r.downloadable and r.notes == "## Notes"
    linux = selfupdate.release_from_json(api_answer(), "cbzredactor", "linux")
    assert linux.asset_name.endswith(".tar.gz")


def test_an_asset_without_digest_is_not_downloadable():
    assert not release(digest=None).downloadable
    assert not release(digest="md5:abc").downloadable
    assert not release(name="other.exe").downloadable


def test_a_download_url_off_github_is_dropped():
    data = api_answer()
    data["assets"][1]["browser_download_url"] = "https://evil.example/cbzredactor.exe"
    assert not selfupdate.release_from_json(data, "cbzredactor", "win32").downloadable


def test_odd_answers_raise_update_error_not_a_crash():
    for data in ({}, [], None, {"tag_name": "nightly"}, {"tag_name": 5}):
        with pytest.raises(UpdateError):
            selfupdate.release_from_json(data, "cbzredactor", "win32")
    r = selfupdate.release_from_json({"tag_name": "v2026-11-02-03", "assets": "x", "body": None}, "cbzredactor")
    assert not r.downloadable and r.notes == ""


def test_fetch_latest_asks_the_right_url():
    seen = []

    def fetch(request):
        seen.append(request.full_url)
        return json.dumps(api_answer()).encode()

    r = selfupdate.fetch_latest("Erlbon/cbzredactor", "cbzredactor", fetch, "win32")
    assert seen == ["https://api.github.com/repos/Erlbon/cbzredactor/releases/latest"]
    assert r.sha256 == DIGEST


def test_fetch_latest_turns_failures_into_update_error():
    def offline(_request):
        raise urllib.error.URLError("no network")

    def missing(request):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    with pytest.raises(UpdateError, match="Could not reach GitHub"):
        selfupdate.fetch_latest("Erlbon/cbzredactor", "cbzredactor", offline)
    with pytest.raises(UpdateError, match="no published release"):
        selfupdate.fetch_latest("Erlbon/cbzredactor", "cbzredactor", missing)


def test_is_newer():
    r = release(tag="v2026-11-02-03")
    assert selfupdate.is_newer(r, "2026-11-02#02")
    assert not selfupdate.is_newer(r, "2026-11-02#03")
    assert not selfupdate.is_newer(r, "2026-12-01#01")


# --- download -----------------------------------------------------------------------------------------------------------


def test_download_checks_and_keeps_a_good_file(tmp_path):
    dest = tmp_path / "app.exe.new"
    seen = []
    got = selfupdate.download(release(), dest, lambda d, t: seen.append((d, t)), opener=FakeOpener())
    assert got == dest and dest.read_bytes() == PAYLOAD
    assert seen[-1] == (len(PAYLOAD), len(PAYLOAD))
    assert not (tmp_path / "app.exe.new.part").exists()


@pytest.mark.parametrize("data, message", [
    (PAYLOAD[:-1] + b"X", "checksum"),
    (PAYLOAD[:100], "stopped early"),
    (PAYLOAD + b"more", "larger than GitHub says"),
], ids=["tampered", "short", "long"])
def test_a_wrong_download_is_refused_and_removed(tmp_path, data, message):
    dest = tmp_path / "app.exe.new"
    with pytest.raises(UpdateError, match=message):
        selfupdate.download(release(), dest, opener=FakeOpener(data))
    assert list(tmp_path.iterdir()) == []


def test_a_network_failure_is_an_update_error_and_leaves_nothing(tmp_path):
    with pytest.raises(UpdateError, match="Could not download"):
        selfupdate.download(release(), tmp_path / "a.new", opener=FakeOpener(error=OSError("reset")))
    assert list(tmp_path.iterdir()) == []


def test_cancel_stops_and_removes_the_partial_file(tmp_path):
    cancel = threading.Event()
    calls = []

    def progress(done, total):
        calls.append(done)
        cancel.set()

    assert selfupdate.download(release(), tmp_path / "a.new", progress, cancel, opener=FakeOpener()) is None
    assert len(calls) == 1 and list(tmp_path.iterdir()) == []


def test_download_refuses_an_unverifiable_release(tmp_path):
    with pytest.raises(UpdateError, match="no verifiable download"):
        selfupdate.download(release(digest=None), tmp_path / "a.new", opener=FakeOpener())


# --- install and leftovers ----------------------------------------------------------------------------------------------


def test_install_swaps_by_renaming(tmp_path):
    exe = tmp_path / "cbzredactor.exe"
    exe.write_bytes(b"old")
    new = tmp_path / "cbzredactor.exe.new"
    new.write_bytes(b"new")
    (tmp_path / "cbzredactor.exe.old").write_bytes(b"older")      # left by an earlier update
    old = selfupdate.install(new, exe)
    assert exe.read_bytes() == b"new" and old.read_bytes() == b"old" and not new.exists()


def test_install_puts_the_old_exe_back_when_the_new_one_cannot_be_moved(tmp_path):
    exe = tmp_path / "cbzredactor.exe"
    exe.write_bytes(b"old")
    with pytest.raises(UpdateError, match="new version in place"):
        selfupdate.install(tmp_path / "missing.new", exe)
    assert exe.read_bytes() == b"old" and not (tmp_path / "cbzredactor.exe.old").exists()


def test_remove_leftovers_only_when_frozen(tmp_path):
    exe = tmp_path / "app.exe"
    exe.write_bytes(b"x")
    for suffix in (".old", ".new", ".new.part"):
        (tmp_path / ("app.exe" + suffix)).write_bytes(b"y")
    selfupdate.remove_leftovers(exe, frozen=False)
    assert len(list(tmp_path.iterdir())) == 4
    selfupdate.remove_leftovers(exe, frozen=True)
    assert [p.name for p in tmp_path.iterdir()] == ["app.exe"]


def test_restart_command_resets_the_pyinstaller_environment(tmp_path):
    argv, env = selfupdate.restart_command(tmp_path / "app.exe", {
        "PATH": "p", "_PYI_APPLICATION_HOME_DIR": "x", "_PYI_ARCHIVE_FILE": "y", "_MEIPASS2": "z"})
    assert argv == [str(tmp_path / "app.exe")]
    assert env == {"PATH": "p", "PYINSTALLER_RESET_ENVIRONMENT": "1"}


def test_can_self_replace(tmp_path):
    exe = tmp_path / "app.exe"
    assert selfupdate.can_self_replace(exe, "win32", frozen=True) == (True, "")
    ok, why = selfupdate.can_self_replace(exe, "win32", frozen=False)
    assert not ok and "git pull" in why
    ok, why = selfupdate.can_self_replace(exe, "linux", frozen=True)
    assert not ok and "download" in why
    ok, why = selfupdate.can_self_replace(tmp_path / "missing" / "app.exe", "win32", frozen=True)
    assert not ok and "cannot be written" in why
    assert list(tmp_path.iterdir()) == []     # the write probe cleans up after itself


# --- settings ---------------------------------------------------------------------------------------------------------


def test_settings_default_off_and_due_once_a_day(tmp_path):
    s = UpdateSettings(tmp_path / "x_updates.json")
    assert not s.enabled and not s.due(now=10**9) and s.skipped == ""
    s.update(check_for_updates=True)
    assert s.due(now=10**9)
    s.update(last_check=10**9)
    assert not s.due(now=10**9 + 3600)
    assert s.due(now=10**9 + selfupdate.CHECK_INTERVAL)
    assert s.due(now=10**9 - 5)                  # the clock went back: check anyway


def test_a_broken_settings_file_reads_as_defaults(tmp_path):
    path = tmp_path / "x_updates.json"
    for text in ("{oops", "[1]", '{"check_for_updates": "yes", "last_check": "x", "skipped": 3}'):
        path.write_text(text, encoding="utf-8")
        s = UpdateSettings(path)
        assert (s.enabled, s.last_check, s.skipped) == (False, 0.0, "")
    s.update(skipped="v1")
    assert json.loads(path.read_text(encoding="utf-8"))["skipped"] == "v1"


def test_preferences_section_is_off_by_default():
    section = selfupdate.updates_section()
    (spec,) = section.specs
    assert spec.key == selfupdate.KEY_CHECK_FOR_UPDATES and spec.kind == "bool" and spec.default is False


def test_with_update_settings_routes_only_the_update_key(tmp_path):
    app = MemoryBackend({"ascii_filenames": True})
    settings = UpdateSettings(tmp_path / "x_updates.json")
    backend = selfupdate.with_update_settings(app, settings)
    assert backend.get(selfupdate.KEY_CHECK_FOR_UPDATES) is False
    assert backend.get("ascii_filenames") is True
    backend.set_many({selfupdate.KEY_CHECK_FOR_UPDATES: True, "ascii_filenames": False})
    assert settings.enabled and app.writes == [{"ascii_filenames": False}]
    backend.set_many({selfupdate.KEY_CHECK_FOR_UPDATES: False})
    assert not settings.enabled and len(app.writes) == 1     # nothing else changed: the app's store is not written


def test_selfupdate_is_qt_free():
    import subprocess
    import sys
    code = "import sys, redactor_common.core.selfupdate; sys.exit(1 if any(m.startswith('PyQt') for m in sys.modules) else 0)"
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0

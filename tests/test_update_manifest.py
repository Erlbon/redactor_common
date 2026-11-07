"""The update framework (docs/update-framework.md): the release manifest, the declared channel, and the decision a copy
makes about a release, with every way of not updating named. Driven against a fake GitHub (no network)."""

import hashlib
import json

import pytest

from redactor_common.core import selfupdate, update_manifest
from redactor_common.core.selfupdate import INSTALL, NONE, PAGE, UpdateError
from redactor_common.core.update_manifest import WINDOWS_EXE, WINDOWS_INSTALLER, WINDOWS_ZIP

SETUP = b"the new installer " * 1000
ZIP = b"the new zip " * 500
REPO = "Erlbon/accipio"
TAG = "v2026-11-07-02"
SETUP_NAME = "Accipio-2026-11-07-02-windows-x64-setup.exe"
ZIP_NAME = "Accipio-2026-11-07-02-windows-x64.zip"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def asset(name, data, digest=True):
    entry = {"name": name, "size": len(data),
             "browser_download_url": f"https://github.com/{REPO}/releases/download/{TAG}/{name}"}
    if digest:
        entry["digest"] = "sha256:" + sha(data)
    return entry


def manifest_bytes(**changes):
    packages = {WINDOWS_INSTALLER: {"file": SETUP_NAME, "size": len(SETUP), "sha256": sha(SETUP)},
                WINDOWS_ZIP: {"file": ZIP_NAME, "size": len(ZIP), "sha256": sha(ZIP)}}
    data = {"schema": 1, "app": "accipio", "tag": TAG, "version": "2026-11-07#02", "min_client": 1,
            "packages": packages}
    data.update(changes)
    return json.dumps(data).encode()


class FakeGitHub:
    """The release API answer for `tag` and the files it lists; records the URLs asked for."""

    def __init__(self, manifest=None, assets=None, tag=TAG):
        self.manifest = manifest_bytes() if manifest is None else manifest
        self.files = {}
        if assets is None:
            assets = [asset(SETUP_NAME, SETUP), asset(ZIP_NAME, ZIP)]
            if self.manifest is not False:
                assets.append(asset(update_manifest.MANIFEST_NAME, self.manifest))
        for entry in assets:
            self.files[entry["browser_download_url"]] = {SETUP_NAME: SETUP, ZIP_NAME: ZIP}.get(
                entry["name"], self.manifest)
        self.answer = {"tag_name": tag, "html_url": f"https://github.com/{REPO}/releases/tag/{tag}",
                       "body": "* new things", "assets": assets}
        self.urls = []

    def __call__(self, request):
        url = request.full_url if hasattr(request, "full_url") else request
        self.urls.append(url)
        if url.startswith("https://api.github.com/"):
            return json.dumps(self.answer).encode()
        return self.files[url]


def decide(github=None, channel=WINDOWS_INSTALLER, current="2026-11-06#13", tag="", writable=True):
    return selfupdate.decide(REPO, "accipio", current, channel, fetch=github or FakeGitHub(), tag=tag,
                             writable=lambda: (True, "") if writable else (False, "The folder cannot be written to."))


# --- the manifest ------------------------------------------------------------------------------------------------------


def test_build_lists_each_channels_file_with_size_and_checksum(tmp_path):
    setup = tmp_path / SETUP_NAME
    setup.write_bytes(SETUP)
    data = update_manifest.build("accipio", TAG, "2026-11-07#02", {WINDOWS_INSTALLER: setup})
    assert data == {"schema": 1, "app": "accipio", "tag": TAG, "version": "2026-11-07#02", "min_client": 1,
                    "packages": {WINDOWS_INSTALLER: {"file": SETUP_NAME, "size": len(SETUP), "sha256": sha(SETUP)}}}
    parsed = update_manifest.parse(json.dumps(data).encode())
    assert parsed.packages[WINDOWS_INSTALLER] == update_manifest.Package(SETUP_NAME, len(SETUP), sha(SETUP))


def test_build_refuses_an_unknown_channel_or_a_missing_file(tmp_path):
    with pytest.raises(ValueError, match="channel"):
        update_manifest.build("accipio", TAG, "2026-11-07#02", {"windows-msi": tmp_path / "x"})
    with pytest.raises(ValueError, match="not found"):
        update_manifest.build("accipio", TAG, "2026-11-07#02", {WINDOWS_ZIP: tmp_path / "missing.zip"})


@pytest.mark.parametrize("bad, why", [
    (b"not json", "not readable"),
    (b"[]", "not readable"),
    (manifest_bytes(schema="1"), "schema"),
    (manifest_bytes(packages={WINDOWS_INSTALLER: {"file": "../evil.exe", "size": 1, "sha256": "a" * 64}}), "file name"),
    (manifest_bytes(packages={WINDOWS_INSTALLER: {"file": "a.exe", "size": 0, "sha256": "a" * 64}}), "size"),
    (manifest_bytes(packages={WINDOWS_INSTALLER: {"file": "a.exe", "size": 1, "sha256": "xyz"}}), "checksum"),
    (manifest_bytes(packages={"windows-msi": {"file": "a.msi", "size": 1, "sha256": "a" * 64}}), "channel"),
])
def test_parse_names_what_is_wrong(bad, why):
    with pytest.raises(ValueError, match=why):
        update_manifest.parse(bad)


# --- the channel -------------------------------------------------------------------------------------------------------


def test_the_channel_is_read_from_the_file_beside_the_exe(tmp_path):
    exe = tmp_path / "App.exe"
    assert update_manifest.read_channel(exe, frozen=True) == ""          # unknown: never guessed
    update_manifest.write_channel(tmp_path, WINDOWS_INSTALLER)
    assert update_manifest.read_channel(exe, frozen=True) == WINDOWS_INSTALLER
    assert update_manifest.read_channel(exe, frozen=False) == update_manifest.SOURCE
    (tmp_path / update_manifest.CHANNEL_FILE).write_text("something-else\n", encoding="utf-8")
    assert update_manifest.read_channel(exe, frozen=True) == ""


def test_a_one_file_build_falls_back_to_its_bundled_channel(tmp_path):
    bundle = tmp_path / "meipass"
    update_manifest.write_channel(bundle, WINDOWS_EXE)
    exe = tmp_path / "cbzredactor.exe"
    assert update_manifest.read_channel(exe, frozen=True, bundle=bundle) == WINDOWS_EXE
    update_manifest.write_channel(tmp_path, WINDOWS_ZIP)                 # the file beside the exe wins
    assert update_manifest.read_channel(exe, frozen=True, bundle=bundle) == WINDOWS_ZIP


# --- the decision ------------------------------------------------------------------------------------------------------


def test_an_installed_copy_is_offered_the_installer_the_manifest_names():
    github = FakeGitHub()
    d = decide(github)
    assert d.action == INSTALL and d.reason == ""
    assert d.release.asset_name == SETUP_NAME and d.release.asset_size == len(SETUP) and d.release.sha256 == sha(SETUP)
    assert d.release.downloadable
    assert github.urls[0] == f"https://api.github.com/repos/{REPO}/releases/latest"


def test_a_given_release_tag_is_asked_for_by_tag():
    github = FakeGitHub()
    decide(github, tag=TAG)
    assert github.urls[0] == f"https://api.github.com/repos/{REPO}/releases/tags/{TAG}"


def test_the_newest_version_needs_nothing():
    d = decide(current="2026-11-07#02")
    assert d.action == NONE and "newest version" in d.reason


@pytest.mark.parametrize("channel, says", [
    (WINDOWS_ZIP, "zip"),
    (update_manifest.LINUX_TARBALL, "Linux"),
    (update_manifest.SOURCE, "git pull"),
    ("", "cannot tell how it was installed"),
])
def test_copies_that_cannot_replace_themselves_say_why(channel, says):
    d = decide(channel=channel)
    assert d.action == PAGE and says in d.reason and d.release.page_url


def test_a_release_without_a_manifest_says_so():
    d = decide(FakeGitHub(manifest=False))
    assert d.action == PAGE and update_manifest.MANIFEST_NAME in d.reason and "before" in d.reason


def test_a_manifest_that_does_not_match_its_digest_is_refused():
    github = FakeGitHub()
    url = next(u for u in github.files if u.endswith(update_manifest.MANIFEST_NAME))
    github.files[url] = manifest_bytes(version="2099-01-01#01")
    d = decide(github)
    assert d.action == PAGE and "does not match" in d.reason


def test_a_newer_schema_or_client_level_says_so():
    assert "newer" in decide(FakeGitHub(manifest_bytes(schema=2))).reason
    d = decide(FakeGitHub(manifest_bytes(min_client=99)))
    assert d.action == PAGE and "too old" in d.reason


def test_a_manifest_for_another_program_or_release_is_refused():
    assert "another program" in decide(FakeGitHub(manifest_bytes(app="dossier"))).reason
    assert "another release" in decide(FakeGitHub(manifest_bytes(tag="v2026-11-01-01"))).reason


def test_a_release_with_no_package_for_this_channel_names_the_channel():
    d = decide(channel=WINDOWS_EXE)
    assert d.action == PAGE and WINDOWS_EXE in d.reason


def test_a_package_the_release_does_not_hold_is_refused():
    manifest = manifest_bytes()
    assets = [asset(ZIP_NAME, ZIP), asset(update_manifest.MANIFEST_NAME, manifest),
              asset(SETUP_NAME, b"something else")]
    d = decide(FakeGitHub(manifest, assets))
    assert d.action == PAGE and SETUP_NAME in d.reason and "does not match" in d.reason


def test_an_exe_copy_in_a_folder_it_cannot_write_says_so():
    packages = {WINDOWS_EXE: {"file": "accipio.exe", "size": len(SETUP), "sha256": sha(SETUP)}}
    manifest = manifest_bytes(packages=packages)
    assets = [asset("accipio.exe", SETUP), asset(update_manifest.MANIFEST_NAME, manifest)]
    github = FakeGitHub(manifest, assets)
    github.files[assets[0]["browser_download_url"]] = SETUP
    assert decide(github, channel=WINDOWS_EXE).action == INSTALL
    d = decide(github, channel=WINDOWS_EXE, writable=False)
    assert d.action == PAGE and "cannot be written" in d.reason


def test_a_github_failure_is_an_update_error():
    def offline(_request):
        raise OSError("no network")
    with pytest.raises(UpdateError, match="GitHub"):
        selfupdate.decide(REPO, "accipio", "2026-11-06#13", WINDOWS_INSTALLER, fetch=offline)


def test_the_decision_reads_as_json_and_as_details():
    d = decide(channel=WINDOWS_ZIP)
    data = d.as_json()
    assert data["action"] == PAGE and data["channel"] == WINDOWS_ZIP and data["release"] == TAG
    assert data["current"] == "2026-11-06#13" and data["file"] == "" and "zip" in data["reason"]
    details = d.details("Accipio")
    for text in ("Accipio 2026-11-06#13", "Channel: windows-zip", f"Release: {TAG}", "Reason:"):
        assert text in details

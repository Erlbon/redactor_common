"""release.py: the release tool's offline parts (configuration, zip, manifest, notes, the checks on a published
release and the refusals). The gate itself runs against real releases; see docs/update-framework.md."""

import json
import subprocess
import zipfile

import pytest

from redactor_common import release
from redactor_common.core import update_manifest
from redactor_common.core.update_manifest import MANIFEST_NAME, WINDOWS_INSTALLER, WINDOWS_ZIP

PYPROJECT = """
[project]
name = "app"

[tool.redactor-release]
app = "app"
name = "The App"
repo = "Erlbon/app"
version_file = "app/version.py"
build = ["cmd", "/c", "call build_exe.bat < nul"]
exe = "App.exe"
installer_app_id = "{{1234}"
[tool.redactor-release.zip]
folder = "dist/App"
file = "dist/App-{build}.zip"
[tool.redactor-release.packages]
windows-installer = "dist/App-{build}-setup.exe"
windows-zip = "dist/App-{build}.zip"
"""


def repo(tmp_path, pyproject=PYPROJECT):
    (tmp_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "version.py").write_text('APP_VERSION = "2026-11-07#02"\n', encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 2026-11-07#02 -- The new one\n\n- one\n- two\n\n## 2026-11-07#01 -- Old\n\n- old\n",
        encoding="utf-8")
    return release.load_config(tmp_path)


def built(cfg):
    folder = cfg.root / "dist" / "App"
    folder.mkdir(parents=True)
    (folder / "App.exe").write_bytes(b"MZ")
    (cfg.root / "dist" / "App-2026-11-07-02-setup.exe").write_bytes(b"setup")
    return folder


def test_the_configuration_names_version_tag_and_files(tmp_path):
    cfg = repo(tmp_path)
    assert (cfg.version, cfg.build_name, cfg.tag) == ("2026-11-07#02", "2026-11-07-02", "v2026-11-07-02")
    assert cfg.package_paths()[WINDOWS_INSTALLER] == tmp_path / "dist" / "App-2026-11-07-02-setup.exe"
    assert cfg.installer_app_id == "{{1234}"


@pytest.mark.parametrize("change, says", [
    (('app = "app"', 'app = "other"'), "repository's name"),
    (('windows-zip =', 'windows-msi ='), "unknown channel"),
    (('installer_app_id = "{{1234}"', ''), "installer_app_id"),
    (('exe = "App.exe"', ''), "lacks exe"),
])
def test_a_bad_configuration_is_refused(tmp_path, change, says):
    with pytest.raises(release.ReleaseError, match=says):
        repo(tmp_path, PYPROJECT.replace(*change))


def test_the_zip_must_declare_its_channel(tmp_path):
    cfg = repo(tmp_path)
    folder = built(cfg)
    with pytest.raises(release.ReleaseError, match="does not declare channel windows-zip"):
        release.make_zip(cfg)
    update_manifest.write_channel(folder, WINDOWS_ZIP)
    target = release.make_zip(cfg)
    assert sorted(zipfile.ZipFile(target).namelist()) == ["App/App.exe", "App/update-channel"]


def test_the_manifest_lists_the_configured_packages(tmp_path):
    cfg = repo(tmp_path)
    update_manifest.write_channel(built(cfg), WINDOWS_ZIP)
    release.make_zip(cfg)
    data = json.loads(release.write_manifest(cfg).read_text(encoding="utf-8"))
    assert data["app"] == "app" and data["tag"] == "v2026-11-07-02" and data["version"] == "2026-11-07#02"
    assert data["packages"][WINDOWS_INSTALLER]["file"] == "App-2026-11-07-02-setup.exe"
    assert data["packages"][WINDOWS_ZIP]["file"] == "App-2026-11-07-02.zip"
    assert [p.name for p in release.assets_of(cfg)] == ["App-2026-11-07-02-setup.exe", "App-2026-11-07-02.zip",
                                                       MANIFEST_NAME]


def test_the_notes_are_this_versions_changelog_section(tmp_path):
    assert release.release_notes(repo(tmp_path)) == "- one\n- two"


class FakeRunner:
    """Answers git and gh like a repository whose main matches origin; `gh` answers come from `releases`."""

    def __init__(self, releases=None, status="", branch="main", files=None):
        self.releases, self.status, self.branch, self.files = releases or {}, status, branch, files or {}
        self.calls = []

    def __call__(self, argv, cwd=None, check=True, env=None, capture=True):
        self.calls.append(list(argv))
        out, code = "", 0
        if argv[:2] == ["git", "status"]:
            out = self.status
        elif argv[:2] == ["git", "rev-parse"]:
            out = self.branch if "--abbrev-ref" in argv else "abc123"
        elif argv[:2] == ["gh", "api"]:
            key = argv[2].split("/releases/", 1)[1]
            if key in self.releases:
                out = json.dumps(self.releases[key])
            else:
                code = 1
        elif argv[:3] == ["gh", "release", "download"]:
            name, folder = argv[argv.index("--pattern") + 1], argv[argv.index("--dir") + 1]
            (release.Path(folder) / name).write_bytes(self.files[name])
        if check and code:
            raise release.ReleaseError(f"{argv} failed")
        return subprocess.CompletedProcess(argv, code, out, "")


def test_preflight_refuses_a_dirty_tree_another_branch_or_a_published_release(tmp_path):
    cfg = repo(tmp_path)
    with pytest.raises(release.ReleaseError, match="not clean"):
        release.preflight(cfg, FakeRunner(status=" M x.py"))
    with pytest.raises(release.ReleaseError, match="not main"):
        release.preflight(cfg, FakeRunner(branch="work"))
    with pytest.raises(release.ReleaseError, match="already exists"):
        release.preflight(cfg, FakeRunner({"tags/v2026-11-07-02": {"prerelease": False}}))
    release.preflight(cfg, FakeRunner({"tags/v2026-11-07-02": {"prerelease": True}}))     # resuming is fine
    release.preflight(cfg, FakeRunner())


def published(cfg, tamper=None):
    """A fake GitHub holding cfg's built release (its manifest and packages); `tamper` changes one download."""
    import hashlib
    update_manifest.write_channel(built(cfg), WINDOWS_ZIP)
    release.make_zip(cfg)
    release.write_manifest(cfg)
    files = {p.name: p.read_bytes() for p in release.assets_of(cfg)}
    assets = [{"name": n, "size": len(d), "digest": "sha256:" + hashlib.sha256(d).hexdigest(),
               "browser_download_url": f"https://github.com/Erlbon/app/releases/download/{cfg.tag}/{n}"}
              for n, d in files.items()]
    if tamper:
        files[tamper] = b"something else"
    return FakeRunner({f"tags/{cfg.tag}": {"tag_name": cfg.tag, "assets": assets}}, files=files)


def test_verify_release_checks_github_and_the_downloads(tmp_path):
    cfg = repo(tmp_path)
    manifest = release.verify_release(cfg, cfg.tag, tmp_path / "scratch", published(cfg))
    assert set(manifest.packages) == {WINDOWS_INSTALLER, WINDOWS_ZIP}


def test_verify_release_refuses_a_download_that_differs(tmp_path):
    cfg = repo(tmp_path)
    with pytest.raises(release.ReleaseError, match="downloaded back does not match"):
        release.verify_release(cfg, cfg.tag, tmp_path / "scratch", published(cfg, "App-2026-11-07-02-setup.exe"))


def test_the_gate_wants_a_previous_release_or_says_how_to_go_on(tmp_path, monkeypatch):
    cfg = repo(tmp_path)
    monkeypatch.setenv("REDACTOR_GATE_DIR", str(tmp_path / "gate"))
    runner = published(cfg)
    runner.releases["latest"] = {"tag_name": "v2026-11-06-13"}
    runner.releases["tags/v2026-11-06-13"] = {"tag_name": "v2026-11-06-13", "assets": []}
    with pytest.raises(release.ReleaseError, match="--first-framework-release"):
        release.gate(cfg, cfg.tag, runner=runner)
    assert (tmp_path / "gate" / f"app-{cfg.tag}").is_dir()          # kept for a look after a failure


def test_the_build_uses_the_repositorys_own_venv(tmp_path):
    cfg = repo(tmp_path)
    assert release.build_env(cfg)["PATH"] == release.os.environ.get("PATH", "")
    scripts = tmp_path / ".venv" / ("Scripts" if release.sys.platform == "win32" else "bin")
    scripts.mkdir(parents=True)
    env = release.build_env(cfg)
    assert env["PATH"].split(release.os.pathsep)[0] == str(scripts) and env["VIRTUAL_ENV"] == str(scripts.parent)


def test_a_build_script_is_called_by_its_full_path(tmp_path):
    """2026-11-07: "cmd /c call build_exe.bat" in the repository folder answered "not recognized"."""
    cfg = repo(tmp_path, PYPROJECT.replace('build = ["cmd", "/c", "call build_exe.bat < nul"]',
                                           'build = ["build_exe.bat", "--x"]'))
    assert release.build_command(cfg) == ["build_exe.bat", "--x"]          # not there: left as written
    (tmp_path / "build_exe.bat").write_text("@echo off\n", encoding="utf-8")
    assert release.build_command(cfg) == ["cmd", "/d", "/c", "call", str(tmp_path / "build_exe.bat"), "--x"]


def test_publish_can_leave_the_gated_release_a_pre_release(tmp_path, monkeypatch):
    cfg = repo(tmp_path)
    steps = []
    for name in ("preflight", "make_zip", "write_manifest", "gate", "promote"):
        monkeypatch.setattr(release, name, lambda *a, _n=name, **k: steps.append(_n))
    monkeypatch.setattr(release, "assets_of", lambda cfg: [])
    monkeypatch.setattr(release, "release_exists", lambda *a: {"prerelease": True})
    (tmp_path / "dist").mkdir()
    runner = FakeRunner()
    release.publish(cfg, skip_build=True, first=True, runner=runner, promote_after=False)
    assert "gate" in steps and "promote" not in steps
    release.publish(cfg, skip_build=True, first=True, runner=runner)
    assert steps[-1] == "promote"


def test_contract_path_cli(capsys):
    assert release.main(["contract-path"]) == 0
    assert capsys.readouterr().out.strip().endswith("update-contract.iss")

"""release.py: the release tool's offline parts (configuration, zip, manifest, notes, the checks on a published
release and the refusals). The gate itself runs against real releases; see docs/update-framework.md."""

import json
import subprocess
import sys
import types
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
    cfg = repo(tmp_path)
    assert release.release_notes(cfg) == "- one\n- two"
    assert cfg.release_title == "The App 2026-11-07#02"


def test_an_app_can_name_its_title_and_notes_file_by_its_release_number(tmp_path):
    """Dossier: "Dossier 0.8 (build 2026-11-07#02)", notes in docs/releases/v0.8.md."""
    cfg = repo(tmp_path, PYPROJECT.replace('exe = "App.exe"', 'exe = "App.exe"\ntitle = "{name} {release} (build {version})"\n'
                                                              'notes = "docs/releases/v{release}.md"'))
    with pytest.raises(release.ReleaseError, match="no RELEASE"):
        cfg.release_title
    (tmp_path / "app" / "version.py").write_text('RELEASE = "0.8"\nAPP_VERSION = "2026-11-07#02"\n', encoding="utf-8")
    assert cfg.release_title == "The App 0.8 (build 2026-11-07#02)"
    with pytest.raises(release.ReleaseError, match="write .*v0.8.md first"):
        release.release_notes(cfg)
    (tmp_path / "docs" / "releases").mkdir(parents=True)
    (tmp_path / "docs" / "releases" / "v0.8.md").write_text("Notes for 0.8\n", encoding="utf-8")
    assert release.release_notes(cfg) == "Notes for 0.8"


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


def test_the_gate_asks_a_one_file_exe_for_the_channel_it_carries_inside(tmp_path, monkeypatch):
    """2026-11-07, Dossier 0.8: the one-file dossier.exe bundles "windows-exe" (no file beside it), and the gate read
    only the folder, so it said the copy declared nothing."""
    cfg = repo(tmp_path)
    folder = tmp_path / "program"
    folder.mkdir()
    gate = release.Gate(cfg, cfg.tag, tmp_path / "scratch", FakeRunner())
    asked = []
    monkeypatch.setattr(gate, "ask", lambda exe, *args: asked.append(args) or {"channel": "windows-exe"})
    gate.expect_channel(folder, "windows-exe")
    assert asked == [("--check-update", "--release-tag", cfg.tag)]
    with pytest.raises(release.ReleaseError, match="declares channel 'windows-exe', expected 'windows-installer'"):
        gate.expect_channel(folder, "windows-installer")
    update_manifest.write_channel(folder, "windows-installer")             # a file beside the exe wins, nothing asked
    asked.clear()
    gate.expect_channel(folder, "windows-installer")
    assert asked == []


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


# ---- 81A (2026-11-07): the gate runs the program, as a user does ---------------------------------------------------

PROGRAM = """
import os, sys, time
profile = os.environ["REDACTOR_CONFIG_DIR"]
stop = os.path.join(profile, "stop")
if "--quit" in sys.argv:
    open(stop, "w").close()
    sys.exit(0)
mode = open(os.path.join(profile, "mode")).read() if os.path.exists(os.path.join(profile, "mode")) else "fine"
if mode == "dies":
    sys.exit(3)
# every start writes crash_log's start marker, as run_app does; only what comes after it is a crash
open(os.path.join(profile, "crash.log"), "a").write(chr(10) + "=" * 70 + chr(10) + "2026-11-07 16:54:21 started: pid 1" + chr(10))
if mode == "crashes":
    open(os.path.join(profile, "crash.log"), "a").write("Traceback: ConnectionResetError")
while not os.path.exists(stop):
    time.sleep(0.05)
os.remove(stop)
"""


def running_gate(tmp_path, monkeypatch, mode="fine"):
    monkeypatch.setattr(release, "RUN_SETTLE", 0.5)
    cfg = repo(tmp_path, PYPROJECT + '\n[tool.redactor-release.gate]\nrun = ["PROGRAM", "--minimized"]\n'
                                      'quit = ["PROGRAM", "--quit"]\ncrash_log = "crash.log"\nlive = 1\n')
    program = tmp_path / "program.py"
    program.write_text(PROGRAM, encoding="utf-8")
    cfg.gate_run = [str(program), "--minimized"]
    cfg.gate_quit = [str(program), "--quit"]
    gate = release.Gate(cfg, cfg.tag, tmp_path / "scratch")
    (gate.profile / "mode").write_text(mode, encoding="utf-8")
    return gate, release.Path(release.sys.executable)


def test_the_gate_config_needs_both_run_and_quit(tmp_path):
    with pytest.raises(release.ReleaseError, match="both run and quit"):
        repo(tmp_path, PYPROJECT + '\n[tool.redactor-release.gate]\nrun = ["--minimized"]\n')


def test_the_new_program_must_run_and_stop_when_asked(tmp_path, monkeypatch):
    gate, exe = running_gate(tmp_path, monkeypatch)
    gate.expect_it_runs(exe)


def test_a_program_that_crashes_on_its_first_start_fails_the_gate(tmp_path, monkeypatch):
    gate, exe = running_gate(tmp_path, monkeypatch, "crashes")
    with pytest.raises(release.ReleaseError, match=r"crash log(.|\n)*ConnectionResetError"):
        gate.expect_it_runs(exe)


def test_only_what_follows_the_start_markers_is_a_crash():
    from redactor_common.core import crash_log
    started = crash_log.format_start_marker("Accipio 2026-11-07#10") * 2
    assert release.crash_entries(started) == ""
    assert release.crash_entries(started + "Windows fatal exception: access violation") == \
        "Windows fatal exception: access violation"


@pytest.mark.skipif(sys.platform != "win32", reason="Inno Setup's uninstall entry is in Windows' registry")
def test_a_dead_scratch_install_is_forgotten(tmp_path, monkeypatch):
    """2026-11-07: a failed gate's scratch folder was wiped before its install was uninstalled, so the next gate
    found an uninstall entry (and a Start menu shortcut) pointing at nothing."""
    import uuid
    import winreg
    app_id = "{" + str(uuid.uuid4()).upper() + "}"
    location = tmp_path / "gate" / "x-v1" / "windows-installer" / "program"        # never created: wiped
    key = release.UNINSTALL_KEY.format(app_id=app_id)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as entry:
        winreg.SetValueEx(entry, "InstallLocation", 0, winreg.REG_SZ, str(location) + "\\")
        winreg.SetValueEx(entry, "Inno Setup: Icon Group", 0, winreg.REG_SZ, "Thing")
    programs = tmp_path / "appdata" / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    programs.mkdir(parents=True)
    (programs / "Thing.lnk").write_bytes(b"lnk" + str(location / "Thing.exe").encode("utf-16-le"))
    (programs / "Other.lnk").write_bytes(b"lnk elsewhere")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    cfg = types.SimpleNamespace(installer_app_id=app_id)
    try:
        release.remove_leftover_install(cfg, location, lambda target: pytest.fail("there is no uninstaller"))
        assert release.installed_location(app_id) is None
        assert not (programs / "Thing.lnk").exists() and (programs / "Other.lnk").exists()
    finally:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except OSError:
            pass


def test_a_program_that_stops_by_itself_fails_the_gate(tmp_path, monkeypatch):
    gate, exe = running_gate(tmp_path, monkeypatch, "dies")
    with pytest.raises(release.ReleaseError, match="stopped right after starting"):
        gate.start_running(exe)


def test_an_update_that_closes_the_running_program_by_force_fails_the_gate(tmp_path, monkeypatch):
    gate, exe = running_gate(tmp_path, monkeypatch)
    running = gate.start_running(exe)
    running.kill()                                      # what Windows' Restart Manager does
    with pytest.raises(release.ReleaseError, match="by force"):
        gate.expect_stopped_by_itself(running, "")


def test_the_installer_log_shows_a_forced_close(tmp_path, monkeypatch):
    gate, exe = running_gate(tmp_path, monkeypatch)
    running = gate.start_running(exe)
    gate.runner([str(exe), *gate.cfg.gate_quit], env=gate.env)
    log = tmp_path / "setup.log"
    log.write_text("2026-11-07 12:00:01.000   RestartManager found an application using one of our files: aria2c.exe\n",
                   encoding="utf-8")
    with pytest.raises(release.ReleaseError, match="aria2c.exe"):
        gate.expect_stopped_by_itself(running, str(log))
    clean = tmp_path / "clean.log"
    clean.write_text("Update contract: waited 250 ms for the programs in C:\\x to stop\n", encoding="utf-8")
    running = gate.start_running(exe)
    gate.runner([str(exe), *gate.cfg.gate_quit], env=gate.env)
    gate.expect_stopped_by_itself(running, str(clean))


def test_contract_path_cli(capsys):
    assert release.main(["contract-path"]) == 0
    assert capsys.readouterr().out.strip().endswith("update-contract.iss")


def test_detach_passes_on_everything_but_itself(tmp_path, monkeypatch):
    got = {}
    monkeypatch.setattr(release, "detach", lambda argv, log: got.update(argv=argv, log=log) or 0)
    log = tmp_path / "r.log"
    assert release.main(["--repo", "C:\\Dev\\x", "--detach", "--log", str(log), "publish", "--skip-build"]) == 0
    assert got == {"argv": ["--repo", "C:\\Dev\\x", "publish", "--skip-build"], "log": log}


def test_detach_starts_a_process_of_its_own(tmp_path):
    started = {}

    def popen(command, **kw):
        started.update(command=command, **kw)
        return types.SimpleNamespace(pid=4242)
    log = tmp_path / "logs" / "r.log"
    assert release.detach(["publish"], log, popen) == 0
    assert started["command"][1:] == ["-m", "redactor_common.release", "publish"] and log.is_file()
    assert started["env"][release.DETACHED_ENV] == "1" and started["stdin"] == subprocess.DEVNULL
    if sys.platform == "win32":
        assert started["creationflags"] & subprocess.DETACHED_PROCESS


def test_a_detached_run_logs_its_end_line(tmp_path):
    """A real detached child: its log ends with the line a watcher waits for, after this test's process let go."""
    import time
    log = tmp_path / "r.log"
    assert release.main(["--detach", "--log", str(log), "contract-path"]) == 0
    deadline = time.monotonic() + 60
    while release.FINISHED.format(code=0) not in (log.read_text(encoding="utf-8") if log.is_file() else ""):
        assert time.monotonic() < deadline, log.read_text(encoding="utf-8")
        time.sleep(0.2)
    assert "update-contract.iss" in log.read_text(encoding="utf-8")


def test_a_detached_run_that_crashes_still_writes_its_end_line(monkeypatch, capsys):
    monkeypatch.setenv(release.DETACHED_ENV, "1")

    def boom(argv):
        raise RuntimeError("the build exploded")
    monkeypatch.setattr(release, "run_command", boom)
    assert release.main(["publish"]) == 1
    captured = capsys.readouterr()
    assert "the build exploded" in captured.err and release.FINISHED.format(code=1) in captured.out

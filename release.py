"""
redactor_common/release.py: the family's release tool and its release gate (docs/update-framework.md, section 6).

    python -m redactor_common.release publish [--skip-build] [--previous vX | --first-framework-release]
    python -m redactor_common.release gate --tag vX [--previous vY | --first-framework-release]
    python -m redactor_common.release promote --tag vX
    python -m redactor_common.release manifest            (build the manifest from dist\\ only)
    python -m redactor_common.release contract-path       (where installer/update-contract.iss is, for the build)
    python -m redactor_common.release --detach [--log FILE] publish ...   (on its own: outlives the terminal)

Run from the app's repository (or pass --repo PATH). The app describes itself in its pyproject.toml:

    [tool.redactor-release]
    app = "accipio"                                   # the manifest's app: the GitHub repository's name, lower case
    name = "Accipio"
    repo = "Erlbon/accipio"
    version_file = "accipio/core/version.py"          # holds APP_VERSION = "YYYY-MM-DD#NN"
    build = ["build_exe.bat"]                         # a .bat/.cmd is called by its full path, stdin closed
    exe = "Accipio.exe"                               # the program inside an install or the exe package itself
    installer_app_id = "{6C0E5D8A-...}"               # windows-installer only: its Inno Setup AppId
    after_gate = [["python", "-m", "accipio", "--register"]]     # optional: put back what an install changed
    title = "Dossier {release} (build {version})"     # optional: the release's title (default "{name} {version}");
                                                      # {release} is RELEASE = "0.8" in version_file
    notes = "docs/releases/v{release}.md"             # optional: the notes file (default: this version's CHANGELOG section)
    [tool.redactor-release.zip]                     # optional: zip a folder build ("{build}" = 2026-11-07-02)
    folder = "dist/Accipio"
    file = "dist/Accipio-{build}-windows-x64.zip"
    [tool.redactor-release.packages]                  # channel -> built file; becomes update-manifest.json
    windows-installer = "dist/Accipio-{build}-windows-x64-setup.exe"
    windows-zip = "dist/Accipio-{build}-windows-x64.zip"

`publish`: refuses unless the tree is clean, on main and equal to origin/main, and the release does not exist yet;
builds; writes the manifest; tags and pushes the tag; creates the GitHub release as a PRE-release (nobody's copy sees
it); checks every asset against the manifest (GitHub's digests, then downloaded back); runs the gate; and only then
makes the release "Latest". A failure anywhere leaves the release a pre-release, and `gate` / `promote` resume.

The gate, per self-updating channel the release ships, in a scratch folder (REDACTOR_GATE_DIR, else
C:\\Temp\\Claude\\release-gate) with a scratch profile (REDACTOR_CONFIG_DIR, APPDATA, TEMP):
  windows-installer: install the PREVIOUS release with its real installer; it must report its version and channel;
      its `--check-update --release-tag <new>` must say "install" with the manifest's file; its `--update` must
      install the new release, after which the program reports the new version and channel; then uninstall, which
      must leave nothing in the program folder. Refused when the app is installed on this PC for real (the scratch
      install would take over its uninstall entry and shortcuts).
  windows-exe: the same with the previous exe in a scratch folder, without install and uninstall.
`--first-framework-release`: the previous release cannot answer --check-update yet; the NEW release is installed
instead and must report its version and channel and uninstall cleanly. Copies of the old releases are updated by hand.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from redactor_common.core import crash_log, selfupdate, update_manifest, zip_rewrite
from redactor_common.core.update_manifest import MANIFEST_NAME, WINDOWS_EXE, WINDOWS_INSTALLER, WINDOWS_ZIP

DEFAULT_GATE_DIR = r"C:\Temp\Claude\release-gate"
INSTALL_WAIT = 600          # seconds an install or update may take
UNINSTALL_WAIT = 120
RUN_SETTLE = 8.0            # seconds a program started by the gate gets before it must still be running
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{app_id}_is1"


class ReleaseError(Exception):
    """A step that failed; the message says what to do."""


def contract_path() -> str:
    """installer/update-contract.iss, for ISCC /DUpdateContract=..."""
    return str(Path(__file__).resolve().parent / "installer" / "update-contract.iss")


# --- configuration ----------------------------------------------------------------------------------------------------


@dataclass
class Config:
    root: Path
    app: str
    name: str
    repo: str
    version_file: Path
    build: list[str]
    exe: str
    packages: dict[str, str]
    installer_app_id: str = ""
    zip_folder: str = ""
    zip_file: str = ""
    after_gate: list[list[str]] = field(default_factory=list)
    extra_assets: list[str] = field(default_factory=list)
    title: str = "{name} {version}"
    notes: str = ""
    gate_run: list[str] = field(default_factory=list)       # [gate] run: how the program is started to run (empty:
    gate_quit: list[str] = field(default_factory=list)      # the gate never runs it); quit: how it is asked to stop
    gate_crash_log: str = ""                                # crash log, relative to the scratch profile
    gate_live: float = 20.0                                 # seconds the new program must keep running

    @property
    def version(self) -> str:
        text = self.version_file.read_text(encoding="utf-8")
        m = re.search(r'APP_VERSION\s*=\s*"(\d{4}-\d{2}-\d{2}#\d{2,3})"', text)
        if not m:
            raise ReleaseError(f"No APP_VERSION = \"YYYY-MM-DD#NN\" in {self.version_file}")
        return m.group(1)

    @property
    def release(self) -> str:
        """RELEASE = "0.8" in version_file, for an app that names its releases by a number ("" when it has none)."""
        m = re.search(r'(?m)^RELEASE\s*=\s*"([^"]+)"', self.version_file.read_text(encoding="utf-8"))
        return m.group(1) if m else ""

    def fill(self, pattern: str) -> str:
        if "{release}" in pattern and not self.release:
            raise ReleaseError(f"{pattern!r} uses {{release}}, but {self.version_file} has no RELEASE = \"...\"")
        return pattern.format(name=self.name, version=self.version, build=self.build_name, release=self.release)

    @property
    def release_title(self) -> str:
        return self.fill(self.title)

    @property
    def build_name(self) -> str:
        return self.version.replace("#", "-")

    @property
    def tag(self) -> str:
        return "v" + self.build_name

    def path(self, pattern: str) -> Path:
        return self.root / pattern.format(build=self.build_name)

    def package_paths(self) -> dict[str, Path]:
        return {channel: self.path(pattern) for channel, pattern in self.packages.items()}


def load_config(root: Path) -> Config:
    try:
        import tomllib
    except ModuleNotFoundError:                     # Python 3.10: the backport, a dependency there (pyproject.toml)
        import tomli as tomllib

    root = Path(root).resolve()
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseError(f"Cannot read {root / 'pyproject.toml'}: {exc}") from exc
    raw = data.get("tool", {}).get("redactor-release")
    if not isinstance(raw, dict):
        raise ReleaseError(f"{root / 'pyproject.toml'} has no [tool.redactor-release] section")
    missing = [key for key in ("app", "name", "repo", "version_file", "build", "exe", "packages") if not raw.get(key)]
    if missing:
        raise ReleaseError(f"[tool.redactor-release] lacks {', '.join(missing)}")
    packages = dict(raw["packages"])
    for channel in packages:
        if channel not in update_manifest.CHANNELS:
            raise ReleaseError(f"[tool.redactor-release.packages] names an unknown channel {channel!r}")
    if raw["app"] != raw["repo"].split("/", 1)[-1].lower():
        raise ReleaseError(f"app {raw['app']!r} must be the repository's name in lower case ({raw['repo']})")
    if WINDOWS_INSTALLER in packages and not raw.get("installer_app_id"):
        raise ReleaseError("a windows-installer package needs installer_app_id (the Inno Setup AppId)")
    zip_cfg = raw.get("zip") or {}
    gate_cfg = raw.get("gate") or {}
    if bool(gate_cfg.get("run")) != bool(gate_cfg.get("quit")):
        raise ReleaseError("[tool.redactor-release.gate] needs both run and quit (how the program starts and stops)")
    return Config(root, raw["app"], raw["name"], raw["repo"], root / raw["version_file"], list(raw["build"]),
                  raw["exe"], packages, raw.get("installer_app_id", ""), zip_cfg.get("folder", ""),
                  zip_cfg.get("file", ""), [list(c) for c in raw.get("after_gate", [])],
                  list(raw.get("extra_assets", [])), raw.get("title") or "{name} {version}", raw.get("notes", ""),
                  list(gate_cfg.get("run", [])), list(gate_cfg.get("quit", [])), gate_cfg.get("crash_log", ""),
                  float(gate_cfg.get("live", 20.0)))


# --- running things ---------------------------------------------------------------------------------------------------


Runner = Callable[..., subprocess.CompletedProcess]


def run(argv: Sequence[str], cwd: Path | None = None, check: bool = True, env: dict | None = None,
        capture: bool = True) -> subprocess.CompletedProcess:
    # stdin closed: a build script's closing "pause" returns at once instead of waiting for a key
    done = subprocess.run(list(argv), cwd=cwd, env=env, capture_output=capture, text=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL)
    if check and done.returncode != 0:
        raise ReleaseError(f"{' '.join(map(str, argv))} failed (exit {done.returncode}):\n"
                           f"{(done.stdout or '')[-2000:]}{(done.stderr or '')[-2000:]}")
    return done


def say(text: str) -> None:
    print(text, flush=True)


def gh_json(cfg: Config, path: str, runner: Runner = run) -> dict:
    return json.loads(runner(["gh", "api", f"repos/{cfg.repo}/{path}"], cwd=cfg.root).stdout or "{}")


def release_exists(cfg: Config, tag: str, runner: Runner = run) -> dict | None:
    done = runner(["gh", "api", f"repos/{cfg.repo}/releases/tags/{tag}"], cwd=cfg.root, check=False)
    return json.loads(done.stdout) if done.returncode == 0 and done.stdout.strip() else None


# --- building ---------------------------------------------------------------------------------------------------------


def preflight(cfg: Config, runner: Runner = run) -> None:
    status = runner(["git", "status", "--porcelain"], cwd=cfg.root).stdout.strip()
    if status:
        raise ReleaseError(f"The working tree is not clean; commit first:\n{status}")
    branch = runner(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cfg.root).stdout.strip()
    if branch != "main":
        raise ReleaseError(f"On branch {branch!r}, not main.")
    runner(["git", "fetch", "origin", "main", "-q"], cwd=cfg.root)
    local = runner(["git", "rev-parse", "HEAD"], cwd=cfg.root).stdout.strip()
    remote = runner(["git", "rev-parse", "origin/main"], cwd=cfg.root).stdout.strip()
    if local != remote:
        raise ReleaseError(f"main ({local[:8]}) is not origin/main ({remote[:8]}): push or pull first.")
    found = release_exists(cfg, cfg.tag, runner)
    if found and not found.get("prerelease"):
        raise ReleaseError(f"Release {cfg.tag} already exists and is published. Bump the version first.")


def build_env(cfg: Config) -> dict:
    """The build runs with the repository's own .venv first on PATH when it has one, so its build script's "python"
    is that venv's (the family rule: never pip into the shared Python; a package installed there for something else
    would also end up in the exe)."""
    env = dict(os.environ)
    scripts = cfg.root / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
    if scripts.is_dir():
        env["PATH"] = str(scripts) + os.pathsep + env.get("PATH", "")
        env["VIRTUAL_ENV"] = str(scripts.parent)
    return env


def build_command(cfg: Config) -> list[str]:
    """`build` as run: a .bat/.cmd script named first is called by its absolute path (2026-11-07: "cmd /c call
    build_exe.bat" in the repository folder answered "not recognized", although `where` found it there)."""
    first, rest = cfg.build[0], cfg.build[1:]
    script = cfg.root / first
    if first.lower().endswith((".bat", ".cmd")) and script.is_file():
        return ["cmd", "/d", "/c", "call", str(script), *rest]
    return list(cfg.build)


def make_zip(cfg: Config) -> Path | None:
    """Zips the folder build (the folder itself as the zip's top level), after checking it declares windows-zip."""
    if not cfg.zip_folder:
        return None
    folder, target = cfg.path(cfg.zip_folder), cfg.path(cfg.zip_file)
    channel = update_manifest.read_channel(folder / cfg.exe, frozen=True)
    if channel != WINDOWS_ZIP:
        raise ReleaseError(f"{folder} does not declare channel {WINDOWS_ZIP} (its {update_manifest.CHANNEL_FILE} says "
                           f"{channel or 'nothing'}); the build must write it.")
    target.unlink(missing_ok=True)
    zip_rewrite.write_new_archive(str(target), (
        zip_rewrite.NewEntry((Path(folder.name) / path.relative_to(folder)).as_posix(), source_path=str(path),
                             compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        for path in sorted(folder.rglob("*")) if path.is_file()))
    return target


def write_manifest(cfg: Config) -> Path:
    data = update_manifest.build(cfg.app, cfg.tag, cfg.version, cfg.package_paths())
    target = cfg.root / "dist" / MANIFEST_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    return update_manifest.write(target, data)


def release_notes(cfg: Config) -> str:
    """The `notes` file when the app names one (it must exist), else this version's section of CHANGELOG.md (its
    "## <version>" heading up to the next "## ")."""
    if cfg.notes:
        path = cfg.root / cfg.fill(cfg.notes)
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            raise ReleaseError(f"There are no release notes: write {path} first.") from None
    try:
        lines = (cfg.root / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    out, inside = [], False
    for line in lines:
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].split(" ", 1)[0] == cfg.version
            continue
        if inside:
            out.append(line)
    return "\n".join(out).strip()


def assets_of(cfg: Config) -> list[Path]:
    paths = list(cfg.package_paths().values()) + [cfg.root / "dist" / MANIFEST_NAME]
    paths += [cfg.path(extra) for extra in cfg.extra_assets]
    return list(dict.fromkeys(paths))


# --- checking a published release -------------------------------------------------------------------------------------


def verify_release(cfg: Config, tag: str, scratch: Path, runner: Runner = run) -> update_manifest.Manifest:
    """Every package the release's manifest names is an asset with the manifest's size and digest, on GitHub and when
    downloaded back. Returns the manifest."""
    data = gh_json(cfg, f"releases/tags/{tag}", runner)
    assets = selfupdate._assets(data)
    entry = assets.get(MANIFEST_NAME)
    if entry is None or not entry["sha256"]:
        raise ReleaseError(f"Release {tag} has no {MANIFEST_NAME} with a digest.")
    folder = scratch / "downloaded"
    folder.mkdir(parents=True, exist_ok=True)
    runner(["gh", "release", "download", tag, "--repo", cfg.repo, "--pattern", MANIFEST_NAME, "--dir", str(folder),
            "--clobber"], cwd=cfg.root)
    raw = (folder / MANIFEST_NAME).read_bytes()
    if update_manifest.file_sha256(folder / MANIFEST_NAME) != entry["sha256"]:
        raise ReleaseError(f"{MANIFEST_NAME} downloaded from {tag} does not match GitHub's digest.")
    manifest = update_manifest.parse(raw)
    if (manifest.app, manifest.tag) != (cfg.app, tag):
        raise ReleaseError(f"{tag}'s manifest is for {manifest.app} {manifest.tag}.")
    for channel, package in manifest.packages.items():
        held = assets.get(package.file)
        if held is None or (held["size"], held["sha256"]) != (package.size, package.sha256):
            raise ReleaseError(f"{tag}: {package.file} ({channel}) on GitHub does not match the manifest.")
        runner(["gh", "release", "download", tag, "--repo", cfg.repo, "--pattern", package.file, "--dir", str(folder),
                "--clobber"], cwd=cfg.root)
        if update_manifest.file_sha256(folder / package.file) != package.sha256:
            raise ReleaseError(f"{tag}: {package.file} downloaded back does not match the manifest.")
    say(f"Verified {tag}: {MANIFEST_NAME} and {', '.join(p.file for p in manifest.packages.values())}")
    return manifest


# --- the gate ---------------------------------------------------------------------------------------------------------


START_MARKER = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d started: pid \d+.*$")


def crash_entries(text: str) -> str:
    """What a crash log holds beyond core.crash_log's start markers (the separator and "<time> started: pid ..."
    that every launch writes): a traceback or a faulthandler dump. "" when the program only started."""
    return "\n".join(line for line in text.splitlines()
                     if line.strip() and line.strip() != crash_log.ENTRY_SEPARATOR
                     and not START_MARKER.match(line.strip())).strip()


def wait_for_pid(pid: int, timeout: float) -> int | None:
    """The exit code of process `pid` once it ends; None when it still runs after `timeout` (or cannot be watched)."""
    if sys.platform != "win32":
        raise ReleaseError("The gate runs on Windows.")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)        # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
    if not handle:
        return 0                                                            # already gone
    try:
        if kernel32.WaitForSingleObject(handle, int(timeout * 1000)) != 0:
            return None
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return int(code.value)
    finally:
        kernel32.CloseHandle(handle)


class Gate:
    def __init__(self, cfg: Config, tag: str, scratch: Path, runner: Runner = run):
        self.cfg, self.tag, self.scratch, self.runner = cfg, tag, scratch, runner
        self.profile = scratch / "profile"
        self.started: list[subprocess.Popen] = []
        self.env = dict(os.environ)
        for key, sub in (("REDACTOR_CONFIG_DIR", "profile"), ("APPDATA", "appdata"), ("TEMP", "temp"),
                         ("TMP", "temp")):
            (scratch / sub).mkdir(parents=True, exist_ok=True)
            self.env[key] = str(scratch / sub)

    # the program's own answers
    def ask(self, exe: Path, *args: str) -> dict | str:
        result = self.scratch / "answers" / f"{int(time.time() * 1000)}.json"
        done = self.runner([str(exe), *args, "--result-file", str(result)], cwd=exe.parent, env=self.env, check=False)
        try:
            text = result.read_text(encoding="utf-8").strip()
        except OSError:
            raise ReleaseError(f"{exe.name} {' '.join(args)} gave no answer (exit {done.returncode}); this release "
                               "of the program does not speak the update framework.") from None
        return text if "--version" in args else json.loads(text)

    def expect_version(self, exe: Path, version: str) -> None:
        got = self.ask(exe, "--version")
        if got != version:
            raise ReleaseError(f"{exe} reports version {got!r}, expected {version!r}.")
        say(f"  {exe.name} --version: {got}")

    def expect_channel(self, folder: Path, channel: str) -> None:
        """The channel the copy in `folder` declares: its update-channel file, or, for a one-file exe that carries its
        channel inside (a bundled file the gate cannot see), what the program itself says (--check-update)."""
        got = update_manifest.read_channel(folder / self.cfg.exe, frozen=True)
        if not got:
            answer = self.ask(folder / self.cfg.exe, "--check-update", "--release-tag", self.tag)
            got = answer.get("channel", "") if isinstance(answer, dict) else ""
        if got != channel:
            raise ReleaseError(f"{folder} declares channel {got or 'nothing'!r}, expected {channel!r}.")
        say(f"  channel: {got}")

    def expect_update(self, exe: Path, new: update_manifest.Manifest, channel: str) -> dict:
        check = self.ask(exe, "--check-update", "--release-tag", self.tag)
        package = new.packages[channel]
        if check.get("action") != selfupdate.INSTALL or check.get("file") != package.file:
            raise ReleaseError(f"--check-update said {check}, expected install of {package.file}.")
        say(f"  --check-update: install {package.file}")
        done = self.ask(exe, "--update", "--release-tag", self.tag)
        if done.get("action") not in ("started", "installed"):
            raise ReleaseError(f"--update said {done}.")
        say(f"  --update: {done['action']}")
        return done

    # the installer channel
    def guard_install(self) -> None:
        location = installed_location(self.cfg.installer_app_id)
        if location is None:
            return
        gate_root = Path(os.environ.get("REDACTOR_GATE_DIR") or DEFAULT_GATE_DIR)
        if gate_root in Path(location).parents:
            remove_leftover_install(self.cfg, Path(location), lambda target: self.uninstall(target))
            return
        raise ReleaseError(f"{self.cfg.name} is installed on this PC ({location}). The gate's scratch install would take "
                           "over its uninstall entry and shortcuts: uninstall it first, or run the gate on another PC.")

    def install(self, setup: Path) -> Path:
        target = self.scratch / "program"
        log = self.scratch / "install.log"
        done = self.runner([str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/DIR={target}", "/TASKS=",
                            f"/LOG={log}"], env=self.env, check=False)
        if done.returncode != 0:
            raise ReleaseError(f"{setup.name} failed to install (exit {done.returncode}); see {log}.")
        say(f"  installed {setup.name} into {target}")
        return target

    def uninstall(self, target: Path) -> None:
        uninstaller = target / "unins000.exe"
        if not uninstaller.is_file():
            raise ReleaseError(f"No uninstaller in {target}.")
        self.runner([str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], env=self.env, check=False)
        deadline = time.monotonic() + UNINSTALL_WAIT     # it hands over to a copy of itself and returns at once
        while time.monotonic() < deadline:
            if installed_location(self.cfg.installer_app_id) is None and not uninstaller.exists():
                break
            time.sleep(1)
        else:
            raise ReleaseError(f"The uninstaller did not finish within {UNINSTALL_WAIT} seconds.")
        time.sleep(2)
        left = [str(p.relative_to(target)) for p in target.rglob("*")] if target.exists() else []
        if left:
            raise ReleaseError(f"Uninstalling left files behind in {target}: {', '.join(left[:10])}")
        say("  uninstalled; nothing left in the program folder")

    def run_installer(self, new: update_manifest.Manifest, previous_setup: Path | None, previous_version: str,
                      new_setup: Path) -> None:
        self.guard_install()
        exe_of = lambda folder: folder / self.cfg.exe  # noqa: E731
        if previous_setup is None:
            target = self.install(new_setup)
            self.expect_version(exe_of(target), new.version)
            self.expect_channel(target, WINDOWS_INSTALLER)
            check = self.ask(exe_of(target), "--check-update", "--release-tag", self.tag)
            if check.get("action") != selfupdate.NONE:
                raise ReleaseError(f"The new release asked about itself said {check}, expected none.")
            say("  --check-update on itself: none")
        else:
            target = self.install(previous_setup)
            self.expect_version(exe_of(target), previous_version)
            self.expect_channel(target, WINDOWS_INSTALLER)
            running = self.start_running(exe_of(target))        # 81A: the update happens over a running copy
            done = self.expect_update(exe_of(target), new, WINDOWS_INSTALLER)
            code = wait_for_pid(int(done.get("pid") or 0), INSTALL_WAIT)
            if code is None:
                raise ReleaseError(f"The installer started by --update was still running after {INSTALL_WAIT} s.")
            if code != 0:
                raise ReleaseError(f"The installer started by --update failed (exit {code}).")
            self.expect_stopped_by_itself(running, done.get("log", ""))
            self.expect_version(exe_of(target), new.version)
            self.expect_channel(target, WINDOWS_INSTALLER)
        self.expect_it_runs(exe_of(target))                     # 81A: the new program starts and keeps running
        self.uninstall(target)

    # running the program (81A, 2026-11-07: an update over a running Accipio closed it and aria2 by force, and the
    # first start after it crashed; the gate had never run the program)

    def start_running(self, exe: Path) -> subprocess.Popen | None:
        """The program started the way a user runs it ([gate] run), in the scratch profile; None when not set."""
        if not self.cfg.gate_run:
            return None
        process = subprocess.Popen([str(exe), *self.cfg.gate_run], cwd=exe.parent, env=self.env)
        self.started.append(process)
        time.sleep(RUN_SETTLE)
        if process.poll() is not None:
            raise ReleaseError(f"{exe.name} {' '.join(self.cfg.gate_run)} stopped right after starting "
                               f"(exit {process.returncode}).")
        say(f"  running: {exe.name} {' '.join(self.cfg.gate_run)}")
        return process

    def stop_started(self) -> None:
        """A failed gate stops what it started, with the programs those started (Accipio's aria2): left running,
        they held the scratch install (2026-11-08)."""
        for process in self.started:
            if process.poll() is not None:
                continue
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True)
            else:
                process.kill()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                pass

    def expect_stopped_by_itself(self, running: subprocess.Popen | None, log: str) -> None:
        """The update stopped the running program the way it asked (exit 0), not Windows by force."""
        if running is None:
            return
        try:
            code = running.wait(30)
        except subprocess.TimeoutExpired:
            running.kill()
            raise ReleaseError("The program was still running after the update.") from None
        if code != 0:
            raise ReleaseError(f"The update closed the running program by force (exit {code}), not by asking it.")
        say("  the running copy stopped by itself for the update")
        if not log:
            say("  (that release's --update keeps no installer log: from redactor_common 2026-11-07-09 it does)")
            return
        try:
            text = Path(log).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            raise ReleaseError(f"The installer log {log} is missing.") from None
        for sign in ("RestartManager found an application", "still running after"):
            if sign in text:
                line = next(l for l in text.splitlines() if sign in l)
                raise ReleaseError(f"The installer had to close a program by force: {line.strip()}")
        say("  installer log: no program had to be closed by force")

    def expect_it_runs(self, exe: Path) -> None:
        """The program starts, is still running after [gate] live seconds with no crash log, and stops when asked."""
        if not self.cfg.gate_run:
            return
        crash = self.profile / self.cfg.gate_crash_log if self.cfg.gate_crash_log else None
        if crash is not None and crash.exists():
            crash.unlink()
        process = subprocess.Popen([str(exe), *self.cfg.gate_run], cwd=exe.parent, env=self.env)
        self.started.append(process)
        time.sleep(self.cfg.gate_live)
        alive = process.poll() is None
        crashed = crash is not None and crash.exists() and crash_entries(crash.read_text(encoding="utf-8",
                                                                                          errors="replace"))
        stopped = self.runner([str(exe), *self.cfg.gate_quit], cwd=exe.parent, env=self.env, check=False)
        try:
            code = process.wait(30)
        except subprocess.TimeoutExpired:
            process.kill()
            raise ReleaseError(f"{exe.name} did not stop when asked ({' '.join(self.cfg.gate_quit)}).") from None
        if not alive:
            raise ReleaseError(f"{exe.name} stopped by itself within {self.cfg.gate_live:.0f} s (exit {code}).")
        if crashed:
            raise ReleaseError(f"{exe.name} wrote a crash log on its first start:\n" + crashed[-3000:])
        if stopped.returncode != 0 or code != 0:
            raise ReleaseError(f"{exe.name} did not stop cleanly (quit exit {stopped.returncode}, program exit {code}).")
        say(f"  {exe.name} ran {self.cfg.gate_live:.0f} s with no crash and stopped when asked")

    def run_exe(self, new: update_manifest.Manifest, previous_exe: Path | None, previous_version: str,
                new_exe: Path) -> None:
        target = self.scratch / "program"
        target.mkdir(parents=True, exist_ok=True)
        exe = target / self.cfg.exe
        shutil.copy2(previous_exe or new_exe, exe)
        if previous_exe is None:
            self.expect_version(exe, new.version)
            self.expect_channel(target, WINDOWS_EXE)
            return
        self.expect_version(exe, previous_version)
        self.expect_channel(target, WINDOWS_EXE)
        self.expect_update(exe, new, WINDOWS_EXE)
        self.expect_version(exe, new.version)


def installed_location(app_id: str) -> str | None:
    """The InstallLocation of the app's Inno Setup uninstall entry for this user, or None when it has none."""
    if sys.platform != "win32" or not app_id:
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY.format(app_id=app_id)) as key:
            return str(winreg.QueryValueEx(key, "InstallLocation")[0]).rstrip("\\")
    except OSError:
        return None


def previous_release(cfg: Config, tag: str, runner: Runner = run) -> str:
    latest = gh_json(cfg, "releases/latest", runner).get("tag_name", "")
    if not latest or latest == tag:
        raise ReleaseError(f"No previous published release to update from (Latest is {latest or 'none'}); "
                           "pass --previous TAG.")
    return latest


def remove_leftover_install(cfg: Config, location: Path, uninstall: Callable[[Path], None]) -> None:
    """A scratch install a failed gate left: uninstalled with its own uninstaller; when its folder is already gone
    (an older gate wiped the scratch first), its uninstall entry and Start menu shortcuts are removed by hand."""
    say(f"  removing a scratch install a failed gate left in {location}")
    if (location / "unins000.exe").is_file():
        uninstall(location)
        return
    import winreg
    key = UNINSTALL_KEY.format(app_id=cfg.installer_app_id)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as entry:
            group = winreg.QueryValueEx(entry, "Inno Setup: Icon Group")[0]
    except OSError:
        group = ""
    programs = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    shortcut = programs / f"{group}.lnk"
    if group and shortcut.is_file():
        held = shortcut.read_bytes()
        if any(str(location).encode(code) in held for code in ("utf-16-le", "mbcs")):    # only one into the scratch
            shortcut.unlink()
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
    except OSError:
        pass
    if installed_location(cfg.installer_app_id) is not None:
        raise ReleaseError(f"The uninstall entry of the scratch install in {location} could not be removed.")


def gate(cfg: Config, tag: str, previous: str = "", first: bool = False, runner: Runner = run) -> None:
    scratch_root = Path(os.environ.get("REDACTOR_GATE_DIR") or DEFAULT_GATE_DIR)
    scratch = scratch_root / f"{cfg.app}-{tag}"
    leftover = installed_location(cfg.installer_app_id) if cfg.installer_app_id else None
    if leftover is not None and scratch_root in Path(leftover).parents:     # before its folder is wiped below
        remove_leftover_install(cfg, Path(leftover),
                                lambda target: Gate(cfg, tag, scratch_root / "leftover", runner).uninstall(target))
        shutil.rmtree(scratch_root / "leftover", ignore_errors=True)
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    say(f"Gate for {cfg.name} {tag} in {scratch}")
    ok = False
    gate_run = None
    try:
        new = verify_release(cfg, tag, scratch / "new", runner)
        old_tag, old = "", None
        if not first:
            old_tag = previous or previous_release(cfg, tag, runner)
            say(f"Previous release: {old_tag}")
            try:
                old = verify_release(cfg, old_tag, scratch / "old", runner)
            except ReleaseError as exc:
                raise ReleaseError(f"{exc}\nThe previous release is not on the update framework; for this app's "
                                   "first framework release pass --first-framework-release.") from None
        for channel in (WINDOWS_INSTALLER, WINDOWS_EXE):
            if channel not in new.packages:
                continue
            say(f"Channel {channel}:")
            new_file = scratch / "new" / "downloaded" / new.packages[channel].file
            old_file = None
            if old is not None:
                if channel not in old.packages:
                    raise ReleaseError(f"{old_tag} has no {channel} package to update from.")
                old_file = scratch / "old" / "downloaded" / old.packages[channel].file
            gate_run = Gate(cfg, tag, scratch / channel, runner)
            if channel == WINDOWS_INSTALLER:
                gate_run.run_installer(new, old_file, old.version if old else "", new_file)
            else:
                gate_run.run_exe(new, old_file, old.version if old else "", new_file)
        ok = True
        say(f"Gate passed for {tag}.")
    finally:
        if gate_run is not None and not ok:
            gate_run.stop_started()
        for command in cfg.after_gate:
            runner(command, cwd=cfg.root, check=False)
        if ok:
            shutil.rmtree(scratch, ignore_errors=True)
        else:
            say(f"Gate FAILED; scratch kept for a look: {scratch}")


def promote(cfg: Config, tag: str, runner: Runner = run) -> None:
    runner(["gh", "release", "edit", tag, "--repo", cfg.repo, "--prerelease=false", "--latest"], cwd=cfg.root)
    say(f"{tag} is now the Latest release.")


def publish(cfg: Config, skip_build: bool = False, previous: str = "", first: bool = False,
            runner: Runner = run, promote_after: bool = True) -> None:
    """`promote_after` False: the gated release stays a pre-release (a stepping stone another release is gated
    from, such as an app's first framework release)."""
    preflight(cfg, runner)
    tag = cfg.tag
    notes_text = release_notes(cfg)                 # a missing notes file stops here, before anything is built or tagged
    say(f"Releasing {cfg.release_title} as {tag}")
    if not skip_build:
        say("Building...")
        runner(build_command(cfg), cwd=cfg.root, capture=False, env=build_env(cfg))
    make_zip(cfg)
    write_manifest(cfg)
    assets = assets_of(cfg)
    for path in assets:
        if not path.is_file():
            raise ReleaseError(f"Missing release file: {path}")
    if not runner(["git", "tag", "-l", tag], cwd=cfg.root).stdout.strip():
        runner(["git", "tag", tag], cwd=cfg.root)
    if runner(["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"], cwd=cfg.root).stdout.strip() == "":
        runner(["git", "push", "origin", f"refs/tags/{tag}"], cwd=cfg.root)
    notes = cfg.root / "dist" / "release-notes.md"
    notes.write_text(notes_text or f"{cfg.name} {cfg.version}", encoding="utf-8")
    if release_exists(cfg, tag, runner) is None:
        runner(["gh", "release", "create", tag, "--repo", cfg.repo, "--prerelease", "--title",
                cfg.release_title, "--notes-file", str(notes), "--verify-tag", *map(str, assets)],
               cwd=cfg.root)
    else:
        runner(["gh", "release", "upload", tag, "--repo", cfg.repo, "--clobber", *map(str, assets)], cwd=cfg.root)
    say(f"Published {tag} as a pre-release.")
    gate(cfg, tag, previous, first, runner)
    if promote_after:
        promote(cfg, tag, runner)
    else:
        say(f"{tag} passed the gate and stays a pre-release (--no-promote).")


DETACHED_ENV = "REDACTOR_RELEASE_DETACHED"
FINISHED = "release finished: exit {code}"      # the last line of a detached run's log


def detach(argv: Sequence[str], log: Path, popen: Callable = subprocess.Popen) -> int:
    """Run this command again as a process of its own and return at once. A release started from a session that
    ends (a terminal closed, an agent's shell torn down) died with it halfway through the build on 2026-11-07; the
    detached run is not its child, and its log ends with FINISHED so a watcher knows when and how it ended."""
    log.parent.mkdir(parents=True, exist_ok=True)
    flags = 0
    if sys.platform == "win32":
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                 | subprocess.CREATE_NO_WINDOW)
    env = {**os.environ, DETACHED_ENV: "1", "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    command = [sys.executable, "-m", "redactor_common.release", *argv]
    with open(log, "w", encoding="utf-8") as out:
        try:
            child = popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env,
                          creationflags=flags | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0))
        except OSError:         # a job that forbids breaking away: still a process of its own, just inside the job
            child = popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env,
                          creationflags=flags)
    print(f"Release running on its own (pid {child.pid}); its log: {log}")
    print(f"It ends with the line \"{FINISHED.format(code='N')}\" (0 = done).")
    return 0


def default_log(repo: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return Path(DEFAULT_GATE_DIR).parent / "release-logs" / f"{Path(repo).resolve().name}-{stamp}.log"


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # taken out, not just read: the build calls this tool again (contract-path), and a child that also printed
    # the end line put it into the installer script (2026-11-08)
    if not os.environ.pop(DETACHED_ENV, None):
        return run_command(argv)
    try:
        code = run_command(argv)
    except Exception:           # nobody watches a detached run's stderr: the traceback goes to its log, then the end line
        traceback.print_exc()
        code = 1
    say(FINISHED.format(code=code))
    return code


def run_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m redactor_common.release")
    parser.add_argument("--repo", default=".", help="the app's repository (default: here)")
    parser.add_argument("--detach", action="store_true",
                        help="run as a process of its own that outlives this terminal; prints where its log is")
    parser.add_argument("--log", default="", help="with --detach: the log file (default: under C:\\Temp\\Claude)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("contract-path")
    sub.add_parser("manifest")
    p = sub.add_parser("publish")
    p.add_argument("--skip-build", action="store_true")
    p.add_argument("--no-promote", action="store_true", help="leave the gated release a pre-release")
    for name in ("publish", "gate"):
        q = p if name == "publish" else sub.add_parser("gate")
        if name == "gate":
            q.add_argument("--tag", required=True)
        group = q.add_mutually_exclusive_group()
        group.add_argument("--previous", default="")
        group.add_argument("--first-framework-release", action="store_true")
    sub.add_parser("promote").add_argument("--tag", required=True)
    args = parser.parse_args(argv)
    if args.detach:
        rest = [a for i, a in enumerate(argv)
                if a != "--detach" and not a.startswith("--log=") and a != "--log"
                and not (i > 0 and argv[i - 1] == "--log")]
        return detach(rest, Path(args.log) if args.log else default_log(args.repo))
    if args.command == "contract-path":
        print(contract_path())
        return 0
    try:
        cfg = load_config(Path(args.repo))
        if args.command == "manifest":
            make_zip(cfg)
            print(write_manifest(cfg))
        elif args.command == "publish":
            publish(cfg, args.skip_build, args.previous, args.first_framework_release,
                    promote_after=not args.no_promote)
        elif args.command == "gate":
            gate(cfg, args.tag, args.previous, args.first_framework_release)
        elif args.command == "promote":
            promote(cfg, args.tag)
    except (ReleaseError, ValueError) as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
redactor_common/release.py: the family's release tool and its release gate (docs/update-framework.md, section 6).

    python -m redactor_common.release publish [--skip-build] [--previous vX | --first-framework-release]
    python -m redactor_common.release gate --tag vX [--previous vY | --first-framework-release]
    python -m redactor_common.release promote --tag vX
    python -m redactor_common.release manifest            (build the manifest from dist\\ only)
    python -m redactor_common.release contract-path       (where installer/update-contract.iss is, for the build)

Run from the app's repository (or pass --repo PATH). The app describes itself in its pyproject.toml:

    [tool.redactor-release]
    app = "accipio"                                   # the manifest's app: the GitHub repository's name, lower case
    name = "Accipio"
    repo = "Erlbon/accipio"
    version_file = "accipio/core/version.py"          # holds APP_VERSION = "YYYY-MM-DD#NN"
    build = ["cmd", "/c", "call build_exe.bat < nul"]
    exe = "Accipio.exe"                               # the program inside an install or the exe package itself
    installer_app_id = "{6C0E5D8A-...}"               # windows-installer only: its Inno Setup AppId
    after_gate = [["python", "-m", "accipio", "--register"]]     # optional: put back what an install changed
    [tool.redactor-release.zip]                       # optional: zip a folder build ("{build}" = 2026-11-07-02)
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
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from redactor_common.core import selfupdate, update_manifest, zip_rewrite
from redactor_common.core.update_manifest import MANIFEST_NAME, WINDOWS_EXE, WINDOWS_INSTALLER, WINDOWS_ZIP

DEFAULT_GATE_DIR = r"C:\Temp\Claude\release-gate"
INSTALL_WAIT = 600          # seconds an install or update may take
UNINSTALL_WAIT = 120
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

    @property
    def version(self) -> str:
        text = self.version_file.read_text(encoding="utf-8")
        m = re.search(r'APP_VERSION\s*=\s*"(\d{4}-\d{2}-\d{2}#\d{2,3})"', text)
        if not m:
            raise ReleaseError(f"No APP_VERSION = \"YYYY-MM-DD#NN\" in {self.version_file}")
        return m.group(1)

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
    import tomllib

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
    return Config(root, raw["app"], raw["name"], raw["repo"], root / raw["version_file"], list(raw["build"]),
                  raw["exe"], packages, raw.get("installer_app_id", ""), zip_cfg.get("folder", ""),
                  zip_cfg.get("file", ""), [list(c) for c in raw.get("after_gate", [])],
                  list(raw.get("extra_assets", [])))


# --- running things ---------------------------------------------------------------------------------------------------


Runner = Callable[..., subprocess.CompletedProcess]


def run(argv: Sequence[str], cwd: Path | None = None, check: bool = True, env: dict | None = None,
        capture: bool = True) -> subprocess.CompletedProcess:
    done = subprocess.run(list(argv), cwd=cwd, env=env, capture_output=capture, text=True, encoding="utf-8",
                          errors="replace")
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
    """This version's section of CHANGELOG.md (its "## <version>" heading up to the next "## ")."""
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
        got = update_manifest.read_channel(folder / self.cfg.exe, frozen=True)
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
            say(f"  removing a scratch install a failed gate left in {location}")
            self.uninstall(Path(location))
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
            done = self.expect_update(exe_of(target), new, WINDOWS_INSTALLER)
            code = wait_for_pid(int(done.get("pid") or 0), INSTALL_WAIT)
            if code is None:
                raise ReleaseError(f"The installer started by --update was still running after {INSTALL_WAIT} s.")
            if code != 0:
                raise ReleaseError(f"The installer started by --update failed (exit {code}).")
            self.expect_version(exe_of(target), new.version)
            self.expect_channel(target, WINDOWS_INSTALLER)
        self.uninstall(target)

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


def gate(cfg: Config, tag: str, previous: str = "", first: bool = False, runner: Runner = run) -> None:
    scratch_root = Path(os.environ.get("REDACTOR_GATE_DIR") or DEFAULT_GATE_DIR)
    scratch = scratch_root / f"{cfg.app}-{tag}"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    say(f"Gate for {cfg.name} {tag} in {scratch}")
    ok = False
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
            runner: Runner = run) -> None:
    preflight(cfg, runner)
    tag = cfg.tag
    say(f"Releasing {cfg.name} {cfg.version} as {tag}")
    if not skip_build:
        say("Building...")
        runner(cfg.build, cwd=cfg.root, capture=False)
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
    notes.write_text(release_notes(cfg) or f"{cfg.name} {cfg.version}", encoding="utf-8")
    if release_exists(cfg, tag, runner) is None:
        runner(["gh", "release", "create", tag, "--repo", cfg.repo, "--prerelease", "--title",
                f"{cfg.name} {cfg.version}", "--notes-file", str(notes), "--verify-tag", *map(str, assets)],
               cwd=cfg.root)
    else:
        runner(["gh", "release", "upload", tag, "--repo", cfg.repo, "--clobber", *map(str, assets)], cwd=cfg.root)
    say(f"Published {tag} as a pre-release.")
    gate(cfg, tag, previous, first, runner)
    promote(cfg, tag, runner)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m redactor_common.release")
    parser.add_argument("--repo", default=".", help="the app's repository (default: here)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("contract-path")
    sub.add_parser("manifest")
    p = sub.add_parser("publish")
    p.add_argument("--skip-build", action="store_true")
    for name in ("publish", "gate"):
        q = p if name == "publish" else sub.add_parser("gate")
        if name == "gate":
            q.add_argument("--tag", required=True)
        group = q.add_mutually_exclusive_group()
        group.add_argument("--previous", default="")
        group.add_argument("--first-framework-release", action="store_true")
    sub.add_parser("promote").add_argument("--tag", required=True)
    args = parser.parse_args(argv)
    if args.command == "contract-path":
        print(contract_path())
        return 0
    try:
        cfg = load_config(Path(args.repo))
        if args.command == "manifest":
            make_zip(cfg)
            print(write_manifest(cfg))
        elif args.command == "publish":
            publish(cfg, args.skip_build, args.previous, args.first_framework_release)
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

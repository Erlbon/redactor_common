"""installer/update-contract.iss: the family's installer contract, compiled with the real Inno Setup compiler.

Compiling is always tested when ISCC is installed. Running the built installer (it really installs into tmp_path, with
an uninstall entry under HKCU for the moment, then uninstalls) only with REDACTOR_INSTALLER_TESTS=1, since it touches
the PC beyond the test folder."""

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from redactor_common import release

CONTRACT = Path(release.contract_path())
APP_ID = "{{7D2B1C55-0B9E-4C49-A0D7-5E1F00C0FFEE}"

SCRIPT = """
#define UpdateExe "Fake.exe"
#define UpdateQuitArgs "--quit"
#define UpdateQuitCheck "KnowsQuit"
[Setup]
AppId=""" + APP_ID + """
AppName=Update Contract Test
AppVersion=1.0
PrivilegesRequired=lowest
DefaultDirName={autopf}\\UpdateContractTest
DisableProgramGroupPage=yes
OutputDir=out
OutputBaseFilename=contract-test-setup
Uninstallable=yes
CreateUninstallRegKey=yes

[Files]
Source: "Fake.exe"; DestDir: "{app}"
Source: "update-channel"; DestDir: "{app}"

[Code]
function KnowsQuit: Boolean;
begin
  Log('MAIN KnowsQuit');
  Result := True;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Log('MAIN PrepareToInstall');
  Result := '';
end;

#include UpdateContract
"""


def iscc():
    for root in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles(x86)", ""),
                 os.environ.get("ProgramFiles", "")):
        path = Path(root) / ("Programs" if root == os.environ.get("LOCALAPPDATA") else "") / "Inno Setup 6" / "ISCC.exe"
        if root and path.is_file():
            return path
    return None


needs_iscc = pytest.mark.skipif(sys.platform != "win32" or iscc() is None, reason="needs Inno Setup 6 (ISCC)")


def build(tmp_path):
    (tmp_path / "Fake.exe").write_bytes(b"MZ not really a program")
    (tmp_path / "update-channel").write_text("windows-zip\n", encoding="utf-8")   # what the folder build carries
    (tmp_path / "test.iss").write_text(SCRIPT, encoding="utf-8")
    done = subprocess.run([str(iscc()), "/Q", f"/DUpdateContract={CONTRACT}", str(tmp_path / "test.iss")],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    return tmp_path / "out" / "contract-test-setup.exe"


def test_the_contract_ships_with_the_package():
    assert CONTRACT.is_file() and CONTRACT.name == "update-contract.iss"
    text = CONTRACT.read_text(encoding="utf-8")
    assert "{param:relaunch|no}" in text and "update-channel" in text and "windows-installer" in text


@needs_iscc
def test_the_contract_compiles_next_to_the_apps_own_event_functions(tmp_path):
    assert build(tmp_path).is_file()


@needs_iscc
@pytest.mark.skipif(os.environ.get("REDACTOR_INSTALLER_TESTS") != "1", reason="set REDACTOR_INSTALLER_TESTS=1")
def test_an_update_declares_its_channel_asks_the_program_to_stop_and_waits_for_it(tmp_path):
    setup = build(tmp_path)
    target = tmp_path / "installed"

    def install(log):
        return subprocess.run([str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/DIR={target}",
                               f"/LOG={log}"]).returncode

    def read(log):
        return log.read_text(encoding="utf-8-sig", errors="replace")

    try:
        assert install(tmp_path / "first.log") == 0
        assert (target / "update-channel").read_text(encoding="utf-8").strip() == "windows-installer"
        assert "Update contract" not in read(tmp_path / "first.log")       # nothing to stop on a first install

        holder = open(target / "Fake.exe", "rb")          # "the program is still running" for three seconds
        threading.Timer(3.0, holder.close).start()
        assert install(tmp_path / "update.log") == 0
        text = read(tmp_path / "update.log")
        assert text.index("MAIN KnowsQuit") < text.index("asking the program to stop") < text.index("MAIN PrepareToInstall")
        waited = int(re.search(r"waited (\d+) ms", text).group(1))
        assert 250 <= waited < 30000                         # the installer's own start used part of the 3 s
        assert (target / "update-channel").read_text(encoding="utf-8").strip() == "windows-installer"
    finally:
        uninstaller = target / "unins000.exe"
        if uninstaller.is_file():
            subprocess.run([str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])
            deadline = time.monotonic() + 30          # the uninstaller hands over to a copy of itself and returns
            while (target / "Fake.exe").exists() and time.monotonic() < deadline:
                time.sleep(0.5)
        shutil.rmtree(target, ignore_errors=True)
    assert not (target / "Fake.exe").exists()

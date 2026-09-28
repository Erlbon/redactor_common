"""Tests for core/app_paths.py: where a frozen build keeps its settings
on each platform (simulated by patching sys.frozen/executable/platform)."""

import sys
from pathlib import Path

from redactor_common.core import app_paths


def _frozen(monkeypatch, platform, executable):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(sys, "platform", platform)


def test_from_source_uses_the_project_root(tmp_path):
    assert app_paths.base_dir(tmp_path) == tmp_path.resolve()
    assert app_paths.tools_dir(tmp_path) == tmp_path.resolve() / "tools"


def test_frozen_windows_stays_portable(monkeypatch, tmp_path):
    _frozen(monkeypatch, "win32", tmp_path / "app" / "cbzredactor.exe")
    assert app_paths.base_dir("unused") == tmp_path / "app"
    assert app_paths.settings_ini_path("cbzredactor", "unused") == tmp_path / "app" / "cbzredactor_settings.ini"


def test_frozen_linux_uses_xdg_config(monkeypatch, tmp_path):
    _frozen(monkeypatch, "linux", Path("/usr/local/bin/cbzredactor"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert app_paths.base_dir("unused") == tmp_path / "config" / "cbzredactor"
    assert (tmp_path / "config" / "cbzredactor").is_dir()
    # Bundled tools still sit beside the executable.
    assert app_paths.tools_dir("unused") == Path("/usr/local/bin/tools")


def test_frozen_linux_without_xdg_uses_dot_config(monkeypatch, tmp_path):
    _frozen(monkeypatch, "linux", tmp_path / "cbzredactor")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    assert app_paths.base_dir("unused") == tmp_path / "home" / ".config" / "cbzredactor"


def test_frozen_mac_uses_application_support(monkeypatch, tmp_path):
    _frozen(monkeypatch, "darwin", tmp_path / "cbzredactor")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    assert app_paths.base_dir("unused") == tmp_path / "home" / "Library" / "Application Support" / "cbzredactor"


def test_no_window_kwargs_without_the_windows_flag(monkeypatch):
    import subprocess

    from redactor_common.core.subprocess_utils import no_window_kwargs

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delattr(subprocess, "CREATE_NO_WINDOW", raising=False)
    assert no_window_kwargs() == {}
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    assert no_window_kwargs() == {"creationflags": 0x08000000}
    monkeypatch.setattr(sys, "platform", "linux")
    assert no_window_kwargs() == {}

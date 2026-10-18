"""Per-launch start marker in the shared crash log (newest entry last)."""

import sys

from redactor_common.core import crash_log


def _fresh_install(log, monkeypatch, **kw):
    monkeypatch.setattr(sys, "excepthook", lambda *a: None)
    monkeypatch.setattr(crash_log, "_faulthandler_file", None)
    crash_log.install(log, enable_faulthandler=False, **kw)


def test_marker_written_once_with_timestamp_pid_and_label(tmp_path, monkeypatch):
    import os, re
    log = tmp_path / "c.log"
    _fresh_install(log, monkeypatch, app_label="epubredactor 2026-10-18#03")
    text = log.read_text(encoding="utf-8")
    assert text.count(crash_log.ENTRY_SEPARATOR) == 1
    assert re.search(
        rf"\d{{4}}-\d\d-\d\d \d\d:\d\d:\d\d started: pid {os.getpid()} epubredactor 2026-10-18#03", text)


def test_marker_without_label_and_one_per_launch(tmp_path, monkeypatch):
    log = tmp_path / "c.log"
    _fresh_install(log, monkeypatch)
    _fresh_install(log, monkeypatch)
    text = log.read_text(encoding="utf-8")
    assert text.count("started: pid") == 2
    assert all(line.endswith(("started: pid " + str(__import__("os").getpid()))) for line in text.splitlines() if "started" in line)


def test_exceptions_follow_the_marker_newest_last(tmp_path, monkeypatch):
    log = tmp_path / "c.log"
    _fresh_install(log, monkeypatch)
    crash_log.write_crash_entry(ValueError, ValueError("first"), None, log)
    crash_log.write_crash_entry(ValueError, ValueError("second"), None, log)
    text = log.read_text(encoding="utf-8")
    assert text.index("started: pid") < text.index("first") < text.index("second")


def test_trim_still_starts_at_a_separator_with_markers(tmp_path, monkeypatch):
    log = tmp_path / "c.log"
    for _ in range(40):
        _fresh_install(log, monkeypatch)
        crash_log.write_crash_entry(ValueError, ValueError("x" * 200), None, log)
    crash_log.trim_if_oversized(log, max_bytes=2000)
    text = log.read_text(encoding="utf-8")
    assert len(text.encode("utf-8")) <= 2000
    assert text.startswith(crash_log.ENTRY_SEPARATOR)


def test_unwritable_path_does_not_raise(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    _fresh_install(blocker / "sub" / "c.log", monkeypatch)  # parent is a file
    crash_log.write_start_marker(blocker / "sub" / "c.log")

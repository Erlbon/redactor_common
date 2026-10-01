"""Tests for the lock-retry helpers (core/os_utils) and their use in
commit_in_place() and RenameLog: transient Windows file locks must not
fail a commit, and a lock that never clears must fail it cleanly."""

import os

import pytest

from redactor_common.core import os_utils
from redactor_common.core.os_utils import (
    is_lock_error,
    rename_with_retry,
    replace_with_retry,
    retry_on_lock,
)
from redactor_common.core.pipeline import CommitError, commit_in_place
from redactor_common.core.rename_log import RenameLog


def _flaky(real, failures, exc_factory=lambda: PermissionError(13, "locked")):
    """Wrap `real` so its first `failures` calls raise (None = always)."""
    calls = {"n": 0}

    def wrapper(*args, **kwargs):
        calls["n"] += 1
        if failures is None or calls["n"] <= failures:
            raise exc_factory()
        return real(*args, **kwargs)

    wrapper.calls = calls
    return wrapper


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(os_utils.time, "sleep", lambda s: None)


def _files(tmp_path, data=b"old", new=b"new content"):
    orig = tmp_path / "book.cbz"
    orig.write_bytes(data)
    temp = tmp_path / "book.tmp"
    temp.write_bytes(new)
    return str(orig), str(temp)


def _trash(path):
    os.remove(path)


# --- helper ----------------------------------------------------------------


def test_retries_permission_error_then_succeeds():
    f = _flaky(lambda: "ok", 2)
    sleeps = []
    assert retry_on_lock(f, sleep=sleeps.append) == "ok"
    assert f.calls["n"] == 3 and len(sleeps) == 2


def test_gives_up_after_attempts_and_reraises_last():
    f = _flaky(lambda: "ok", None)
    sleeps = []
    with pytest.raises(PermissionError):
        retry_on_lock(f, attempts=4, sleep=sleeps.append)
    assert f.calls["n"] == 4 and len(sleeps) == 3


def test_default_total_wait_is_under_two_seconds():
    sleeps = []
    with pytest.raises(PermissionError):
        retry_on_lock(_flaky(lambda: 0, None), sleep=sleeps.append)
    assert len(sleeps) == 5 and sum(sleeps) < 2.0


@pytest.mark.parametrize("exc", [FileExistsError("x"), FileNotFoundError("x"), ValueError("x")])
def test_does_not_retry_other_errors(exc):
    calls = []

    def boom():
        calls.append(1)
        raise exc

    with pytest.raises(type(exc)):
        retry_on_lock(boom, sleep=lambda s: None)
    assert len(calls) == 1


def test_passes_args_and_kwargs_through():
    assert retry_on_lock(lambda a, b=0: a + b, 1, b=2, sleep=lambda s: None) == 3


def test_winerror_handling(monkeypatch):
    def winerr(code):
        exc = OSError(code, "win")
        exc.winerror = code
        return exc

    monkeypatch.setattr(os_utils.sys, "platform", "win32")
    for code in (5, 32, 33):
        assert is_lock_error(winerr(code))
    assert not is_lock_error(winerr(2))
    f = _flaky(lambda: "ok", 1, lambda: winerr(32))
    assert retry_on_lock(f, sleep=lambda s: None) == "ok"
    other = _flaky(lambda: "ok", 1, lambda: winerr(2))
    with pytest.raises(OSError):
        retry_on_lock(other, sleep=lambda s: None)
    assert other.calls["n"] == 1
    monkeypatch.setattr(os_utils.sys, "platform", "linux")
    assert not is_lock_error(winerr(32))


def test_replace_and_rename_wrappers(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_text("1")
    monkeypatch.setattr(os, "replace", _flaky(os.replace, 2))
    replace_with_retry(str(a), str(b))
    assert b.read_text() == "1" and not a.exists()
    a.write_text("2")
    with pytest.raises(FileExistsError):  # still refuses to clobber
        rename_with_retry(str(a), str(b))
    assert b.read_text() == "1"
    c = tmp_path / "c"
    rename_with_retry(str(a), str(c))
    assert c.read_text() == "2"


# --- commit_in_place ---------------------------------------------------------


@pytest.mark.parametrize("failures", [1, 2])
def test_commit_survives_transient_replace_lock(tmp_path, monkeypatch, failures):
    orig, temp = _files(tmp_path)
    flaky = _flaky(os.replace, failures)
    monkeypatch.setattr(os, "replace", flaky)
    result = commit_in_place(orig, temp, trash=_trash)
    assert open(orig, "rb").read() == b"new content"
    assert not result.backup_kept and flaky.calls["n"] == failures + 1
    assert os.listdir(tmp_path) == ["book.cbz"]


def test_commit_survives_transient_set_aside_lock(tmp_path, monkeypatch):
    orig, temp = _files(tmp_path)
    monkeypatch.setattr(os, "rename", _flaky(os.rename, 2))
    commit_in_place(orig, temp, trash=_trash)
    assert open(orig, "rb").read() == b"new content"


def test_commit_replace_lock_never_clears_rolls_back(tmp_path, monkeypatch):
    orig, temp = _files(tmp_path)
    real = os.replace

    def replace(src, dst):
        if src == temp:  # the new file can't go in; the rollback can
            raise PermissionError(13, "locked")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(CommitError) as info:
        commit_in_place(orig, temp, trash=_trash)
    assert "may be locked by another program" in str(info.value)
    assert "original restored" in str(info.value)
    assert open(orig, "rb").read() == b"old"
    assert open(temp, "rb").read() == b"new content"
    assert sorted(os.listdir(tmp_path)) == ["book.cbz", "book.tmp"]


def test_commit_set_aside_lock_never_clears(tmp_path, monkeypatch):
    orig, temp = _files(tmp_path)
    monkeypatch.setattr(os, "rename", _flaky(os.rename, None))
    with pytest.raises(CommitError) as info:
        commit_in_place(orig, temp, trash=_trash)
    assert "may be locked by another program" in str(info.value)
    assert "nothing was changed" in str(info.value)
    assert open(orig, "rb").read() == b"old"
    assert os.path.exists(temp)


def test_commit_total_lock_keeps_backup_path_in_message(tmp_path, monkeypatch):
    orig, temp = _files(tmp_path)
    monkeypatch.setattr(os, "replace", _flaky(os.replace, None))  # rollback fails too
    with pytest.raises(CommitError) as info:
        commit_in_place(orig, temp, trash=_trash)
    msg = str(info.value)
    assert "rollback failed" in msg and "redact-orig" in msg
    backup = str(tmp_path / "book.redact-orig.cbz")
    assert open(backup, "rb").read() == b"old"  # the original is preserved


# --- new_path branch ---------------------------------------------------------


def test_new_path_survives_transient_lock(tmp_path, monkeypatch):
    orig, temp = _files(tmp_path)
    target = str(tmp_path / "book.cbz2")
    monkeypatch.setattr(os, "rename", _flaky(os.rename, 3))
    monkeypatch.setattr(os, "link", _flaky(os.link, 3))  # POSIX no-clobber rename goes via os.link
    result = commit_in_place(orig, temp, trash=_trash, new_path=target)
    assert result.final_path == target
    assert open(target, "rb").read() == b"new content"
    assert not os.path.exists(orig) and not os.path.exists(temp)


def test_new_path_lock_never_clears_leaves_original_and_temp(tmp_path, monkeypatch):
    orig, temp = _files(tmp_path)
    target = str(tmp_path / "book.cbz2")
    monkeypatch.setattr(os, "rename", _flaky(os.rename, None))
    monkeypatch.setattr(os, "link", _flaky(os.link, None))  # POSIX no-clobber rename goes via os.link
    with pytest.raises(CommitError) as info:
        commit_in_place(orig, temp, trash=_trash, new_path=target)
    assert "may be locked by another program" in str(info.value)
    assert open(orig, "rb").read() == b"old"
    assert os.path.exists(temp) and not os.path.exists(target)


def test_new_path_original_set_aside_lock_undoes_new_file(tmp_path, monkeypatch):
    orig, temp = _files(tmp_path)
    target = str(tmp_path / "book.cbz2")
    real = os.rename

    def rename(src, dst):
        if src == orig:  # only the original is locked
            raise PermissionError(13, "locked")
        return real(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    with pytest.raises(CommitError) as info:
        commit_in_place(orig, temp, trash=_trash, new_path=target)
    assert "may be locked by another program" in str(info.value)
    assert open(orig, "rb").read() == b"old"
    assert os.path.exists(temp) and not os.path.exists(target)


def test_non_lock_failure_has_no_lock_hint(tmp_path, monkeypatch):
    orig, temp = _files(tmp_path)

    def boom(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "rename", boom)
    with pytest.raises(CommitError) as info:
        commit_in_place(orig, temp, trash=_trash)
    assert "locked" not in str(info.value)


# --- rename_log --------------------------------------------------------------


def test_rename_log_write_rides_out_a_lock(tmp_path, monkeypatch):
    log = RenameLog(str(tmp_path / "log.json"))
    monkeypatch.setattr(os, "replace", _flaky(os.replace, 3))
    log.record("x", [("a", "b")])
    assert log.last_batch().renames == [("a", "b")]

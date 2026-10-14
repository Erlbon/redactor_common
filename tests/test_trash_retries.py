"""core/trash: trash_with_retries() and move_to_trash_retrying(), promoted from cli/commands and
cbzredactor's _trash_retrying (2026-10-14)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from redactor_common.core import trash  # noqa: E402
from redactor_common.core.trash import (  # noqa: E402
    TrashError, TrashUnavailable, move_to_trash_retrying, trash_with_retries,
)

NO_SLEEP = dict(sleep=lambda s: None)


def make(tmp_path, name="a.cbz"):
    p = tmp_path / name
    p.write_bytes(b"x")
    return str(p)


def test_cli_import_is_the_same_function():
    from redactor_common.cli.commands import trash_with_retries as from_cli

    assert from_cli is trash_with_retries


def test_a_brief_lock_is_retried_until_it_works(tmp_path):
    path = make(tmp_path)
    calls = []

    def flaky(p):
        calls.append(p)
        if len(calls) < 3:
            raise TrashError("in use")
        os.remove(p)

    trash_with_retries(flaky, **NO_SLEEP)(path)
    assert len(calls) == 3 and not os.path.exists(path)


def test_waits_between_attempts(tmp_path):
    path = make(tmp_path)
    waits = []
    with pytest.raises(TrashError):
        trash_with_retries(lambda p: (_ for _ in ()).throw(TrashError("locked")), attempts=3, delay=0.5,
                           sleep=waits.append)(path)
    assert waits == [0.5, 0.5]  # none after the last attempt


def test_a_file_gone_after_an_error_counts_as_done(tmp_path):
    path = make(tmp_path)
    calls = []

    def moved_then_error(p):
        calls.append(p)
        os.remove(p)
        raise TrashError("the shell said no, after doing it")

    trash_with_retries(moved_then_error, **NO_SLEEP)(path)
    assert len(calls) == 1


def test_a_lasting_failure_raises_after_all_attempts(tmp_path):
    path = make(tmp_path)
    calls = []

    def locked(p):
        calls.append(p)
        raise TrashError("locked")

    with pytest.raises(TrashError, match="locked"):
        trash_with_retries(locked, attempts=3, **NO_SLEEP)(path)
    assert len(calls) == 3 and os.path.exists(path)


def test_unavailable_bin_is_not_retried(tmp_path):
    path = make(tmp_path)
    calls = []

    def no_bin(p):
        calls.append(p)
        raise TrashUnavailable("network drive")

    with pytest.raises(TrashUnavailable):
        trash_with_retries(no_bin, **NO_SLEEP)(path)
    assert len(calls) == 1


def test_unavailable_is_still_a_trash_error():
    assert issubclass(TrashUnavailable, TrashError)


def test_other_exceptions_are_not_swallowed(tmp_path):
    path = make(tmp_path)
    with pytest.raises(ValueError):
        trash_with_retries(lambda p: (_ for _ in ()).throw(ValueError("bug")), **NO_SLEEP)(path)


def test_attempts_below_one_still_tries_once(tmp_path):
    path = make(tmp_path)
    seen = []
    trash_with_retries(seen.append, attempts=0, **NO_SLEEP)(path)
    assert seen == [path]


def test_the_path_is_given_in_its_plain_shell_form(tmp_path):
    seen = []
    ext = chr(92) * 2 + "?" + chr(92)
    trash_with_retries(seen.append, **NO_SLEEP)(ext + "D:/Download" + chr(92) + "a.cbz")
    assert seen == [os.path.normpath("D:/Download" + chr(92) + "a.cbz")]


def test_default_is_move_to_trash_looked_up_when_called(tmp_path, monkeypatch):
    path = make(tmp_path)
    wrapper = trash_with_retries(**NO_SLEEP)  # made before the patch, like a module-level default would be
    seen = []
    monkeypatch.setattr(trash, "move_to_trash", seen.append)
    wrapper(path)
    assert seen == [path]


def test_default_sleep_is_time_sleep_looked_up_when_called(tmp_path, monkeypatch):
    path = make(tmp_path)
    waits = []
    monkeypatch.setattr(trash.time, "sleep", waits.append)
    with pytest.raises(TrashError):
        trash_with_retries(lambda p: (_ for _ in ()).throw(TrashError("locked")), attempts=2, delay=0.1)(path)
    assert waits == [0.1]


def test_move_to_trash_retrying_passes_the_fallback_folder(tmp_path, monkeypatch):
    path = make(tmp_path)
    calls = []

    def fake(p, fallback_folder=None):
        calls.append((p, fallback_folder))
        if len(calls) == 1:
            raise TrashError("in use")
        os.remove(p)

    monkeypatch.setattr(trash, "move_to_trash", fake)
    monkeypatch.setattr(trash.time, "sleep", lambda s: None)
    move_to_trash_retrying(path, fallback_folder="F")
    assert calls == [(path, "F"), (path, "F")]


def test_move_to_trash_retrying_raises_a_lasting_failure(tmp_path, monkeypatch):
    path = make(tmp_path)
    monkeypatch.setattr(trash, "move_to_trash", lambda p, fallback_folder=None: (_ for _ in ()).throw(TrashError("x")))
    monkeypatch.setattr(trash.time, "sleep", lambda s: None)
    with pytest.raises(TrashError):
        move_to_trash_retrying(path, attempts=2)
    assert os.path.exists(path)


def test_move_to_trash_raises_unavailable_on_an_unreliable_drive(tmp_path, monkeypatch):
    path = make(tmp_path)
    monkeypatch.setattr(trash, "trash_is_reliable", lambda p: False)
    with pytest.raises(TrashUnavailable):
        trash.move_to_trash(path)
    assert os.path.exists(path)

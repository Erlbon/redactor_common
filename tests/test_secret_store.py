"""Tests for the shared secret store (fake in-memory keyring backend; the
real Credential Manager / Keychain / Secret Service are never touched)."""

import os
import stat
import sys

import pytest

from redactor_common.core import secret_store as ss

SECRET = "s3cr3t-VALUE-12345"


class FakeBackend:
    def __init__(self, fail_set=False, drop_writes=False):
        self.data = {}
        self.fail_set = fail_set
        self.drop_writes = drop_writes  # simulates a store that "saves" but reads back nothing

    def get_password(self, service, name):
        return self.data.get((service, name))

    def set_password(self, service, name, value):
        if self.fail_set:
            raise OSError(f"boom {value}")  # message deliberately contains the value
        if not self.drop_writes:
            self.data[(service, name)] = value

    def delete_password(self, service, name):
        if (service, name) not in self.data:
            raise type("PasswordDeleteError", (Exception,), {})("not found")
        del self.data[(service, name)]


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    ss.set_backend(None)
    ss.set_fallback_dir(tmp_path)
    ss.set_allow_unencrypted_fallback(False)
    monkeypatch.delenv("TEST_SECRET_ENV", raising=False)
    yield
    ss.set_backend(None)
    ss.set_fallback_dir(None)
    ss.set_allow_unencrypted_fallback(False)


@pytest.fixture
def backend():
    b = FakeBackend()
    ss.set_backend(b)
    return b


def test_set_get_delete(backend):
    assert ss.get_secret("app", "k") == ""
    assert ss.set_secret("app", "k", SECRET) == "keyring"
    assert backend.data[("redactor/app", "k")] == SECRET
    assert ss.get_secret("app", "k") == SECRET
    assert ss.secret_source("app", "k") == "keyring"
    ss.delete_secret("app", "k")
    assert ss.get_secret("app", "k") == ""
    assert ss.secret_source("app", "k") == "none"
    ss.delete_secret("app", "k")  # absent: no error


def test_empty_value_deletes(backend):
    ss.set_secret("app", "k", SECRET)
    ss.set_secret("app", "k", "")
    assert ss.get_secret("app", "k") == ""


def test_env_overrides_keyring_then_legacy_last(backend, monkeypatch):
    ss.set_secret("app", "k", SECRET)
    monkeypatch.setenv("TEST_SECRET_ENV", "from-env")
    assert ss.get_secret("app", "k", env_var="TEST_SECRET_ENV") == "from-env"
    assert ss.secret_source("app", "k", env_var="TEST_SECRET_ENV") == "env"
    assert ss.get_secret("app", "k") == SECRET  # no env_var named -> ignored
    ss.delete_secret("app", "k")
    monkeypatch.delenv("TEST_SECRET_ENV")
    assert ss.get_secret("app", "k", legacy="old") == "old"
    assert ss.get_secret("app", "k", legacy=lambda: "older") == "older"
    assert ss.secret_source("app", "k") == "none"  # legacy isn't a source


def test_unavailable_keyring_raises(tmp_path):
    ss.set_backend(None)  # auto-detect: keyring absent or fail backend either way
    if ss.keyring_available():
        pytest.skip("a real keyring is installed here")
    assert not ss.keyring_available()
    with pytest.raises(ss.SecretStoreUnavailable):
        ss.set_secret("app", "k", SECRET)
    assert not list(tmp_path.iterdir())


def test_keyring_exception_raises_without_value(backend):
    backend.fail_set = True
    with pytest.raises(ss.SecretStoreUnavailable) as err:
        ss.set_secret("app", "k", SECRET)
    assert SECRET not in str(err.value) and SECRET not in repr(err.value)
    assert SECRET not in repr(err.value.__cause__)  # raised `from None`


def test_get_survives_broken_keyring():
    class Broken(FakeBackend):
        def get_password(self, service, name):
            raise RuntimeError("locked")
    ss.set_backend(Broken())
    assert ss.get_secret("app", "k") == ""


def test_optin_fallback_file(tmp_path):
    ss.set_backend(None)
    if ss.keyring_available():
        pytest.skip("a real keyring is installed here")
    assert ss.set_secret("app", "k", SECRET, allow_unencrypted_fallback=True) == "unencrypted-file"
    path = ss.fallback_path("app")
    assert path.exists() and "UNENCRYPTED" in path.name
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert ss.get_secret("app", "k") == SECRET  # reading needs no opt-in
    assert ss.secret_source("app", "k") == "unencrypted-file"
    ss.delete_secret("app", "k")
    assert ss.get_secret("app", "k") == ""


def test_process_wide_optin_and_fallback_when_keyring_write_fails(backend):
    backend.fail_set = True
    ss.set_allow_unencrypted_fallback(True)
    assert ss.set_secret("app", "k", SECRET) == "unencrypted-file"
    assert ss.get_secret("app", "k") == SECRET


def test_keyring_write_removes_stale_fallback_copy(backend):
    backend.fail_set = True
    ss.set_secret("app", "k", "old", allow_unencrypted_fallback=True)
    backend.fail_set = False
    ss.set_secret("app", "k", SECRET)
    assert ss.secret_source("app", "k") == "keyring"
    assert "old" not in ss.fallback_path("app").read_text(encoding="utf-8")


def test_migration_success(backend):
    state = {"legacy": SECRET, "cleared": False}

    def clear():
        state["legacy"] = ""
        state["cleared"] = True

    assert ss.migrate_legacy_secret("app", "k", lambda: state["legacy"], clear) is True
    assert state["cleared"] and ss.get_secret("app", "k") == SECRET


def test_migration_nothing_to_migrate(backend):
    assert ss.migrate_legacy_secret("app", "k", lambda: "", lambda: pytest.fail("cleared")) is False


def test_migration_store_unavailable_keeps_legacy():
    ss.set_backend(None)
    if ss.keyring_available():
        pytest.skip("a real keyring is installed here")
    cleared = []
    assert ss.migrate_legacy_secret("app", "k", lambda: SECRET, lambda: cleared.append(1)) is False
    assert not cleared


def test_migration_verify_failure_keeps_legacy():
    ss.set_backend(FakeBackend(drop_writes=True))
    cleared = []
    assert ss.migrate_legacy_secret("app", "k", lambda: SECRET, lambda: cleared.append(1)) is False
    assert not cleared


def test_unavailable_message_has_no_value():
    ss.set_backend(None)
    if ss.keyring_available():
        pytest.skip("a real keyring is installed here")
    with pytest.raises(ss.SecretStoreUnavailable) as err:
        ss.set_secret("app", "k", SECRET)
    assert SECRET not in str(err.value) and SECRET not in repr(err.value)

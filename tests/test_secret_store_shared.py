"""The family's shared keys (secret_store.get_key / set_key / unshared_keys / share_keys, 2026-11-06): one entry per
service for every Redactor; an app's own old entry is only the fallback; nothing is shared without being asked; a
removal takes the old copy along; and the API Keys row (SecretField(shared=True)) reads and writes the shared entry and
says which apps use it. A fake in-memory keyring: the real credential store is never touched."""

import os

import pytest

from redactor_common.core import secret_store as ss
from tests.test_secret_store import FakeBackend

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def backend(tmp_path, monkeypatch):
    b = FakeBackend()
    ss.set_backend(b)
    ss.set_fallback_dir(tmp_path)
    ss.set_allow_unencrypted_fallback(False)
    for key in ss.FAMILY_KEYS.values():
        if key.env_var:
            monkeypatch.delenv(key.env_var, raising=False)
    yield b
    ss.set_backend(None)
    ss.set_fallback_dir(None)


def test_env_then_shared_then_the_apps_own_old_entry(backend, monkeypatch):
    assert ss.get_key("discogs_token", "folderredactor") == ""
    ss.set_secret("mp3redactor", "discogs_token", "OWN")
    assert ss.get_key("discogs_token", "mp3redactor") == "OWN"  # not shared yet: its own entry still works
    assert ss.get_key("discogs_token", "folderredactor") == ""  # another app's own entry is not read silently
    ss.set_key("discogs_token", "SHARED")
    assert ss.get_key("discogs_token", "mp3redactor") == "SHARED" == ss.get_key("discogs_token", "folderredactor")
    assert ss.key_location("discogs_token", "mp3redactor") == (ss.WHERE_SHARED, ss.SOURCE_KEYRING)
    monkeypatch.setenv("DISCOGS_TOKEN", "ENV")
    assert ss.get_key("discogs_token", "folderredactor", env_var="DISCOGS_TOKEN") == "ENV"
    assert ss.key_location("discogs_token", "x", env_var="DISCOGS_TOKEN") == (ss.WHERE_ENV, ss.SOURCE_ENV)


def test_a_save_goes_to_the_shared_entry_under_redactor_shared(backend):
    ss.set_key("tmdb_api_key", "T")
    assert backend.data == {("redactor/shared", "tmdb_api_key"): "T"}


def test_the_unshared_keys_are_found_once_each_and_shared_on_request(backend):
    ss.set_secret("mp3redactor", "discogs_token", "D")
    ss.set_secret("folderredactor", "discogs_token", "D2")
    ss.set_secret("videoredactor", "tmdb_api_key", "T")
    ss.set_secret("cbzredactor", "comicvine_api_key", "C")
    ss.set_key("comicvine_api_key", "already shared")
    found = ss.unshared_keys()
    assert [(k.name, k.app) for k in found] == [("discogs_token", "mp3redactor"), ("tmdb_api_key", "videoredactor")]
    assert "D" not in repr(found)  # never the value
    assert ss.share_keys(found) == []
    assert ss.get_key("tmdb_api_key", "folderredactor") == "T" and ss.unshared_keys() == []
    assert ss.get_secret("videoredactor", "tmdb_api_key") == "T"  # the old entry stays for an app on an older pin
    assert [k.name for k in ss.unshared_keys(["discogs_token"])] == []


def test_removing_a_key_removes_the_shared_entry_and_the_apps_old_copy(backend):
    ss.set_secret("mp3redactor", "discogs_token", "OLD")
    ss.set_key("discogs_token", "NEW")
    ss.delete_key("discogs_token", "mp3redactor")
    assert ss.get_key("discogs_token", "mp3redactor") == ""  # the old copy did not come back


def test_without_a_keyring_sharing_asks_for_the_fallback_and_the_fallback_file_is_shared_too(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "_get_backend", lambda: None)  # no usable credential store on this machine
    found = [ss.UnsharedKey("tmdb_api_key", "TMDB API Key", "videoredactor", "SECRET-T")]
    problems = ss.share_keys(found)  # not allowed yet: said, per key, never with the value
    assert len(problems) == 1 and problems[0].startswith("TMDB API Key: ") and "SECRET-T" not in problems[0]
    found[0].value = "T"
    assert ss.share_keys(found, allow_unencrypted_fallback=True) == []
    assert (tmp_path / "redactor_shared_secrets_UNENCRYPTED.json").is_file()
    assert ss.get_key("tmdb_api_key", "folderredactor") == "T"  # another app reads the one shared file


def test_every_registered_key_names_its_apps():
    assert ss.users_of("discogs_token") == ("mp3redactor", "folderredactor")
    assert ss.users_of("tmdb_api_key") == ("videoredactor", "folderredactor")
    assert all(k.apps for k in ss.FAMILY_KEYS.values()) and ss.users_of("nope") == ()


def test_the_api_keys_row_reads_and_writes_the_shared_entry_and_names_the_other_apps(backend):
    from PyQt6.QtWidgets import QApplication

    from redactor_common.gui.secret_field import SecretField

    QApplication.instance() or QApplication([])
    ss.set_secret("folderredactor", "tmdb_api_key", "MINE")
    row = SecretField("folderredactor", "tmdb_api_key", env_var="TMDB_API_KEY", shared=True)
    assert "for this program only" in row.source_label.text()
    row.edit.setText("NEW")
    assert row.apply() is True
    assert ss.get_secret(ss.SHARED_APP, "tmdb_api_key") == "NEW" and ss.get_key("tmdb_api_key", "videoredactor") == "NEW"
    assert "shared with videoredactor" in row.source_label.text()
    row._on_remove()
    row.apply()
    assert ss.get_key("tmdb_api_key", "folderredactor") == ""  # shared entry and the old own copy both gone
    plain = SecretField("cbzredactor", "gcd_password")  # an app that has not moved over: unchanged behaviour
    plain.edit.setText("PW")
    plain.apply()
    assert ss.get_secret("cbzredactor", "gcd_password") == "PW" and ss.get_secret(ss.SHARED_APP, "gcd_password") == ""


def test_the_discogs_client_reads_the_shared_token(backend, monkeypatch):
    from redactor_common.core.lookups import discogs

    monkeypatch.setattr(discogs, "SECRET_APP", "folderredactor")
    assert discogs.load_token() == ""
    ss.set_key("discogs_token", "SHARED")
    assert discogs.load_token() == "SHARED"

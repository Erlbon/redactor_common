"""The "no secure store: may I use an unencrypted file?" flow:
secret_store.save_with_consent()/bind_fallback_preference() (Qt-free) and
gui/secret_prompts.save_with_fallback_prompt() (offscreen, message boxes
patched). Covers what mp3redactor's, cbzredactor's and videoredactor's
copies did, plus the second-save failure cbz once crashed on."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from redactor_common.core import secret_store as ss  # noqa: E402
from redactor_common.gui import secret_prompts as sp  # noqa: E402
from test_secret_store import SECRET, FakeBackend  # noqa: E402

_app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolated(tmp_path):
    ss.set_backend(FakeBackend(fail_set=True))  # the keyring refuses: the prompt is needed
    ss.set_fallback_dir(tmp_path)
    ss.set_allow_unencrypted_fallback(False)
    yield
    ss.set_backend(None)
    ss.set_fallback_dir(None)
    ss.set_allow_unencrypted_fallback(False)


def _save(value=SECRET):
    calls = []

    def save(allow):
        calls.append(allow)
        ss.set_secret("app", "k", value, allow)

    return save, calls


# -- save_with_consent (Qt-free) ----------------------------------------------

def test_keyring_works_no_question():
    ss.set_backend(FakeBackend())
    save, calls = _save()
    assert ss.save_with_consent(save, ask=lambda: pytest.fail("asked")) is True
    assert calls == [None]
    assert ss.secret_source("app", "k") == ss.SOURCE_KEYRING


def test_already_allowed_no_question():
    ss.set_allow_unencrypted_fallback(True)
    save, calls = _save()
    assert ss.save_with_consent(save, ask=lambda: pytest.fail("asked")) is True
    assert calls == [None]
    assert ss.secret_source("app", "k") == ss.SOURCE_FILE


def test_no_keeps_nothing_and_remembers_nothing():
    save, calls = _save()
    remembered = []
    assert ss.save_with_consent(save, ask=lambda: False, remember=lambda: remembered.append(1)) is False
    assert calls == [None]
    assert remembered == []
    assert not ss.allow_unencrypted_fallback_enabled()
    assert ss.get_secret("app", "k") == ""


def test_yes_remembers_and_saves_to_the_file():
    save, calls = _save()
    remembered = []
    assert ss.save_with_consent(save, ask=lambda: True, remember=lambda: remembered.append(1)) is True
    assert calls == [None, True]
    assert remembered == [1]
    assert ss.allow_unencrypted_fallback_enabled()
    assert ss.secret_source("app", "k") == ss.SOURCE_FILE
    assert ss.get_secret("app", "k") == SECRET


def test_yes_without_remember_still_saves():
    save, _ = _save()
    assert ss.save_with_consent(save, ask=lambda: True) is True
    assert ss.allow_unencrypted_fallback_enabled()


def test_second_save_failure_is_reported_not_raised(tmp_path):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x")
    ss.set_fallback_dir(blocker / "sub")  # can't be created: a file is in the way
    save, calls = _save()
    errors = []
    assert ss.save_with_consent(save, ask=lambda: True, on_error=errors.append) is False
    assert calls == [None, True]
    assert len(errors) == 1 and isinstance(errors[0], ss.SecretStoreUnavailable)
    assert SECRET not in str(errors[0])


def test_second_save_failure_without_on_error_still_does_not_raise():
    def save(allow):
        raise ss.SecretStoreUnavailable("nope")

    assert ss.save_with_consent(save, ask=lambda: True) is False


def test_remember_failure_is_reported():
    save, calls = _save()
    errors = []

    def remember():
        raise OSError("settings file locked")

    assert ss.save_with_consent(save, ask=lambda: True, remember=remember, on_error=errors.append) is False
    assert calls == [None]
    assert len(errors) == 1


def test_other_errors_from_save_propagate():
    def save(allow):
        raise KeyError("a bug")

    with pytest.raises(KeyError):
        ss.save_with_consent(save, ask=lambda: True)


# -- bind_fallback_preference --------------------------------------------------

def test_bind_applies_a_stored_yes_at_startup():
    ss.bind_fallback_preference(lambda: True, lambda on: None)
    assert ss.allow_unencrypted_fallback_enabled()


def test_bind_applies_a_stored_no_at_startup():
    ss.set_allow_unencrypted_fallback(True)
    ss.bind_fallback_preference(lambda: False, lambda on: None)
    assert not ss.allow_unencrypted_fallback_enabled()


def test_bind_load_failure_counts_as_no():
    def load():
        raise ValueError("garbled ini")

    ss.bind_fallback_preference(load, lambda on: None)
    assert not ss.allow_unencrypted_fallback_enabled()


def test_bind_accepts_string_like_values():
    ss.bind_fallback_preference(lambda: "", lambda on: None)
    assert not ss.allow_unencrypted_fallback_enabled()


def test_bind_remember_persists_and_enables():
    stored = []
    remember = ss.bind_fallback_preference(lambda: False, stored.append)
    assert not ss.allow_unencrypted_fallback_enabled()
    remember()
    assert stored == [True]
    assert ss.allow_unencrypted_fallback_enabled()


def test_bind_remember_end_to_end_with_consent():
    stored = []
    remember = ss.bind_fallback_preference(lambda: False, stored.append)
    save, _ = _save()
    assert ss.save_with_consent(save, ask=lambda: True, remember=remember) is True
    assert stored == [True]
    # The next save in this process doesn't ask again.
    save2, calls2 = _save("other")
    assert ss.save_with_consent(save2, ask=lambda: pytest.fail("asked twice")) is True
    assert calls2 == [None]


# -- gui/secret_prompts ----------------------------------------------------------

@pytest.fixture
def boxes(monkeypatch):
    seen = {"questions": [], "warnings": [], "answer": QMessageBox.StandardButton.Yes}

    def question(parent, title, text, buttons, default):
        seen["questions"].append((title, text, default))
        return seen["answer"]

    def warning(parent, title, text):
        seen["warnings"].append((title, text))
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(warning))
    return seen


def test_question_names_the_noun_and_defaults_to_no(boxes):
    boxes["answer"] = QMessageBox.StandardButton.No
    assert sp.ask_allow_unencrypted_fallback(None, noun="key or password") is False
    title, text, default = boxes["questions"][0]
    assert title == "No Secure Storage Available"
    assert "the key or password can't be stored safely" in text
    assert "UNENCRYPTED" in text
    assert default == QMessageBox.StandardButton.No


def test_question_default_noun():
    assert "the key can't" in sp.fallback_question()
    assert "the key can't" in sp.fallback_question("")


def test_prompt_yes_saves_and_remembers(boxes):
    save, calls = _save()
    remembered = []
    assert sp.save_with_fallback_prompt(None, save, lambda: remembered.append(1), noun="token") is True
    assert calls == [None, True] and remembered == [1]
    assert "the token can't" in boxes["questions"][0][1]
    assert boxes["warnings"] == []


def test_prompt_no_returns_false(boxes):
    boxes["answer"] = QMessageBox.StandardButton.No
    save, calls = _save()
    assert sp.save_with_fallback_prompt(None, save) is False
    assert calls == [None]


def test_prompt_keyring_ok_asks_nothing(boxes):
    ss.set_backend(FakeBackend())
    save, _ = _save()
    assert sp.save_with_fallback_prompt(None, save) is True
    assert boxes["questions"] == []


def test_prompt_second_failure_shows_a_warning(boxes, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    ss.set_fallback_dir(blocker / "sub")
    save, _ = _save()
    assert sp.save_with_fallback_prompt(None, save) is False
    assert len(boxes["warnings"]) == 1
    title, text = boxes["warnings"][0]
    assert title == "Couldn't Save" and SECRET not in text


def test_prompt_remember_is_optional(boxes):
    save, _ = _save()
    assert sp.save_with_fallback_prompt(None, save) is True
    assert ss.allow_unencrypted_fallback_enabled()


def test_the_question_reads_right_for_one_key_and_for_several():
    from redactor_common.gui.secret_prompts import fallback_question

    one = fallback_question("key or token")
    assert "Save it in an unencrypted file" in one and "could read it." in one and "It can instead be saved" in one
    many = fallback_question("API keys")
    assert "Save them in an unencrypted file" in many and "could read them." in many and "They can instead be saved" in many
    assert "Save it in" in fallback_question("API keys", plural=False)  # an explicit choice wins
    assert "Save them in" in fallback_question("passphrase", plural=True)
    assert "Save it in" in fallback_question("password")  # "ss" is not a plural

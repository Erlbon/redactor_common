"""
redactor_common/gui/secret_prompts.py

Saving an API key / token / password into redactor_common's secret store
and, when this machine has no secure credential store, asking the user
once whether a plain UNENCRYPTED file may be used instead. Promoted from
mp3redactor's and cbzredactor's gui/secret_prompts.py (identical but for
"token"/"password" in one sentence) and videoredactor's API Keys dialog
(a third wording, and no guard when the fallback save failed too).

The flow itself is secret_store.save_with_consent() (Qt-free, tested
without a display); this module only supplies the two message boxes.
Remembering a yes is the app's business: bind_fallback_preference() in
core/secret_store.py gives the `remember` callable and applies the stored
answer at startup.

Usage, in an API Keys dialog's accept():

    remember = secret_store.bind_fallback_preference(load, save)   # at startup
    ...
    if not save_with_fallback_prompt(self, self._save, remember, noun="token"):
        return  # declined or failed: stay open, the typed values are kept
    super().accept()
"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtWidgets import QMessageBox

from redactor_common.core import secret_store

DEFAULT_NOUN = "key"
_TITLE = "No Secure Storage Available"
_ERROR_TITLE = "Couldn't Save"


def fallback_question(noun: str = DEFAULT_NOUN, plural: bool | None = None) -> str:
    """The question's text. `noun` names what is being saved: "key",
    "key or token", "key or password", "API keys"... `plural` picks "them" for "it"; None guesses from the
    noun ("API keys" is plural, "key or token" is not)."""
    noun = (noun or DEFAULT_NOUN).strip()
    if plural is None:
        plural = noun.lower().endswith("s") and not noun.lower().endswith("ss")
    it, they, read = ("them", "They", "read them") if plural else ("it", "It", "read it")
    return (
        "This computer has no usable secure credential store (such as Windows "
        f"Credential Manager, macOS Keychain or the Linux Secret Service), so the {noun} "
        "can't be stored safely.\n\n"
        f"{they} can instead be saved in a plain, UNENCRYPTED file in your user "
        f"profile folder. Anyone who can read your files could {read}.\n\n"
        f"Save {it} in an unencrypted file, and remember that choice?"
    )


def ask_allow_unencrypted_fallback(parent, noun: str = DEFAULT_NOUN) -> bool:
    """Yes/No, defaulting to No. True only on an explicit Yes."""
    answer = QMessageBox.question(
        parent,
        _TITLE,
        fallback_question(noun),
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


def show_save_error(parent, exc: Exception) -> None:
    """The fallback save failed too: say so (the message names no secret)."""
    QMessageBox.warning(parent, _ERROR_TITLE, str(exc) or "The key could not be saved.")


def save_with_fallback_prompt(
    parent,
    save: Callable[[Optional[bool]], object],
    remember: Optional[Callable[[], None]] = None,
    noun: str = DEFAULT_NOUN,
) -> bool:
    """Call save(None); on SecretStoreUnavailable ask the user and, if they
    agree, remember it (`remember()`, plus the process-wide opt-in) and call
    save(True). A second SecretStoreUnavailable (e.g. the fallback file
    can't be written) is shown in a message box, never raised. True when
    the save happened; False when the user said No or the save failed."""
    return secret_store.save_with_consent(
        save,
        ask=lambda: ask_allow_unencrypted_fallback(parent, noun),
        remember=remember,
        on_error=lambda exc: show_save_error(parent, exc),
    )

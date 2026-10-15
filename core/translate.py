"""
redactor_common/core/translate.py

Machine translation of subtitle cues (Qt-free), with a pluggable engine. NOTHING is bundled: the engine is
either a LibreTranslate-compatible HTTP server (self-hosted, offline models) or the `argos-translate` command
line (argostranslate, offline models), both reached through the Translator interface below.

  translate_cues()          cues in, cues out with every timing kept. Consecutive cues go to the engine in
                            groups (default 8, for context), joined with a separator; when the reply does not
                            hold the separator the right number of times the group is translated cue by cue
                            instead. Empty cues are never sent. Retries, cancel and progress included. Expected
                            conditions (engine down, a cue the engine refuses, cancel) never raise: they are in
                            the TranslateResult.
  Translator                `translate_text(text, source, target) -> str`, `check(source, target) -> str`.
  LibreTranslateTranslator  POST {url}/translate {q, source, target, format: "text", api_key}; the key is never
                            put in a URL or a message.
  ArgosCliTranslator        runs `argos-translate --from-lang xx --to-lang yy` with the text on stdin.
  detect_language()         a small pure-Python guess from the text (script ranges, common words), "" when
                            unsure: the caller asks the user to confirm.
"""

from __future__ import annotations

import re
import time
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from redactor_common.core.lookup_client import (
    FetchFn,
    LookupError,
    build_request,
    fetch_json,
    make_default_fetch,
    redact_text,
)
from redactor_common.core.lrclib import wrap_line
from redactor_common.core.srt import Cue

DEFAULT_GROUP_SIZE = 8
DEFAULT_MAX_CHARS = 1800
DEFAULT_SEPARATOR = "\n"
DEFAULT_URL = "http://localhost:5000"
DEFAULT_COMMAND = "argos-translate"


class TranslateError(LookupError):
    """The engine failed for this request (a message fit to show the user)."""


class TranslateUnavailable(TranslateError):
    """The engine cannot be used at all right now (not reachable, not installed, key refused, language pair
    missing): there is no point trying the next cue."""


class Translator:
    """An engine. Subclasses override translate_text() and check()."""

    name = "translator"
    # Hints for translate_cues(): an engine with a heavy start-up (a command that loads its model on every call)
    # wants bigger groups. None = the module defaults.
    group_size: Optional[int] = None
    max_chars: Optional[int] = None

    def translate_text(self, text: str, source: str, target: str) -> str:
        raise NotImplementedError

    def check(self, source: str = "", target: str = "en") -> str:
        """A one-line report ("reachable, 52 languages", ...); raises TranslateError when unusable."""
        raise NotImplementedError


# -- the cue flow -----------------------------------------------------------------------

@dataclass
class TranslateResult:
    cues: list[Cue]                      # same length and timings as the input; failed cues keep their text
    translated: int = 0                  # cues whose text came from the engine
    fallbacks: int = 0                   # groups that had to be translated cue by cue
    failed: list[int] = field(default_factory=list)  # indexes of cues left untranslated
    cancelled: bool = False
    error: str = ""                      # the engine became unusable: translation stopped
    warnings: list[str] = field(default_factory=list)
    engine: str = ""

    @property
    def complete(self) -> bool:
        return not (self.cancelled or self.error or self.failed)


def _flat(text: str) -> str:
    return " ".join((text or "").split())


def _is_cancelled(cancel: Any) -> bool:
    if cancel is None:
        return False
    if isinstance(cancel, threading.Event):
        return cancel.is_set()
    return bool(cancel())


def _call(translator: Translator, text: str, source: str, target: str, retries: int,
          sleep: Callable[[float], None]) -> str:
    """translator.translate_text with `retries` more tries (short growing waits) for a plain TranslateError;
    TranslateUnavailable is not retried."""
    attempt = 0
    while True:
        try:
            return translator.translate_text(text, source, target)
        except TranslateUnavailable:
            raise
        except TranslateError:
            if attempt >= retries:
                raise
            sleep(0.5 * (2 ** attempt))
            attempt += 1


def translate_cues(
    cues: Sequence[Cue],
    source_lang: str,
    target_lang: str,
    translator: Translator,
    progress: Optional[Callable[[int, int], None]] = None,
    cancel: Any = None,
    group_size: Optional[int] = None,
    max_chars: Optional[int] = None,
    separator: str = DEFAULT_SEPARATOR,
    retries: int = 2,
    wrap_columns: int = 42,
    sleep: Callable[[float], None] = time.sleep,
) -> TranslateResult:
    """Translates the text of `cues`, keeping every start and end. `progress(done, total)` is called after each
    request with cue counts; `cancel` is an Event or a callable returning True to stop (cues not yet
    translated keep their original text, result.cancelled is True)."""
    group_size = group_size or getattr(translator, "group_size", None) or DEFAULT_GROUP_SIZE
    max_chars = max_chars or getattr(translator, "max_chars", None) or DEFAULT_MAX_CHARS
    total = len(cues)
    out = [Cue(c.start, c.end, c.text) for c in cues]
    result = TranslateResult(out, engine=getattr(translator, "name", ""))
    pending = [i for i, c in enumerate(cues) if _flat(c.text)]
    done = total - len(pending)  # empty cues count as done at once
    if progress:
        progress(done, total)

    # groups of consecutive non-empty cues, limited by count and size
    groups: list[list[int]] = []
    current: list[int] = []
    size = 0
    for i in pending:
        length = len(_flat(cues[i].text)) + len(separator)
        if current and (len(current) >= max(1, group_size) or size + length > max_chars):
            groups.append(current)
            current, size = [], 0
        current.append(i)
        size += length
    if current:
        groups.append(current)

    def store(index: int, text: str) -> None:
        out[index].text = wrap_line(text.strip(), wrap_columns) if wrap_columns else text.strip()
        result.translated += 1

    consecutive_single_failures = 0
    for group in groups:
        if _is_cancelled(cancel):
            result.cancelled = True
            break
        texts = [_flat(cues[i].text) for i in group]
        parts: Optional[list[str]] = None
        if len(group) > 1:
            try:
                reply = _call(translator, separator.join(texts), source_lang, target_lang, retries, sleep)
                pieces = reply.strip().split(separator)
                pieces = [p.strip() for p in pieces]
                if len(pieces) == len(group) and all(pieces):
                    parts = pieces
            except TranslateUnavailable as exc:
                result.error = str(exc)
                break
            except TranslateError:
                parts = None
            if parts is None:
                result.fallbacks += 1
                if not any("cue by cue" in w for w in result.warnings):
                    result.warnings.append(
                        "The engine did not keep the lines of a group apart, so some cues were translated one by one "
                        "(slower, with less context)."
                    )
        if parts is not None:
            for index, piece in zip(group, parts):
                store(index, piece)
            done += len(group)
        else:
            for index, text in zip(group, texts):
                if _is_cancelled(cancel):
                    result.cancelled = True
                    break
                try:
                    reply = _call(translator, text, source_lang, target_lang, retries, sleep).strip()
                except TranslateUnavailable as exc:
                    result.error = str(exc)
                    break
                except TranslateError as exc:
                    result.failed.append(index)
                    consecutive_single_failures += 1
                    if consecutive_single_failures >= 3:
                        result.error = str(exc)
                        break
                    continue
                consecutive_single_failures = 0
                if reply:
                    store(index, reply)
                else:
                    result.failed.append(index)
                done += 1
                if progress:
                    progress(min(done, total), total)
            if result.cancelled or result.error:
                break
        if progress:
            progress(min(done, total), total)

    if result.cancelled or result.error:
        translated_ok = {i for i, c in enumerate(out) if c.text != cues[i].text}
        result.failed = sorted(set(result.failed) | {i for i in pending if i not in translated_ok})
    else:
        result.failed = sorted(set(result.failed))
    if result.failed and not result.error and not result.cancelled:
        result.warnings.append(f"{len(result.failed)} cue(s) could not be translated and keep their original text.")
    return result


# -- language guess ------------------------------------------------------------------------

_SCRIPTS: list[tuple[str, str]] = [
    ("ko", "가-힯ᄀ-ᇿ"),
    ("ja", "぀-ヿ"),
    ("zh", "一-鿿"),
    ("ru", "Ѐ-ӿ"),
    ("el", "Ͱ-Ͽ"),
    ("he", "֐-׿"),
    ("ar", "؀-ۿ"),
    ("th", "฀-๿"),
    ("hi", "ऀ-ॿ"),
]
_STOPWORDS = {
    "en": "the and you that with this have for not are was but your".split(),
    "de": "der die das und ich nicht ein ist mit sie auf dem den".split(),
    "fr": "le la les des est une que pas pour dans vous nous qui".split(),
    "es": "el los las que una por con para esto como pero del".split(),
    "it": "il che non per una con sono come questo della gli".split(),
    "pt": "que não uma com para você como mais os das ele".split(),
    "nl": "het een van niet ik je dat zijn voor maar met".split(),
    "sv": "och att det som inte jag på är för med har".split(),
    "da": "og at det som ikke jeg på er for med har af".split(),
    "no": "og at det som ikke jeg på er for med har av".split(),
}


def detect_language(text: str) -> str:
    """A two-letter code guessed from `text`, or "" when it cannot be told. By script (Cyrillic gives "ru", or
    "uk" when letters only Ukrainian uses are present; Japanese kana beat Han), then, for Latin text, by the
    commonest function words. A guess: always confirm with the user."""
    sample = text or ""
    counts = {code: len(re.findall("[" + chars + "]", sample)) for code, chars in _SCRIPTS}
    best = max(counts, key=lambda c: counts[c])
    latin = len(re.findall(r"[A-Za-zÀ-ɏ]", sample))
    if counts[best] and counts[best] >= max(3, latin * 0.3):
        if counts["ja"]:
            return "ja"
        if best == "ru" and re.search("[ґєіїҐЄІЇ]", sample):
            return "uk"
        return best
    words = re.findall(r"[^\W\d_]+", sample.lower())
    if len(words) < 5:
        return ""
    scores = {code: sum(1 for w in words if w in set(stop)) for code, stop in _STOPWORDS.items()}
    best = max(scores, key=lambda c: scores[c])
    if scores[best] >= 2 and scores[best] > sorted(scores.values())[-2]:
        return best
    return ""


# -- backend: LibreTranslate ------------------------------------------------------------------

class LibreTranslateTranslator(Translator):
    """A LibreTranslate-compatible server. `api_key` is optional and travels in the JSON body, never in a URL,
    and is scrubbed from every message."""

    name = "LibreTranslate"

    def __init__(self, url: str = DEFAULT_URL, api_key: str = "", fetch: FetchFn | None = None,
                 timeout: float = 90.0, user_agent: str = "RedactorCommon (https://github.com/Erlbon)",
                 sleep: Callable[[float], None] = time.sleep):
        self.url = (url or DEFAULT_URL).strip().rstrip("/")
        self.api_key = api_key or ""
        self._fetch = fetch or make_default_fetch(user_agent, timeout=timeout)
        self._sleep = sleep

    def _json(self, path: str, body: dict | None = None) -> Any:
        request = build_request(self.url + path, headers={"Accept": "application/json"}, json_body=body)
        secrets = [self.api_key] if self.api_key else []
        data = fetch_json(
            request, self._fetch, TranslateError, "the translation server",
            status_messages={
                400: "The translation server refused the request (HTTP 400): check the language pair.",
                401: "The translation server wants an API key (HTTP 401).",
                403: "The translation server rejected the API key (HTTP 403).",
                429: "The translation server is rate-limiting requests (HTTP 429).",
            },
            status_errors={401: TranslateUnavailable, 403: TranslateUnavailable},
            retry_429=(1.0, 3.0), sleep=self._sleep, secrets=secrets,
            unreachable_message="Could not reach the translation server at " + redact_text(self.url) + ": {detail}",
        )
        return data

    def translate_text(self, text: str, source: str, target: str) -> str:
        body: dict[str, Any] = {"q": text, "source": source or "auto", "target": target, "format": "text"}
        if self.api_key:
            body["api_key"] = self.api_key
        try:
            data = self._json("/translate", body)
        except TranslateError as exc:
            if "Could not reach" in str(exc):
                raise TranslateUnavailable(str(exc)) from exc.__cause__
            raise
        if not isinstance(data, dict) or not isinstance(data.get("translatedText"), str):
            raise TranslateError("The translation server sent an unusable reply.")
        return data["translatedText"]

    def languages(self) -> list[dict]:
        data = self._json("/languages")
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []

    def check(self, source: str = "", target: str = "en") -> str:
        try:
            langs = self.languages()
        except TranslateUnavailable:
            raise
        except TranslateError as exc:
            raise TranslateUnavailable(str(exc)) from exc
        if not langs:
            raise TranslateUnavailable("The server answered but lists no languages.")
        text = f"LibreTranslate at {self.url} is reachable, {len(langs)} languages"
        if source:
            entry = next((l for l in langs if l.get("code") == source), None)
            if entry is None:
                raise TranslateUnavailable(text + f", but it has no {source!r} model.")
            if target not in (entry.get("targets") or [target]):
                raise TranslateUnavailable(text + f", but no {source} -> {target} pair.")
            text += f"; {source} -> {target} is available"
        return text + "."


# -- backend: argos-translate command line ---------------------------------------------------------

class ArgosCliTranslator(Translator):
    """The `argos-translate` command (pip package argostranslate) run as an external program. The text goes in
    on stdin and the translation comes back on stdout (UTF-8). `runner(args, text, timeout)` is injectable and
    returns (returncode, stdout, stderr)."""

    name = "argos-translate"
    group_size = 40      # every call loads the model (several seconds): fewer, bigger requests
    max_chars = 6000

    def __init__(self, command: "str | Sequence[str]" = DEFAULT_COMMAND, runner: Callable | None = None,
                 timeout: float = 300.0):
        # a path / name, or a list when the command needs a prefix (an interpreter and a script)
        self.command = command or DEFAULT_COMMAND
        self._runner = runner or _run_command
        self.timeout = timeout

    def _args(self, source: str, target: str) -> list[str]:
        prefix = [self.command] if isinstance(self.command, str) else list(self.command)
        return prefix + ["--from-lang", source, "--to-lang", target]

    def translate_text(self, text: str, source: str, target: str) -> str:
        if not source:
            raise TranslateUnavailable("argos-translate needs the source language.")
        try:
            code, out, err = self._runner(self._args(source, target), text, self.timeout)
        except FileNotFoundError as exc:
            raise TranslateUnavailable(
                f"The command {self.command!r} was not found: install argostranslate or set its path."
            ) from exc
        except TimeoutError as exc:
            raise TranslateError("argos-translate took too long.") from exc
        if code != 0:
            detail = (err or out or "").strip().splitlines()
            last = detail[-1] if detail else f"exit code {code}"
            low = (err or "").lower()
            if "not an installed language" in low or "no translation" in low or "not installed" in low:
                raise TranslateUnavailable(f"argos-translate has no {source} -> {target} model installed ({last}).")
            raise TranslateError(f"argos-translate failed: {last}")
        return out.rstrip("\n") if out.strip() else ""

    def check(self, source: str = "", target: str = "en") -> str:
        if not source:
            source = "en"
        sample = "Hello." if source == "en" else "."
        # a real call: the only reliable test that the command runs and the model is there
        self.translate_text(sample, source, target)
        return f"{self.command} runs; {source} -> {target} is installed."


def _run_command(args: Sequence[str], text: str, timeout: float) -> tuple[int, str, str]:
    import os
    import subprocess
    from redactor_common.core.subprocess_utils import no_window_kwargs
    # a Python-based command would otherwise read stdin / write stdout in the Windows code page
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    try:
        proc = subprocess.run(
            [str(a) for a in args], input=text.encode("utf-8"), capture_output=True, timeout=timeout,
            env=env, **no_window_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(str(exc)) from exc
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace"), proc.stderr.decode("utf-8", errors="replace")

"""core/srt.py and core/translate.py: SRT parsing/formatting/alignment, the cue translation flow with fake
translators (grouping, a mangled separator, empty cues, retries, cancel, timing), the two backends with a fake
HTTP fetch and a real tiny script as the command-line engine, and the language guess."""

import json
import sys
import threading
import urllib.error

import pytest

from redactor_common.core.srt import Cue, align_cues, clock_text, format_srt, parse_srt
from redactor_common.core.translate import (
    ArgosCliTranslator,
    LibreTranslateTranslator,
    TranslateError,
    TranslateUnavailable,
    Translator,
    detect_language,
    translate_cues,
)

SRT = (
    "﻿1\r\n00:00:01,000 --> 00:00:03,500\r\nПривет, мир\r\n\r\n"
    "2\r\n00:00:04.000 --> 00:00:06.000\r\nПервая строка\r\nвторая строка\r\n\r\n"
    "garbage block\r\n\r\n"
    "00:00:07,000 --> 00:00:08,000\r\nбез номера\r\n"
)


# -- srt ------------------------------------------------------------------------------------

def test_parse_srt_tolerant():
    cues = parse_srt(SRT)
    assert [(c.start, c.end, c.text) for c in cues] == [
        (1.0, 3.5, "Привет, мир"), (4.0, 6.0, "Первая строка\nвторая строка"), (7.0, 8.0, "без номера")]


def test_format_srt_roundtrip_lf_and_numbering():
    out = format_srt(parse_srt(SRT))
    assert "\r" not in out and out.startswith("1\n00:00:01,000 --> 00:00:03,500\nПривет, мир\n\n2\n")
    assert parse_srt(out) == parse_srt(SRT)


def test_clock_text():
    assert clock_text(62.4) == "1:02" and clock_text(3723) == "1:02:03"


def test_align_by_overlap_with_unequal_counts():
    a = [Cue(1, 3, "a1"), Cue(3.1, 5, "a2"), Cue(10, 12, "a3 only")]
    b = [Cue(1.2, 5.1, "b1 long"), Cue(7, 8, "b2 only"), Cue(10.1, 11.9, "b3")]
    rows = align_cues(a, b)
    assert [(r.a_text, r.b_text) for r in rows] == [
        ("a1\na2", "b1 long"), ("", "b2 only"), ("a3 only", "b3")]
    assert [r.start for r in rows] == [1, 7, 10]


def test_align_same_count_pairs_one_to_one():
    a = [Cue(0, 2, "x"), Cue(2.5, 4, "y")]
    b = [Cue(0.1, 2.1, "X"), Cue(2.4, 4.2, "Y")]
    assert [(r.a_text, r.b_text) for r in align_cues(a, b)] == [("x", "X"), ("y", "Y")]


# -- fake translators -----------------------------------------------------------------------------

class Fake(Translator):
    """Translates by upper-casing; records requests."""
    name = "fake"

    def __init__(self, mangle=False, fail_on=(), unavailable_after=None):
        self.requests = []
        self.mangle, self.fail_on, self.unavailable_after = mangle, set(fail_on), unavailable_after

    def translate_text(self, text, source, target):
        self.requests.append(text)
        if self.unavailable_after is not None and len(self.requests) > self.unavailable_after:
            raise TranslateUnavailable("server went away")
        if any(bad in text for bad in self.fail_on):
            raise TranslateError("refused")
        out = text.upper()
        return out.replace("\n", " ") if self.mangle and "\n" in text else out


def make_cues(n, text="line {}"):
    return [Cue(i * 2.0, i * 2.0 + 1.5, text.format(i)) for i in range(n)]


def test_cues_are_grouped_and_timing_is_kept():
    fake = Fake()
    cues = make_cues(20)
    result = translate_cues(cues, "ru", "en", fake, sleep=lambda s: None)
    assert result.complete and result.translated == 20 and result.fallbacks == 0
    assert [len(r.split("\n")) for r in fake.requests] == [8, 8, 4]
    assert [(c.start, c.end) for c in result.cues] == [(c.start, c.end) for c in cues]
    assert result.cues[5].text == "LINE 5" and cues[5].text == "line 5"  # input untouched


def test_mangled_separator_falls_back_to_one_by_one():
    fake = Fake(mangle=True)
    result = translate_cues(make_cues(10), "ru", "en", fake, sleep=lambda s: None)
    assert result.complete and result.translated == 10 and result.fallbacks == 2
    assert any("one by one" in w for w in result.warnings)
    assert [c.text for c in result.cues][:3] == ["LINE 0", "LINE 1", "LINE 2"]
    assert len(fake.requests) == 2 + 10  # two group attempts, then each cue alone


def test_custom_separator_survives_or_not():
    class Keep(Fake):
        def translate_text(self, text, source, target):
            return super().translate_text(text, source, target)
    result = translate_cues(make_cues(4), "ru", "en", Keep(), separator="\n@@\n", sleep=lambda s: None)
    assert result.translated == 4 and result.fallbacks == 0


def test_empty_cues_are_not_sent_and_stay_empty():
    fake = Fake()
    cues = [Cue(0, 1, "one"), Cue(2, 3, ""), Cue(4, 5, "  \n "), Cue(6, 7, "two")]
    result = translate_cues(cues, "ru", "en", fake, sleep=lambda s: None)
    assert fake.requests == ["one\ntwo"]
    assert [c.text for c in result.cues] == ["ONE", "", "  \n ", "TWO"] and result.complete


def test_multiline_cue_is_flattened_then_wrapped():
    cues = [Cue(0, 4, "first part of a long\nsecond part of the very long sentence continues here")]
    result = translate_cues(cues, "ru", "en", Fake(), sleep=lambda s: None)
    lines = result.cues[0].text.split("\n")
    assert len(lines) == 2 and " ".join(lines) == "FIRST PART OF A LONG SECOND PART OF THE VERY LONG SENTENCE CONTINUES HERE"


def test_size_cap_splits_groups():
    fake = Fake()
    translate_cues(make_cues(6, "x" * 100), "ru", "en", fake, max_chars=250, sleep=lambda s: None)
    assert [len(r.split("\n")) for r in fake.requests] == [2, 2, 2]


def test_a_refused_cue_keeps_its_text_and_is_reported():
    fake = Fake(fail_on=["line 3"])
    result = translate_cues(make_cues(6), "ru", "en", fake, sleep=lambda s: None)
    assert result.failed == [3] and not result.complete and not result.error
    assert result.cues[3].text == "line 3" and result.cues[4].text == "LINE 4"
    assert any("could not be translated" in w for w in result.warnings)


def test_retry_then_success():
    class Flaky(Fake):
        def translate_text(self, text, source, target):
            self.requests.append(text)
            if len(self.requests) < 3:
                raise TranslateError("busy")
            return text.upper()
    fake = Flaky()
    sleeps = []
    result = translate_cues(make_cues(3), "ru", "en", fake, retries=2, sleep=sleeps.append)
    assert result.complete and len(sleeps) == 2


def test_engine_unavailable_stops_without_raising():
    fake = Fake(unavailable_after=1)
    result = translate_cues(make_cues(20), "ru", "en", fake, sleep=lambda s: None)
    assert result.error == "server went away" and not result.complete
    assert result.cues[0].text == "LINE 0" and result.translated == 8  # first group done
    assert result.cues[19].text == "line 19" and 19 in result.failed


def test_cancel_between_requests():
    event = threading.Event()
    fake = Fake()
    seen = []

    def progress(done, total):
        seen.append((done, total))
        if done >= 8:
            event.set()

    result = translate_cues(make_cues(24), "ru", "en", fake, progress=progress, cancel=event, sleep=lambda s: None)
    assert result.cancelled and result.translated == 8 and len(fake.requests) == 1
    assert seen[0] == (0, 24) and seen[-1] == (8, 24)
    result = translate_cues(make_cues(24), "ru", "en", Fake(), cancel=lambda: True)
    assert result.cancelled and result.translated == 0


def test_progress_reaches_total():
    seen = []
    translate_cues(make_cues(10), "ru", "en", Fake(), progress=lambda d, t: seen.append((d, t)))
    assert seen[-1] == (10, 10)


# -- LibreTranslate ----------------------------------------------------------------------------------

class FakeHttp:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def __call__(self, request):
        self.requests.append(request)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return json.dumps(r).encode("utf-8")


def _http_error(code):
    return urllib.error.HTTPError("http://localhost:5000/translate?k=SECRETKEY", code, "x", {}, None)


def test_libretranslate_request_shape_and_reply():
    http = FakeHttp({"translatedText": "Hello, world"})
    t = LibreTranslateTranslator("http://box:5000/", api_key="SECRETKEY", fetch=http)
    assert t.translate_text("Привет, мир", "ru", "en") == "Hello, world"
    req = http.requests[0]
    assert req.full_url == "http://box:5000/translate" and req.get_method() == "POST"
    assert json.loads(req.data.decode("utf-8")) == {
        "q": "Привет, мир", "source": "ru", "target": "en", "format": "text", "api_key": "SECRETKEY"}
    assert "SECRETKEY" not in req.full_url


def test_libretranslate_without_key_sends_none():
    http = FakeHttp({"translatedText": "x"})
    LibreTranslateTranslator(fetch=http).translate_text("a", "ru", "en")
    assert "api_key" not in json.loads(http.requests[0].data.decode("utf-8"))


def test_libretranslate_errors_never_carry_the_key():
    t = LibreTranslateTranslator(api_key="SECRETKEY", fetch=FakeHttp(_http_error(403)))
    with pytest.raises(TranslateUnavailable) as err:
        t.translate_text("a", "ru", "en")
    assert "SECRETKEY" not in str(err.value) and "rejected the API key" in str(err.value)
    t = LibreTranslateTranslator(api_key="SECRETKEY", fetch=FakeHttp(urllib.error.URLError("refused SECRETKEY")))
    with pytest.raises(TranslateUnavailable) as err:
        t.translate_text("a", "ru", "en")
    assert "SECRETKEY" not in str(err.value) and "Could not reach the translation server" in str(err.value)


def test_libretranslate_400_is_a_plain_error_429_retried():
    with pytest.raises(TranslateError) as err:
        LibreTranslateTranslator(fetch=FakeHttp(_http_error(400))).translate_text("a", "xx", "en")
    assert not isinstance(err.value, TranslateUnavailable)
    http = FakeHttp(_http_error(429), {"translatedText": "ok"})
    assert LibreTranslateTranslator(fetch=http, sleep=lambda s: None).translate_text("a", "ru", "en") == "ok"


def test_libretranslate_unusable_reply():
    with pytest.raises(TranslateError, match="unusable"):
        LibreTranslateTranslator(fetch=FakeHttp({"nope": 1})).translate_text("a", "ru", "en")


def test_libretranslate_check_reports_pairs():
    langs = [{"code": "en", "name": "English", "targets": ["ru"]}, {"code": "ru", "name": "Russian", "targets": ["en"]}]
    assert "2 languages" in LibreTranslateTranslator(fetch=FakeHttp(langs)).check()
    assert "ru -> en is available" in LibreTranslateTranslator(fetch=FakeHttp(langs)).check("ru", "en")
    with pytest.raises(TranslateUnavailable, match="no 'de' model"):
        LibreTranslateTranslator(fetch=FakeHttp(langs)).check("de", "en")
    with pytest.raises(TranslateUnavailable, match="Could not reach"):
        LibreTranslateTranslator(fetch=FakeHttp(urllib.error.URLError("down"))).check()


# -- argos-translate command line ------------------------------------------------------------------------

SCRIPT = '''
import sys
args = sys.argv[1:]
src = args[args.index("--from-lang") + 1]
dst = args[args.index("--to-lang") + 1]
text = sys.stdin.buffer.read().decode("utf-8")
if src == "xx":
    sys.stderr.write("No translation path from xx to en\\n")
    sys.exit(2)
sys.stdout.buffer.write(("[" + src + ">" + dst + "] " + text).encode("utf-8"))
'''


def test_argos_cli_with_a_real_tiny_script(tmp_path):
    script = tmp_path / "fake_argos.py"
    script.write_text(SCRIPT, encoding="utf-8")
    t = ArgosCliTranslator([sys.executable, str(script)])
    assert t.translate_text("Привет", "ru", "en") == "[ru>en] Привет"
    result = translate_cues([Cue(0, 1, "привет"), Cue(2, 3, "мир")], "ru", "en", t, wrap_columns=0)
    # the fake keeps the line break, so the two cues went out as one group of two
    assert [c.text for c in result.cues] == ["[ru>en] привет", "мир"] and result.fallbacks == 0
    with pytest.raises(TranslateUnavailable, match="model installed"):
        t.translate_text("x", "xx", "en")
    assert "runs" in t.check("ru", "en")


def test_argos_cli_real_error_text_means_unavailable():
    t = ArgosCliTranslator(runner=lambda *a: (2, "", "usage: argos-translate [-h]\nargos-translate: error: 'es' is not an installed language."))
    with pytest.raises(TranslateUnavailable, match="model installed"):
        t.translate_text("hola", "es", "en")


def test_engine_group_size_hint_is_used():
    seen = []
    t = ArgosCliTranslator(runner=lambda args, text, timeout: (seen.append(text) or 0, text, ""))
    translate_cues(make_cues(70), "ru", "en", t)
    assert [len(x.split("\n")) for x in seen] == [40, 30]


def test_argos_cli_missing_command():
    t = ArgosCliTranslator("definitely-not-a-real-command-xyz")
    with pytest.raises(TranslateUnavailable, match="not found"):
        t.translate_text("x", "ru", "en")


def test_argos_cli_injected_runner_errors():
    t = ArgosCliTranslator(runner=lambda args, text, timeout: (1, "", "boom\nlast line"))
    with pytest.raises(TranslateError, match="last line"):
        t.translate_text("x", "ru", "en")

    def slow(args, text, timeout):
        raise TimeoutError("late")
    with pytest.raises(TranslateError, match="too long"):
        ArgosCliTranslator(runner=slow).translate_text("x", "ru", "en")
    with pytest.raises(TranslateUnavailable):
        ArgosCliTranslator(runner=lambda *a: (0, "x", "")).translate_text("x", "", "en")


# -- language guess ------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, code", [
    ("Когда переехал — не помню\nНаверное, был я бухой", "ru"),
    ("Привіт, як справи? Це їжак і ґанок", "uk"),
    ("Καλημέρα κόσμε, τι κάνεις", "el"),
    ("こんにちは世界 元気ですか", "ja"),
    ("你好世界你好", "zh"),
    ("안녕하세요 여러분", "ko"),
    ("Ich habe das nicht gesehen und die Frau ist mit dem Mann auf der Straße", "de"),
    ("Je ne sais pas pourquoi vous êtes dans la maison avec les enfants", "fr"),
    ("You are not the one that I have been waiting for and the", "en"),
    ("ok", ""),
    ("", ""),
])
def test_detect_language(text, code):
    assert detect_language(text) == code

"""
redactor_common/core/languages.py

One ISO 639 table for the whole family. Each format wants a different
code style for the same language:

- EPUB (dc:language) and ComicInfo.xml (LanguageISO): 2-letter ISO 639-1
  ("de")
- ID3 TLAN (mp3): 3-letter ISO 639-2/T ("deu")
- Matroska's legacy track language: 3-letter ISO 639-2/B ("ger") --
  the bibliographic form differs from /T for about 20 languages

cbz, epub and mp3 each kept their own hand-typed (code, name) list for
the Language quick-pick, drifting apart in both coverage and naming.
They now build those lists from this table in whichever code style
their format needs (see language_pairs()), and can convert a code a
lookup or another tool returned into their own style (see convert()).

A practical shortlist, not the full registry -- every picker in the
family still accepts free text and custom entries.
Pure logic, no Qt dependency.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

CodeStyle = Literal["alpha2", "alpha3", "alpha3b"]


class Language(NamedTuple):
    alpha2: str   # ISO 639-1
    alpha3: str   # ISO 639-2/T (terminology)
    alpha3b: str  # ISO 639-2/B (bibliographic)
    name: str     # English display name


LANGUAGES: tuple[Language, ...] = (
    Language("ar", "ara", "ara", "Arabic"),
    Language("bg", "bul", "bul", "Bulgarian"),
    Language("ca", "cat", "cat", "Catalan"),
    Language("cs", "ces", "cze", "Czech"),
    Language("cy", "cym", "wel", "Welsh"),
    Language("da", "dan", "dan", "Danish"),
    Language("de", "deu", "ger", "German"),
    Language("el", "ell", "gre", "Greek"),
    Language("en", "eng", "eng", "English"),
    Language("es", "spa", "spa", "Spanish"),
    Language("et", "est", "est", "Estonian"),
    Language("eu", "eus", "baq", "Basque"),
    Language("fa", "fas", "per", "Persian"),
    Language("fi", "fin", "fin", "Finnish"),
    Language("fr", "fra", "fre", "French"),
    Language("ga", "gle", "gle", "Irish"),
    Language("he", "heb", "heb", "Hebrew"),
    Language("hi", "hin", "hin", "Hindi"),
    Language("hr", "hrv", "hrv", "Croatian"),
    Language("hu", "hun", "hun", "Hungarian"),
    Language("id", "ind", "ind", "Indonesian"),
    Language("is", "isl", "ice", "Icelandic"),
    Language("it", "ita", "ita", "Italian"),
    Language("ja", "jpn", "jpn", "Japanese"),
    Language("ko", "kor", "kor", "Korean"),
    Language("la", "lat", "lat", "Latin"),
    Language("lt", "lit", "lit", "Lithuanian"),
    Language("lv", "lav", "lav", "Latvian"),
    Language("nb", "nob", "nob", "Norwegian Bokmål"),
    Language("nl", "nld", "dut", "Dutch"),
    Language("nn", "nno", "nno", "Norwegian Nynorsk"),
    Language("no", "nor", "nor", "Norwegian"),
    Language("pl", "pol", "pol", "Polish"),
    Language("pt", "por", "por", "Portuguese"),
    Language("ro", "ron", "rum", "Romanian"),
    Language("ru", "rus", "rus", "Russian"),
    Language("sk", "slk", "slo", "Slovak"),
    Language("sl", "slv", "slv", "Slovenian"),
    Language("sr", "srp", "srp", "Serbian"),
    Language("sv", "swe", "swe", "Swedish"),
    Language("th", "tha", "tha", "Thai"),
    Language("tr", "tur", "tur", "Turkish"),
    Language("uk", "ukr", "ukr", "Ukrainian"),
    Language("vi", "vie", "vie", "Vietnamese"),
    Language("zh", "zho", "chi", "Chinese"),
    Language("", "und", "und", "Undetermined"),
)

_BY_CODE: dict[str, Language] = {}
for _lang in LANGUAGES:
    for _code in (_lang.alpha2, _lang.alpha3, _lang.alpha3b):
        if _code:
            _BY_CODE.setdefault(_code, _lang)


def lookup(code: str) -> Language | None:
    """Finds a language by any of its codes (case-insensitive). A region
    subtag is ignored ("en-GB" -> English)."""
    base = (code or "").strip().lower().replace("_", "-").split("-")[0]
    return _BY_CODE.get(base)


def code_for(language: Language, style: CodeStyle) -> str:
    return getattr(language, style)


def convert(code: str, style: CodeStyle) -> str:
    """Converts a code to `style`; returns it unchanged if unknown or if
    the language has no code in that style."""
    language = lookup(code)
    if language is None:
        return code
    return code_for(language, style) or code


def name_for(code: str) -> str:
    """English display name for a code, or "" if unknown."""
    language = lookup(code)
    return language.name if language else ""


def language_pairs(codes: list[str], style: CodeStyle) -> list[tuple[str, str]]:
    """(code, name) pairs for a project's quick-pick list, in the order
    given, codes rendered in `style`. Unknown codes are skipped."""
    pairs = []
    for code in codes:
        language = lookup(code)
        if language is not None and code_for(language, style):
            pairs.append((code_for(language, style), language.name))
    return pairs

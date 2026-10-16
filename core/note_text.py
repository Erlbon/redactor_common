"""
redactor_common/core/note_text.py

Text helpers for the notes writer (core/notes.py): turning Discogs profile markup and sidecar HTML into plain
Markdown, the credit lines ("Facts from MusicBrainz (CC0)...") and the final italic footer, wiki links and a
safe folder label. Qt-free, no network.
"""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import quote

# ---- Discogs markup ----------------------------------------------------------------------------------------------

_DISAMBIGUATION = re.compile(r"\s+\(\d+\)$")
_NAMED_REF = re.compile(r"\[([a-z])=([^\]]*)\]", re.IGNORECASE)
_ID_REF = re.compile(r"\[[a-z]\d+\]", re.IGNORECASE)
_URL_PAIR = re.compile(r"\[url=([^\]]*)\](.*?)\[/url\]", re.IGNORECASE | re.DOTALL)
_URL_BARE = re.compile(r"\[url\](.*?)\[/url\]", re.IGNORECASE | re.DOTALL)
_URL_DANGLING = re.compile(r"\[/?url(?:=[^\]]*)?\]", re.IGNORECASE)
_SAFE_URL = re.compile(r"^(?:https?://|mailto:)", re.IGNORECASE)


def _named_ref(match: re.Match[str]) -> str:
    kind, value = match.group(1).lower(), match.group(2).strip()
    if value.isdigit():            # [m=123] / [r=123] / [a=123]: an id with no name, nothing to show
        return ""
    if kind in ("a", "l"):
        return _DISAMBIGUATION.sub("", value)
    return value


def _link(url: str, label: str) -> str:
    url, label = url.strip(), label.strip()
    if _SAFE_URL.match(url):
        return f"[{label or url}]({url.replace(' ', '%20').replace(')', '%29')})"
    return label


def clean_discogs_markup(text: str) -> str:
    """Discogs profile text as plain Markdown.

    ``[a=Name (2)]`` / ``[l=Label]`` -> the name (the ' (2)' disambiguation number is dropped); references by
    bare id (``[a112841]``, ``[m=123]``, ``[r=123]``) have no name to show and are removed; ``[url=..]text[/url]``
    -> ``[text](..)`` for http(s)/mailto links (otherwise just the text); ``[b]``/``[i]`` -> ``**``/``*``;
    ``[u]``, ``[color]``, ``[size]`` tags are dropped; line endings become LF, spaces left by removals are
    tidied and runs of blank lines collapse to one."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _URL_PAIR.sub(lambda m: _link(m.group(1), m.group(2)), text)
    text = _URL_BARE.sub(lambda m: _link(m.group(1), m.group(1)), text)
    text = _URL_DANGLING.sub("", text)
    text = _NAMED_REF.sub(_named_ref, text)
    text = _ID_REF.sub("", text)
    text = re.sub(r"\[/?b\]", "**", text, flags=re.IGNORECASE)
    text = re.sub(r"\[/?i\]", "*", text, flags=re.IGNORECASE)
    text = re.sub(r"\[/?(?:u|s|color(?:=[^\]]*)?|size(?:=[^\]]*)?|quote|spoiler|g=[^\]]*)\]", "", text,
                  flags=re.IGNORECASE)
    text = re.sub(r"\*\*\s*\*\*", "", text)               # an emptied bold pair
    return _tidy(text)


def _tidy(text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    out = "\n".join(lines)
    out = re.sub(r"[ \t]+([,.;:!?])", r"\1", out)            # "with , and" left by a removed id reference
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip("\n")


# ---- HTML (OPF descriptions, NFO reviews) --------------------------------------------------------------------------

def html_to_markdown(text: str) -> str:
    """Markdown-ish plain text from the little HTML sidecars carry (Calibre descriptions are escaped HTML):
    paragraphs and <br> become line breaks, <b>/<strong> and <i>/<em> become ``**``/``*``, <li> becomes
    '- ', <a href> becomes a Markdown link for http(s)/mailto, every other tag is dropped, entities are
    decoded. Text without any tag is only entity-decoded and tidied."""
    text = unescape(text) if "&lt;" in text and "<" not in text else text
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|h[1-6]|ul|ol|blockquote|tr)>", "\n\n", text)
    text = re.sub(r"(?i)<li\b[^>]*>", "\n- ", text)
    text = re.sub(r"(?i)</?(b|strong)\b[^>]*>", "**", text)
    text = re.sub(r"(?i)</?(i|em)\b[^>]*>", "*", text)
    text = re.sub(r'(?is)<a\b[^>]*?href\s*=\s*["\']([^"\']*)["\'][^>]*>(.*?)</a>',
                  lambda m: _link(unescape(m.group(1)), re.sub(r"<[^>]+>", "", m.group(2))), text)
    text = re.sub(r"<[^>]*>", "", text)
    text = unescape(text).replace("\xa0", " ")
    return _tidy(text.replace("\r\n", "\n").replace("\r", "\n"))


# ---- credits -------------------------------------------------------------------------------------------------------

def wikipedia_credit(title: str, lang: str = "en") -> str:
    """'Introduction from Wikipedia ([Title](url), CC BY-SA 4.0)' for an article `title` (spaces become
    underscores, brackets and parentheses are percent-encoded so the Markdown link holds)."""
    page = quote(title.strip().replace(" ", "_"), safe="_,.:'!*~-").replace("(", "%28").replace(")", "%29")
    label = title.strip().replace("[", "(").replace("]", ")")
    return f"Introduction from Wikipedia ([{label}](https://{lang}.wikipedia.org/wiki/{page}), CC BY-SA 4.0)"


def musicbrainz_credit() -> str:
    """'Facts from [MusicBrainz](https://musicbrainz.org) (CC0)'."""
    return "Facts from [MusicBrainz](https://musicbrainz.org) (CC0)"


def discogs_credit(kind: str, id: int | str) -> str:
    """'Profile from [Discogs](https://www.discogs.com/<kind>/<id>) (user-contributed)'; `kind` is 'artist',
    'release', 'master' or 'label'."""
    kind = re.sub(r"[^a-z]", "", kind.lower()) or "artist"
    return f"Profile from [Discogs](https://www.discogs.com/{kind}/{quote(str(id).strip(), safe='')}) (user-contributed)"


def credit_footer(sources: list[str], draft: bool = True) -> str:
    """The closing block of a note: a ``---`` line and one italic line, 'Draft.' first (unless draft is False),
    then each source sentence, each ended with a full stop: ``---\\n*Draft. Facts ... (CC0). Introduction ...*``.
    Put a blank line before it. Empty sources give just the Draft line (or '' when not a draft either)."""
    parts = (["Draft."] if draft else []) + [s.strip() if s.strip().endswith(".") else s.strip() + "."
                                             for s in sources if s and s.strip()]
    if not parts:
        return ""
    return "---\n*" + " ".join(parts) + "*"


# ---- assembling a body --------------------------------------------------------------------------------------------

def fact_lines(pairs) -> str:
    """'**Label:** value' lines as Markdown hard breaks, for the non-empty values."""
    return "  \n".join(f"**{label}:** {value}" for label, value in pairs if value)


def join_blocks(*parts: str) -> str:
    """The non-empty parts of a note body separated by one blank line."""
    return "\n\n".join(p.strip("\n") for p in parts if p and p.strip())


# ---- wiki links and labels -------------------------------------------------------------------------------------------

def wiki_link(name: str, label: str = "") -> str:
    """``[[Name]]`` (or ``[[Name|label]]``) with the characters that break a wiki link (``| [ ]``) and line breaks
    removed from the name. An empty name gives ''."""
    clean = re.sub(r"\s+", " ", re.sub(r"[|\[\]]", " ", name)).strip()
    if not clean:
        return ""
    shown = re.sub(r"\s+", " ", re.sub(r"[|\[\]]", " ", label)).strip()
    return f"[[{clean}|{shown}]]" if shown and shown != clean else f"[[{clean}]]"


_WINDOWS_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def safe_folder_label(text: str) -> str:
    """`text` as a name safe for a folder or file on Windows, Linux and macOS: forbidden characters
    (``< > : " / \\ | ? *`` and controls) removed, whitespace collapsed, trailing dots and spaces stripped, a
    reserved device name (CON, NUL...) prefixed with an underscore. Can return '' (nothing usable left)."""
    out = re.sub(r"\s+", " ", _WINDOWS_BAD.sub(" ", text)).strip().rstrip(". ").lstrip()
    if out.split(".")[0].lower() in _RESERVED:
        out = "_" + out
    return out

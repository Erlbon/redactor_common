"""
redactor_common/core/wikipedia.py

A small, generic Wikipedia client for the folder notes (mp3redactor's artist / album notes, videoredactor's series and
film notes): the lead paragraph of an article and what its Wikidata item says about it. What counts as the RIGHT
article for a film or a series is the app's business (its own `looks_like`); for music the rules are here (section
"Music" at the end, folded in from mp3redactor's core/wikipedia_lookup.py so the Folder Redactor can share them).

Network rules: Wikimedia asks for a descriptive User-Agent (app name + version + a URL), so every app builds its own
throttled fetch with `make_fetch(user_agent)` and passes it to the functions here. `fetch` is the injectable
`fetch(url) -> bytes` of lookup_client, so tests never touch the network. A missing page (404) is None, not an error;
an unreachable service raises WikipediaError.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, Iterable, Optional
from urllib.parse import quote, unquote, urlencode, urlsplit

from redactor_common.core.lookup_client import Throttle, fetch_json, make_default_fetch, throttled

SUMMARY_URL = "https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}"
SEARCH_URL = "https://{lang}.wikipedia.org/w/rest.php/v1/search/title"
WIKIDATA_URL = "https://www.wikidata.org/w/api.php"
MIN_REQUEST_INTERVAL = 0.35
MAX_EXTRACT_CHARS = 1200
_SOURCE_NAME = "Wikipedia"


class WikipediaError(Exception):
    """Searching or fetching from Wikipedia failed (not "there is no such page")."""


def make_fetch(user_agent: str, timeout: float = 30.0) -> Callable:
    """The real fetch(url) -> bytes with the app's User-Agent, paced at one request per MIN_REQUEST_INTERVAL for the
    whole process (one pace per call of make_fetch: an app calls it once)."""
    return throttled(make_default_fetch(user_agent, timeout=timeout), Throttle(MIN_REQUEST_INTERVAL))


def _get(url: str, fetch: Callable) -> Optional[dict]:
    return fetch_json(url, fetch, error_cls=WikipediaError, source_name=_SOURCE_NAME, ignore_404=True,
                      retry_429=(2.0, 5.0))


@dataclass
class Page:
    """The lead of one article."""

    title: str
    extract: str = ""
    description: str = ""
    url: str = ""
    kind: str = "standard"  # Wikipedia's page type; "disambiguation" pages are never used
    via: str = ""  # how it was found: "musicbrainz link", "wikidata", "search"

    @property
    def is_disambiguation(self) -> bool:
        return self.kind == "disambiguation"


@dataclass
class SearchHit:
    title: str
    description: str = ""
    excerpt: str = ""


def title_from_url(url: str) -> str:
    """'Vienna_(Ultravox_album)' as 'Vienna (Ultravox album)' from an en.wikipedia.org/wiki/ link, else ''."""
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    if not (host == "wikipedia.org" or host.endswith(".wikipedia.org")) or not parts.path.startswith("/wiki/"):
        return ""
    return unquote(parts.path[len("/wiki/"):]).replace("_", " ").strip()


def language_of_url(url: str) -> str:
    host = (urlsplit(url or "").hostname or "").lower()
    return host.split(".")[0] if host.endswith(".wikipedia.org") else ""


def get_summary(title: str, fetch: Callable, lang: str = "en") -> Optional[Page]:
    """The summary (lead paragraph, short description, link) of the article `title`, or None when there is no
    such article."""
    title = (title or "").strip()
    if not title:
        return None
    url = SUMMARY_URL.format(lang=lang, title=quote(title.replace(" ", "_"), safe="_,.:'!*~-()"))
    data = _get(url, fetch)
    if not isinstance(data, dict) or not data.get("title"):
        return None
    page_url = ((data.get("content_urls") or {}).get("desktop") or {}).get("page") or ""
    return Page(
        title=str(data["title"]), extract=str(data.get("extract") or "").strip(),
        description=str(data.get("description") or "").strip(), url=page_url, kind=str(data.get("type") or "standard"),
    )


def search_titles(query: str, fetch: Callable, limit: int = 5, lang: str = "en") -> list[SearchHit]:
    """The pages whose TITLE matches `query` (REST search/title), best first."""
    if not (query or "").strip():
        return []
    url = SEARCH_URL.format(lang=lang) + "?" + urlencode({"q": query, "limit": limit})
    data = _get(url, fetch) or {}
    hits = []
    for page in data.get("pages", []) if isinstance(data.get("pages"), list) else []:
        if isinstance(page, dict) and page.get("title"):
            hits.append(SearchHit(
                title=str(page["title"]), description=str(page.get("description") or ""),
                excerpt=re.sub(r"<[^>]+>", "", str(page.get("excerpt") or "")),
            ))
    return hits


def title_from_wikidata(entity_id: str, fetch: Callable, lang: str = "en") -> str:
    """The article title of a Wikidata item in `lang` ('' if it has none)."""
    if not re.fullmatch(r"Q\d+", entity_id or ""):
        return ""
    url = WIKIDATA_URL + "?" + urlencode({
        "action": "wbgetentities", "ids": entity_id, "props": "sitelinks", "sitefilter": f"{lang}wiki", "format": "json",
    })
    data = _get(url, fetch) or {}
    entity = (data.get("entities") or {}).get(entity_id) or {}
    return str((entity.get("sitelinks") or {}).get(f"{lang}wiki", {}).get("title") or "")


def wikidata_claims(properties: dict[str, str], title: str = "", entity_id: str = "", fetch: Optional[Callable] = None,
                    lang: str = "en") -> dict:
    """What the Wikidata item of an article (by `title`, one request, or by item id) says: {'qid': ..., <name>: <first
    string value of the property>} for each `properties` entry ({'P345': 'imdb'} gives key 'imdb'); '' for what the item
    does not have. Raises WikipediaError only when the service cannot be reached."""
    found = {"qid": "", **{name: "" for name in properties.values()}}
    params = {"action": "wbgetentities", "props": "claims", "format": "json"}
    if entity_id:
        if not re.fullmatch(r"Q\d+", entity_id):
            return found
        params["ids"] = entity_id
    elif title:
        params.update({"sites": f"{lang}wiki", "titles": title})
    else:
        return found
    data = _get(WIKIDATA_URL + "?" + urlencode(params), fetch) or {}
    for qid, entity in (data.get("entities") or {}).items():
        if not isinstance(entity, dict) or "missing" in entity or not str(qid).startswith("Q"):
            continue
        found["qid"] = str(qid)
        claims = entity.get("claims") or {}
        for prop, key in properties.items():
            for claim in claims.get(prop) or []:
                value = (((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value"))
                if isinstance(value, str) and value.strip():
                    found[key] = value.strip()
                    break
        break
    return found


def qid_by_statement(prop: str, value: str, fetch: Callable) -> str:
    """The Wikidata item that has `prop` = `value` ('P345', 'tt0903747': the item with that IMDb id), or '' when there is
    none or more than one (an identifier that is not unique is not evidence). One request."""
    if not re.fullmatch(r"P\d+", prop or "") or not (value or "").strip():
        return ""
    url = WIKIDATA_URL + "?" + urlencode({
        "action": "query", "list": "search", "srsearch": f"haswbstatement:{prop}={value.strip()}", "srlimit": 2,
        "format": "json",
    })
    data = _get(url, fetch) or {}
    hits = ((data.get("query") or {}).get("search")) or []
    ids = [str(h.get("title")) for h in hits if isinstance(h, dict) and re.fullmatch(r"Q\d+", str(h.get("title") or ""))]
    return ids[0] if len(ids) == 1 else ""


def fold(text: str) -> str:
    """Case-, accent- and punctuation-insensitive form of a title, for comparing names."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold().replace("&", " and ")
    return re.sub(r"[\W_]+", " ", text).strip()


def strip_parenthetical(title: str) -> str:
    """'Vienna (Ultravox album)' -> 'Vienna'."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", title or "")


def trim_extract(text: str, limit: int = MAX_EXTRACT_CHARS) -> str:
    """`text` cut at a sentence end within `limit` characters (whole text when it is shorter)."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    ends = [m.end() for m in re.finditer(r"[.!?](?=\s|$)", cut)]
    return cut[: ends[-1]] if ends else cut.rsplit(" ", 1)[0] + "..."


def usable(page: Optional[Page]) -> Optional[Page]:
    """`page` unless it is missing, a disambiguation page or has no lead."""
    return page if page is not None and not page.is_disambiguation and page.extract else None


# ---------------------------------------------------------------------------
# Music: which article is the band / album / genre (folded in from mp3redactor's core/wikipedia_lookup.py, 2026-11-04)
# ---------------------------------------------------------------------------
#
# Finding the page, in this order:
#   1. the title MusicBrainz links (a "wikipedia" url-relation), or the English sitelink of its "wikidata" relation
#      (the caller passes `linked_title` / `wikidata_id`);
#   2. a search (the REST search endpoint). MusicBrainz often has no link at all ("Vienna (Ultravox album)" exists on
#      Wikipedia but MusicBrainz does not point to it), and the candidate pages found by search are VERIFIED before one
#      is used: the summary must say it is about a band / singer (or an album), name the artist, and, for an artist
#      found only by search, either mention one of the local album titles or carry the artist's name as its own title.
#
# `compare` is the caller's comparable form of a name (default `fold`); the folder notes pass their number-aware one
# ('Seven' = '7'), which is why it is a parameter and not built in.

def music_identifiers(title: str = "", entity_id: str = "", fetch: Optional[Callable] = None, lang: str = "en") -> dict:
    """What a Wikipedia article's Wikidata item says about the artist elsewhere: {'qid', 'musicbrainz' (P434, the
    MusicBrainz artist id), 'discogs' (P1953, the Discogs artist id)}; empty strings for what the item does not have. By
    the article `title` (one request) or by the item id. Raises WikipediaError only when the service cannot be reached."""
    return wikidata_claims({"P434": "musicbrainz", "P1953": "discogs"}, title, entity_id, fetch, lang)


ARTIST_SUFFIXES = ("band", "musician", "singer", "group", "rapper", "duo", "DJ")

_MUSIC_WORDS = re.compile(
    r"\b(band|singer|musician|group|duo|trio|quartet|rapper|songwriter|vocalist|composer|orchestra|ensemble|dj|"
    r"pianist|guitarist|rock|pop|punk|hip hop|new wave|singer-songwriter|recording artist|music project)\b",
    re.IGNORECASE,
)
_ALBUM_WORD = re.compile(r"\b(album|ep|soundtrack|mixtape|compilation|record)\b", re.IGNORECASE)
_GENRE_WORD = re.compile(
    r"\b(music genre|genre|subgenre|style of (?:popular |rock |electronic |dance )?music|musical style|"
    r"music style|musical movement|music movement|form of (?:popular )?music)\b", re.IGNORECASE)
_SINGLE_WORD = re.compile(r"\b(single|song)\b", re.IGNORECASE)


def mentions(text: str, name: str, compare: Callable[[str], str] = fold) -> bool:
    """Is `name` in `text` as whole words, compared in the `compare` form?"""
    name = compare(name)
    return bool(name) and f" {name} " in f" {compare(text)} "


def looks_like_music(page: Page, kind: str, name: str, artist: str = "", context_terms: Iterable[str] = (),
                     compare: Callable[[str], str] = fold) -> bool:
    """Is `page` about the band/singer `name` (kind "artist"), the album `name` by `artist` (kind "album"), the
    soundtrack `name` ("soundtrack") or the music genre `name` ("genre")?

    Artist: a music word in the description or lead, the name in the page title or lead, and -- because many
    bands share a name -- either one of `context_terms` (the local album titles) in the lead or the name as
    the page's own title. Album: the lead or description says "album" (a "single"/"song" does not count), the
    artist is named, the album title is the page title's start or in the lead."""
    if page.is_disambiguation or not page.extract and not page.description:
        return False
    head = f"{page.description}. {page.extract[:700]}"
    title_core = fold(strip_parenthetical(page.title))
    if kind == "artist":
        if not _MUSIC_WORDS.search(head):
            return False
        if not (mentions(page.title, name, compare) or mentions(head, name, compare)):
            return False
        if title_core == fold(name):
            return True
        return any(mentions(page.extract, term, compare) for term in context_terms if term)
    if kind == "soundtrack":
        # the soundtrack's own article, or the film / game / series it belongs to
        if not re.search(r"\b(soundtrack|score|album|film|movie|video game|game|television|series|musical)\b", head,
                         re.IGNORECASE):
            return False
        return title_core.startswith(fold(name)) or fold(name).startswith(title_core) and len(title_core) > 3
    if kind == "genre":
        # about a MUSIC genre: the lead says genre/style of music, and the name is the page's title ("New wave
        # music") or named in its lead
        if not _GENRE_WORD.search(head):
            return False
        return title_core in (fold(name), fold(name) + " music") or mentions(page.title, name, compare)
    # album
    if not _ALBUM_WORD.search(head):
        return False
    if _SINGLE_WORD.search(page.description) and not _ALBUM_WORD.search(page.description):
        return False
    if artist and not (mentions(head, artist, compare) or mentions(page.title, artist, compare)):
        return False
    return title_core == fold(name) or mentions(head, name, compare)


def find_music_page(
    kind: str, name: str, *, fetch: Callable, artist: str = "", linked_title: str = "", wikidata_id: str = "",
    context_terms: Iterable[str] = (), lang: str = "en", notes: Optional[list[str]] = None,
    compare: Callable[[str], str] = fold,
) -> Optional[Page]:
    """The Wikipedia page for an "artist" (`name` = the band), an "album" (`name` = its title, `artist` = who made it),
    a "soundtrack" or a "genre", or None. `linked_title` / `wikidata_id` come from MusicBrainz (tried first, trusted
    unless the page is a disambiguation page); then the search with verification. `notes` collects what happened, for
    the proposal's reasons. Raises WikipediaError only when Wikipedia cannot be reached."""
    notes = notes if notes is not None else []
    context_terms = [t for t in context_terms if t]
    if linked_title:
        page = usable(get_summary(linked_title, fetch, lang))
        if page:
            page.via = "musicbrainz link"
            return page
        notes.append(f"the Wikipedia link in MusicBrainz ({linked_title}) gave no usable page")
    if wikidata_id:
        title = title_from_wikidata(wikidata_id, fetch, lang)
        page = usable(get_summary(title, fetch, lang)) if title else None
        if page:
            page.via = "wikidata"
            return page
    if kind == "album":
        queries = [f"{name} ({artist} album)", f"{name} (album)", f"{name} {artist} album".strip()]
    elif kind == "genre":
        queries = [f"{name} music", name, f"{name} (genre)"]
    elif kind == "soundtrack":
        queries = [f"{name} soundtrack", f"{name} (soundtrack)", name]
    else:
        queries = [name, f"{name} band"]
    seen: set[str] = set()
    checked = 0
    for query in queries:
        for hit in search_titles(query, fetch, limit=6, lang=lang):
            if hit.title in seen or checked >= 5:
                continue
            seen.add(hit.title)
            blurb = f"{hit.description} {hit.excerpt}"
            if kind == "album" and not (_ALBUM_WORD.search(blurb) or re.search(r"album", hit.title, re.IGNORECASE)):
                continue  # a song, a film...: not worth a request
            if kind == "genre" and not (_GENRE_WORD.search(blurb) or fold(hit.title).endswith(("music", "genre"))
                                        or fold(hit.title) == fold(name)):
                continue
            if kind == "artist" and _SINGLE_WORD.search(hit.description) and not _MUSIC_WORDS.search(hit.description):
                continue
            checked += 1
            page = usable(get_summary(hit.title, fetch, lang))
            if page and looks_like_music(page, kind, name, artist, context_terms, compare):
                page.via = "search"
                return page
        if checked >= 5:
            break
    notes.append("no Wikipedia page was found and verified")
    return None


def find_artist_page(name: str, context_terms: Iterable[str], fetch: Callable, lang: str = "en",
                     notes: Optional[list[str]] = None, compare: Callable[[str], str] = fold) -> Optional[Page]:
    """The Wikipedia article of a band / musician whose plain name is shared with a city, a film or another act:
    'Leningrad (band)', 'X (musician)', 'X (singer)'... tried as the article title, then the usual search. Each page must
    pass the same check as any artist page (a music word in its lead, the name in its title or lead, and the name as the
    title's own or a local album named in the lead). None when no such page is found."""
    notes = notes if notes is not None else []
    context_terms = [t for t in context_terms if t]
    for suffix in ARTIST_SUFFIXES[:3]:
        page = usable(get_summary(f"{name} ({suffix})", fetch, lang))
        if page and looks_like_music(page, "artist", name, "", context_terms, compare):
            page.via = f"the title '{name} ({suffix})'"
            return page
    return find_music_page("artist", name, context_terms=context_terms, fetch=fetch, lang=lang, notes=notes,
                           compare=compare)

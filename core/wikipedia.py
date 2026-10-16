"""
redactor_common/core/wikipedia.py

A small, generic Wikipedia client for the folder notes (mp3redactor's artist / album notes, videoredactor's series and
film notes): the lead paragraph of an article and what its Wikidata item says about it. What counts as the RIGHT
article for a band, a film or a series is the app's business (each app has its own `looks_like`); this module only
fetches and parses.

Network rules: Wikimedia asks for a descriptive User-Agent (app name + version + a URL), so every app builds its own
throttled fetch with `make_fetch(user_agent)` and passes it to the functions here. `fetch` is the injectable
`fetch(url) -> bytes` of lookup_client, so tests never touch the network. A missing page (404) is None, not an error;
an unreachable service raises WikipediaError.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, Optional
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
    if not host.endswith("wikipedia.org") or not parts.path.startswith("/wiki/"):
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

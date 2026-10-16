"""
redactor_common/core/source_ids.py -- the shared SOURCE-ID vocabulary of the four Redactor apps.

A "source reference" says WHICH record in WHICH outside database a file, album, book or comic was matched to:
(service, kind, id). The apps store it inside the file (tags, OPF identifiers, ComicInfo fields), show it as a
clickable link, and use it to skip the search next time. Qt-free, no network, pure functions.

    SourceRef("tmdb", "movie", "603")        # service, kind, id; validated and canonicalised on construction
    parse_link("https://www.themoviedb.org/movie/603-the-matrix")  -> SourceRef("tmdb", "movie", "603") | None
    build_link(ref)                          -> "https://www.themoviedb.org/movie/603"
    normalise_id("imdb", "title", " TT0133093 ")  -> "tt0133093"     (raises SourceIdError when it is not one)
    wire(ref) / from_wire(text)              -> "tmdb:movie:603" (one line, safe to store in a tag) / SourceRef | None
    format_label("tmdb")                     -> "TMDB"

Kinds and id shapes (the id is stored WITHOUT any slug; a slug is not an id):
    musicbrainz  artist, release-group, release, recording, label   UUID (lower case)
    discogs      artist, release, master                            digits
    wikipedia    article                                            "lang:Title" (title with spaces, not underscores)
    wikidata     item                                               Q<digits>
    theporndb    scene, performer, site, movie, jav                 UUID (the API's "id") or a slug (the API accepts
                                                                    "slug, id or uuid" as its identifier; verified in
                                                                    https://api.theporndb.net/openapi.json, 2026-10-16)
    stashdb      scene, performer, studio                           UUID
    tmdb         movie, tv, person                                  digits
                 season                                             "<tv id>/<season>"          e.g. 1396/1
                 episode                                            "<tv id>/<season>/<episode>" e.g. 1396/1/2
    imdb         title, name                                        tt<digits>, nm<digits>
    tvdb         series, movie, episode                             digits (links built through TheTVDB's
                                                                    "dereferrer", which redirects by numeric id)
    openlibrary  edition, work, author                              OL<digits>M / OL<digits>W / OL<digits>A
    googlebooks  volume                                             12 characters [A-Za-z0-9_-]
    isfdb        title, publication, author, series, publisher      digits
    comicvine    issue, volume                                      4000-<digits>, 4050-<digits> (ComicVine's own
                                                                    type-prefixed ids; a bare number is accepted and
                                                                    prefixed)
    gcd          issue, series                                      digits (Grand Comics Database, comics.org)

parse_link() is a SUPERSET of mp3redactor's core/notes_link.py parse_link(): same hosts (musicbrainz.org, discogs.com,
wikipedia.org, wikidata.org and any sub-domain of them), same scheme-less "host/path" form, same bare-MusicBrainz-id
form (bare_as=), same lang from the Wikipedia sub-domain, same Discogs "search anywhere in the path" tolerance, same
Wikidata fallbacks, same plain-language messages (explain_link() raises LinkError with them) -- plus the other
services and the extra MusicBrainz/Discogs kinds. Differences, deliberately stricter: a MusicBrainz id must be a real
UUID (mp3 accepted any 36 hex-and-hyphen text), test.musicbrainz.org (a different database) is refused, and a port
other than 80/443, a user-info part or a non-ASCII host is refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import parse_qs, quote, unquote, urlsplit

__all__ = [
    "SourceIdError", "LinkError", "SourceRef", "SERVICES", "ALLOWED_HOSTS", "ALLOWED_TEXT",
    "services", "kinds", "is_known", "format_label", "format_kind_label", "describe",
    "normalise_id", "is_valid_id", "make_ref", "build_link", "parse_link", "explain_link", "wire", "from_wire",
]

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_DIGITS = re.compile(r"^[1-9][0-9]{0,17}$")
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_LINK = 2048
_MAX_ID = 512


class SourceIdError(ValueError):
    """An id (or a service/kind) that is not valid. The message is plain words."""


class LinkError(SourceIdError):
    """The text is not a link the app can use; the message says why in plain words."""


# ---------------------------------------------------------------- id validators (each returns the canonical id)

def _uuid(text: str) -> str:
    t = text.strip().lower()
    if not _UUID.match(t):
        raise SourceIdError("not a UUID")
    return t


def _digits(text: str) -> str:
    t = text.strip()
    if not t.isascii() or not _DIGITS.match(t):
        raise SourceIdError("not a number")
    return t


def _digits_or_zero(text: str) -> str:
    t = text.strip()
    return "0" if t == "0" else _digits(t)


def _uuid_or_slug(text: str) -> str:
    t = text.strip()
    if _UUID.match(t.lower()):
        return t.lower()
    t = t.lower()
    if _SLUG.match(t) and len(t) <= 255:
        return t
    raise SourceIdError("not a UUID or a slug")


def _prefixed_digits(prefix: str) -> Callable[[str], str]:
    def check(text: str) -> str:
        t = text.strip().lower()
        if t.startswith(prefix) and t[len(prefix):].isascii() and t[len(prefix):].isdigit() and 1 <= len(t) - len(prefix) <= 10:
            return t
        raise SourceIdError(f"not a {prefix}<digits> id")
    return check


def _wikidata(text: str) -> str:
    t = text.strip().upper()
    if re.fullmatch(r"Q[1-9][0-9]{0,15}", t):
        return t
    raise SourceIdError("not a Q<digits> id")


def _wikipedia(text: str) -> str:
    t = text.strip()
    lang, sep, title = t.partition(":")
    lang = lang.lower()
    if not sep or not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{1,8})*|simple", lang):
        raise SourceIdError("not 'lang:Title'")
    title = re.sub(r"\s+", " ", title.replace("_", " ")).strip()
    if not title or len(title) > 255 or re.search(r"[\x00-\x1f\x7f#<>\[\]{|}]", title) or title.lower().startswith("special:"):
        raise SourceIdError("not an article title")
    return f"{lang}:{title}"


def _openlibrary(letter: str) -> Callable[[str], str]:
    def check(text: str) -> str:
        t = text.strip().upper()
        if re.fullmatch(r"OL[1-9][0-9]{0,12}" + letter, t):
            return t
        raise SourceIdError(f"not an OL<digits>{letter} id")
    return check


def _googlebooks(text: str) -> str:
    t = text.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{12}", t):
        return t
    raise SourceIdError("not a 12-character Google Books id")


def _comicvine(prefix: str) -> Callable[[str], str]:
    def check(text: str) -> str:
        t = text.strip()
        if re.fullmatch(r"[1-9][0-9]{0,12}", t):
            return f"{prefix}-{t}"
        match = re.fullmatch(prefix + r"-([1-9][0-9]{0,12})", t)
        if match:
            return t
        raise SourceIdError(f"not a {prefix}-<number> id")
    return check


def _tmdb_season(text: str) -> str:
    parts = text.strip().split("/")
    if len(parts) != 2:
        raise SourceIdError("not '<tv id>/<season>'")
    return f"{_digits(parts[0])}/{_digits_or_zero(parts[1])}"


def _tmdb_episode(text: str) -> str:
    parts = text.strip().split("/")
    if len(parts) != 3:
        raise SourceIdError("not '<tv id>/<season>/<episode>'")
    return f"{_digits(parts[0])}/{_digits_or_zero(parts[1])}/{_digits_or_zero(parts[2])}"


@dataclass(frozen=True)
class _Kind:
    label: str
    check: Callable[[str], str]
    link: Callable[[str], str]  # canonical id -> https link


def _q(text: str) -> str:
    return quote(text, safe="")


def _wikipedia_link(ident: str) -> str:
    lang, _, title = ident.partition(":")
    return f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'), safe='()_,:;@$!*')}"


def _tmdb_link(kind: str) -> Callable[[str], str]:
    def build(ident: str) -> str:
        if kind == "season":
            tv, season = ident.split("/")
            return f"https://www.themoviedb.org/tv/{tv}/season/{season}"
        if kind == "episode":
            tv, season, episode = ident.split("/")
            return f"https://www.themoviedb.org/tv/{tv}/season/{season}/episode/{episode}"
        return f"https://www.themoviedb.org/{kind}/{ident}"
    return build


def _simple(template: str) -> Callable[[str], str]:
    return lambda ident: template.format(id=_q(ident))


SERVICES: dict[str, dict] = {
    "musicbrainz": {"label": "MusicBrainz", "kinds": {
        "artist": _Kind("Artist", _uuid, _simple("https://musicbrainz.org/artist/{id}")),
        "release-group": _Kind("Release group", _uuid, _simple("https://musicbrainz.org/release-group/{id}")),
        "release": _Kind("Release", _uuid, _simple("https://musicbrainz.org/release/{id}")),
        "recording": _Kind("Recording", _uuid, _simple("https://musicbrainz.org/recording/{id}")),
        "label": _Kind("Label", _uuid, _simple("https://musicbrainz.org/label/{id}")),
    }},
    "discogs": {"label": "Discogs", "kinds": {
        "artist": _Kind("Artist", _digits, _simple("https://www.discogs.com/artist/{id}")),
        "release": _Kind("Release", _digits, _simple("https://www.discogs.com/release/{id}")),
        "master": _Kind("Master release", _digits, _simple("https://www.discogs.com/master/{id}")),
    }},
    "wikipedia": {"label": "Wikipedia", "kinds": {
        "article": _Kind("Article", _wikipedia, _wikipedia_link),
    }},
    "wikidata": {"label": "Wikidata", "kinds": {
        "item": _Kind("Item", _wikidata, _simple("https://www.wikidata.org/wiki/{id}")),
    }},
    "theporndb": {"label": "ThePornDB", "kinds": {
        "scene": _Kind("Scene", _uuid_or_slug, _simple("https://theporndb.net/scenes/{id}")),
        "performer": _Kind("Performer", _uuid_or_slug, _simple("https://theporndb.net/performers/{id}")),
        "site": _Kind("Site", _uuid_or_slug, _simple("https://theporndb.net/sites/{id}")),
        "movie": _Kind("Movie", _uuid_or_slug, _simple("https://theporndb.net/movies/{id}")),
        "jav": _Kind("JAV", _uuid_or_slug, _simple("https://theporndb.net/jav/{id}")),
    }},
    "stashdb": {"label": "StashDB", "kinds": {
        "scene": _Kind("Scene", _uuid, _simple("https://stashdb.org/scenes/{id}")),
        "performer": _Kind("Performer", _uuid, _simple("https://stashdb.org/performers/{id}")),
        "studio": _Kind("Studio", _uuid, _simple("https://stashdb.org/studios/{id}")),
    }},
    "tmdb": {"label": "TMDB", "kinds": {
        "movie": _Kind("Movie", _digits, _tmdb_link("movie")),
        "tv": _Kind("TV series", _digits, _tmdb_link("tv")),
        "season": _Kind("Season", _tmdb_season, _tmdb_link("season")),
        "episode": _Kind("Episode", _tmdb_episode, _tmdb_link("episode")),
        "person": _Kind("Person", _digits, _tmdb_link("person")),
    }},
    "imdb": {"label": "IMDb", "kinds": {
        "title": _Kind("Title", _prefixed_digits("tt"), _simple("https://www.imdb.com/title/{id}/")),
        "name": _Kind("Name", _prefixed_digits("nm"), _simple("https://www.imdb.com/name/{id}/")),
    }},
    "tvdb": {"label": "TheTVDB", "kinds": {
        "series": _Kind("Series", _digits, _simple("https://thetvdb.com/dereferrer/series/{id}")),
        "movie": _Kind("Movie", _digits, _simple("https://thetvdb.com/dereferrer/movie/{id}")),
        "episode": _Kind("Episode", _digits, _simple("https://thetvdb.com/dereferrer/episode/{id}")),
    }},
    "openlibrary": {"label": "Open Library", "kinds": {
        "edition": _Kind("Edition", _openlibrary("M"), _simple("https://openlibrary.org/books/{id}")),
        "work": _Kind("Work", _openlibrary("W"), _simple("https://openlibrary.org/works/{id}")),
        "author": _Kind("Author", _openlibrary("A"), _simple("https://openlibrary.org/authors/{id}")),
    }},
    "googlebooks": {"label": "Google Books", "kinds": {
        "volume": _Kind("Volume", _googlebooks, _simple("https://books.google.com/books?id={id}")),
    }},
    "isfdb": {"label": "ISFDB", "kinds": {
        "title": _Kind("Title", _digits, _simple("https://www.isfdb.org/cgi-bin/title.cgi?{id}")),
        "publication": _Kind("Publication", _digits, _simple("https://www.isfdb.org/cgi-bin/pl.cgi?{id}")),
        "author": _Kind("Author", _digits, _simple("https://www.isfdb.org/cgi-bin/ea.cgi?{id}")),
        "series": _Kind("Series", _digits, _simple("https://www.isfdb.org/cgi-bin/seriesgrid.cgi?{id}")),
        "publisher": _Kind("Publisher", _digits, _simple("https://www.isfdb.org/cgi-bin/publisher.cgi?{id}")),
    }},
    "comicvine": {"label": "Comic Vine", "kinds": {
        "issue": _Kind("Issue", _comicvine("4000"), _simple("https://comicvine.gamespot.com/issue/{id}/")),
        "volume": _Kind("Volume", _comicvine("4050"), _simple("https://comicvine.gamespot.com/volume/{id}/")),
    }},
    "gcd": {"label": "Grand Comics Database", "kinds": {
        "issue": _Kind("Issue", _digits, _simple("https://www.comics.org/issue/{id}/")),
        "series": _Kind("Series", _digits, _simple("https://www.comics.org/series/{id}/")),
    }},
}


# ---------------------------------------------------------------- vocabulary helpers

def services() -> list[str]:
    return list(SERVICES)


def kinds(service: str) -> list[str]:
    return list(SERVICES[service]["kinds"]) if service in SERVICES else []


def is_known(service: str, kind: str) -> bool:
    return service in SERVICES and kind in SERVICES[service]["kinds"]


def format_label(service: str) -> str:
    """The service's display name for the UI ('tmdb' -> 'TMDB'); an unknown service comes back as it was given."""
    return SERVICES[service]["label"] if service in SERVICES else str(service)


def format_kind_label(service: str, kind: str) -> str:
    return SERVICES[service]["kinds"][kind].label if is_known(service, kind) else str(kind)


def _kind(service: object, kind: object) -> _Kind:
    if not isinstance(service, str) or service not in SERVICES:
        raise SourceIdError(f"Unknown service: {service!r}.")
    if not isinstance(kind, str) or kind not in SERVICES[service]["kinds"]:
        raise SourceIdError(f"{format_label(service)} has no kind {kind!r} (it has: {', '.join(kinds(service))}).")
    return SERVICES[service]["kinds"][kind]


def normalise_id(service: str, kind: str, text: str) -> str:
    """The canonical id for `text` (case, spaces, prefix, zero rules per service); SourceIdError when it is not one."""
    spec = _kind(service, kind)
    if not isinstance(text, str):
        raise SourceIdError("An id must be text.")
    if len(text) > _MAX_ID or re.search(r"[\x00-\x1f\x7f]", text):
        raise SourceIdError(f"Not a valid {format_label(service)} {format_kind_label(service, kind).lower()} id.")
    try:
        return spec.check(text)
    except SourceIdError as err:
        raise SourceIdError(
            f"Not a valid {format_label(service)} {format_kind_label(service, kind).lower()} id: {err}.") from None


def is_valid_id(service: str, kind: str, text: str) -> bool:
    try:
        normalise_id(service, kind, text)
    except SourceIdError:
        return False
    return True


@dataclass(frozen=True)
class SourceRef:
    """(service, kind, id), validated and canonicalised on construction: an invalid one cannot exist."""
    service: str
    kind: str
    id: str

    def __post_init__(self):
        object.__setattr__(self, "id", normalise_id(self.service, self.kind, self.id))

    @property
    def lang(self) -> str:
        """Wikipedia only: the language code ('en'); empty for every other service."""
        return self.id.partition(":")[0] if self.service == "wikipedia" else ""

    @property
    def title(self) -> str:
        """Wikipedia only: the article title; empty for every other service."""
        return self.id.partition(":")[2] if self.service == "wikipedia" else ""

    def __str__(self) -> str:
        return wire(self)


def make_ref(service: str, kind: str, text: str) -> Optional[SourceRef]:
    """SourceRef(service, kind, text), or None when it is not valid (for readers that must not crash)."""
    try:
        return SourceRef(service, kind, text)
    except SourceIdError:
        return None


def build_link(ref: SourceRef) -> str:
    """The https link to the record (no slug; always the service's own address)."""
    return _kind(ref.service, ref.kind).link(ref.id)


# ---------------------------------------------------------------- wire form

def wire(ref: SourceRef) -> str:
    """One line 'service:kind:id' (no line breaks, ';' free of meaning here), stable and safe to store in a tag."""
    return f"{ref.service}:{ref.kind}:{ref.id}"


def from_wire(text: str) -> Optional[SourceRef]:
    """The SourceRef a wire() text names, or None for anything that is not one (never raises)."""
    if not isinstance(text, str) or len(text) > _MAX_ID + 40 or re.search(r"[\x00-\x1f\x7f]", text):
        return None
    parts = text.strip().split(":", 2)
    if len(parts) != 3:
        return None
    return make_ref(parts[0].strip().lower(), parts[1].strip().lower(), parts[2])


# ---------------------------------------------------------------- links

ALLOWED_HOSTS = (
    "musicbrainz.org", "discogs.com", "wikipedia.org", "wikidata.org", "theporndb.net", "stashdb.org",
    "themoviedb.org", "imdb.com", "thetvdb.com", "openlibrary.org", "books.google.*", "isfdb.org",
    "comicvine.gamespot.com", "comics.org",
)
ALLOWED_TEXT = ("musicbrainz.org, discogs.com, wikipedia.org, wikidata.org, theporndb.net, stashdb.org, "
                "themoviedb.org, imdb.com, thetvdb.com, openlibrary.org, books.google.com, isfdb.org, "
                "comicvine.gamespot.com or comics.org")
_DOMAINS = tuple(h for h in ALLOWED_HOSTS if "*" not in h)
_GOOGLE_BOOKS = re.compile(r"^(?:www\.)?books\.google\.(?:com|[a-z]{2,3}|co\.[a-z]{2}|com\.[a-z]{2})$")
_REFUSED_HOSTS = {"test.musicbrainz.org"}
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _site(host: str) -> str:
    """Which allowlisted site `host` belongs to ('' when none): the domain itself or a sub-domain of it."""
    if host in _REFUSED_HOSTS or not all(_HOST_LABEL.match(label) for label in host.split(".")):
        return ""
    if _GOOGLE_BOOKS.match(host):
        return "books.google"
    for domain in _DOMAINS:
        if host == domain or host.endswith("." + domain):
            return domain
    return ""


def _ref(service: str, kind: str, ident: str) -> SourceRef:
    try:
        return SourceRef(service, kind, ident)
    except SourceIdError as err:
        raise LinkError(f"That {format_label(service)} link has an id I cannot use ({err}).") from None


def explain_link(text: str, bare_as: Optional[tuple[str, str]] = None) -> SourceRef:
    """The SourceRef a pasted link names; LinkError (plain words) when it is not one. `bare_as=('musicbrainz',
    'release-group')` makes a bare id (a UUID) count as that kind, like mp3redactor's 'Look up by link'."""
    raw = text.strip() if isinstance(text, str) else ""
    if not raw:
        raise LinkError("Paste a link from one of the supported sites first.")
    if len(raw) > _MAX_LINK or re.search(r"[\x00-\x20\x7f]", raw):
        raise LinkError("That is not a link I can use (it has spaces, line breaks or is far too long).")
    if bare_as is not None and _UUID.match(raw.lower()):
        return _ref(bare_as[0], bare_as[1], raw)
    if "://" in raw:
        candidate = raw
    elif re.match(r"^[\w.-]+\.[a-z]{2,}/", raw, re.IGNORECASE):
        candidate = "https://" + raw
    else:
        raise LinkError(f"That is not a link I can use. Paste a link from {ALLOWED_TEXT}.")
    try:
        parts = urlsplit(candidate)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        raise LinkError("That is not a link I can use.") from None
    if parts.scheme not in ("http", "https") or "@" in parts.netloc or chr(92) in parts.netloc \
            or port not in (None, 80, 443) or not host.isascii():
        raise LinkError(f"Only links from {ALLOWED_TEXT} are used: {host or raw} is not one of them.")
    site = _site(host)
    if not site:
        raise LinkError(f"Only links from {ALLOWED_TEXT} are used: {host} is not one of them.")
    path = unquote(parts.path)
    if re.search(r"[\x00-\x1f\x7f]", path):
        raise LinkError("That is not a link I can use.")
    return _PARSERS[site](host, path, parts.query)


def _p_musicbrainz(host: str, path: str, query: str) -> SourceRef:
    match = re.match(r"^/(artist|release-group|release|recording|label)/([0-9a-fA-F-]{36})(?![0-9a-zA-Z-])", path)
    if not match:
        raise LinkError("That MusicBrainz link is not an artist, release group, release, recording or label page "
                        "(it looks like .../artist/<id> or .../release-group/<id>).")
    return _ref("musicbrainz", match.group(1), match.group(2))


def _p_discogs(host: str, path: str, query: str) -> SourceRef:
    match = re.search(r"/(artist|release|master)/(?:view/)?(\d+)", path)
    if not match:
        raise LinkError("That Discogs link is not an artist, release or master page (.../artist/<number>-Name).")
    return _ref("discogs", match.group(1), match.group(2))


def _p_wikidata(host: str, path: str, query: str) -> SourceRef:
    match = re.search(r"/(?:wiki|entity)/(?:Special:EntityData/)?(Q\d+)", path) or re.match(r"^.*?(Q\d+)$", path)
    if not match:
        raise LinkError("That Wikidata link has no item number (.../wiki/Q123).")
    return _ref("wikidata", "item", match.group(1))


def _p_wikipedia(host: str, path: str, query: str) -> SourceRef:
    sub = host[: -len("wikipedia.org")].rstrip(".")
    if sub.endswith(".m"):
        sub = sub[:-2]
    elif sub == "m":
        sub = ""
    if not sub or "." in sub or sub == "www":
        raise LinkError("That Wikipedia link names no language edition (it looks like https://en.wikipedia.org/wiki/Title).")
    match = re.match(r"^/wiki/(.+)$", path)
    title = match.group(1) if match else ""
    if not match and path in ("/w/index.php", "/index.php"):
        values = parse_qs(query).get("title", [])
        title = values[0] if values else ""
    if not title:
        raise LinkError("That Wikipedia link is not an article (.../wiki/Title).")
    return _ref("wikipedia", "article", f"{sub}:{title}")


def _p_theporndb(host: str, path: str, query: str) -> SourceRef:
    match = re.match(r"^/(scenes|performers|sites|movies|jav)/([^/?#]+)/?$", path)
    if not match:
        raise LinkError("That ThePornDB link is not a scene, performer, site or movie page (.../scenes/<id>).")
    kind = {"scenes": "scene", "performers": "performer", "sites": "site", "movies": "movie", "jav": "jav"}[match.group(1)]
    return _ref("theporndb", kind, match.group(2))


def _p_stashdb(host: str, path: str, query: str) -> SourceRef:
    match = re.match(r"^/(scenes|performers|studios)/([0-9a-fA-F-]{36})(?![0-9a-zA-Z-])", path)
    if not match:
        raise LinkError("That StashDB link is not a scene, performer or studio page (.../scenes/<id>).")
    return _ref("stashdb", {"scenes": "scene", "performers": "performer", "studios": "studio"}[match.group(1)], match.group(2))


def _p_tmdb(host: str, path: str, query: str) -> SourceRef:
    path = re.sub(r"^/[a-z]{2}(?:-[A-Za-z]{2})?(?=/(?:movie|tv|person)/)", "", path)
    match = re.match(r"^/tv/(\d+)(?:-[^/]*)?/season/(\d+)(?:/episode/(\d+))?(?:/|$)", path)
    if match:
        if match.group(3) is not None:
            return _ref("tmdb", "episode", f"{match.group(1)}/{match.group(2)}/{match.group(3)}")
        return _ref("tmdb", "season", f"{match.group(1)}/{match.group(2)}")
    match = re.match(r"^/(movie|tv|person)/(\d+)(?:-[^/]*)?(?:/|$)", path)
    if not match:
        raise LinkError("That TMDB link is not a movie, TV, season, episode or person page (.../movie/<number>-name).")
    return _ref("tmdb", match.group(1), match.group(2))


def _p_imdb(host: str, path: str, query: str) -> SourceRef:
    match = re.match(r"^(?:/[a-z]{2}(?:-[a-z]{2})?)?/(title|name)/((?:tt|nm)\d+)(?:/|$)", path, re.IGNORECASE)
    if not match:
        raise LinkError("That IMDb link is not a title or name page (.../title/tt1234567/).")
    return _ref("imdb", match.group(1).lower(), match.group(2))


def _p_tvdb(host: str, path: str, query: str) -> SourceRef:
    match = re.match(r"^/dereferrer/(series|movie|episode)/(\d+)/?$", path)
    if match:
        return _ref("tvdb", match.group(1), match.group(2))
    match = re.match(r"^/series/[^/]+/episodes/(\d+)/?$", path)
    if match:
        return _ref("tvdb", "episode", match.group(1))
    match = re.match(r"^/(series|movies)/(\d+)/?$", path)
    if match:
        return _ref("tvdb", "series" if match.group(1) == "series" else "movie", match.group(2))
    values = parse_qs(query)
    if path in ("/", "/index.php") and values.get("tab", [""])[0] in ("series", "episode") and values.get("id"):
        return _ref("tvdb", "series" if values["tab"][0] == "series" else "episode", values["id"][0])
    raise LinkError("That TheTVDB link has no numeric id (a link that only has a name, like /series/the-office, "
                    "cannot be used; the id appears in links like .../dereferrer/series/12345).")


def _p_openlibrary(host: str, path: str, query: str) -> SourceRef:
    match = re.match(r"^/(books|works|authors)/(OL\d+[MWA])(?:/|$)", path, re.IGNORECASE)
    if not match:
        raise LinkError("That Open Library link is not a book, work or author page (.../works/OL123W).")
    return _ref("openlibrary", {"books": "edition", "works": "work", "authors": "author"}[match.group(1).lower()], match.group(2))


def _p_googlebooks(host: str, path: str, query: str) -> SourceRef:
    values = parse_qs(query).get("id", [])
    if not path.startswith("/books") or not values:
        raise LinkError("That Google Books link has no book id (it looks like https://books.google.com/books?id=...).")
    return _ref("googlebooks", "volume", values[0])


def _p_isfdb(host: str, path: str, query: str) -> SourceRef:
    match = re.match(r"^/cgi-bin/(title|pl|ea|seriesgrid|publisher)\.cgi$", path)
    ident = query.split("&")[0] if query else ""
    if not match or not ident:
        raise LinkError("That ISFDB link is not a title, publication, author, series or publisher page "
                        "(.../cgi-bin/title.cgi?12345).")
    kind = {"title": "title", "pl": "publication", "ea": "author", "seriesgrid": "series", "publisher": "publisher"}[match.group(1)]
    return _ref("isfdb", kind, ident)


def _p_comicvine(host: str, path: str, query: str) -> SourceRef:
    match = re.search(r"/(4000|4050)-(\d+)(?:/|$)", path)
    if not match:
        raise LinkError("That Comic Vine link is not an issue or volume page (.../4000-12345/ or .../4050-678/).")
    return _ref("comicvine", "issue" if match.group(1) == "4000" else "volume", f"{match.group(1)}-{match.group(2)}")


def _p_gcd(host: str, path: str, query: str) -> SourceRef:
    match = re.match(r"^/(issue|series)/(\d+)(?:/|$)", path)
    if not match:
        raise LinkError("That Grand Comics Database link is not an issue or series page (.../issue/12345/).")
    return _ref("gcd", match.group(1), match.group(2))


_PARSERS: dict[str, Callable[[str, str, str], SourceRef]] = {
    "musicbrainz.org": _p_musicbrainz, "discogs.com": _p_discogs, "wikipedia.org": _p_wikipedia,
    "wikidata.org": _p_wikidata, "theporndb.net": _p_theporndb, "stashdb.org": _p_stashdb,
    "themoviedb.org": _p_tmdb, "imdb.com": _p_imdb, "thetvdb.com": _p_tvdb, "openlibrary.org": _p_openlibrary,
    "books.google": _p_googlebooks, "isfdb.org": _p_isfdb, "comicvine.gamespot.com": _p_comicvine,
    "comics.org": _p_gcd,
}


def parse_link(text: str, bare_as: Optional[tuple[str, str]] = None) -> Optional[SourceRef]:
    """The SourceRef a pasted link names, or None (never raises; explain_link() gives the reason in words)."""
    try:
        return explain_link(text, bare_as)
    except SourceIdError:
        return None

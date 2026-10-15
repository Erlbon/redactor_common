"""
redactor_common/core/note_convert.py

Converters from the de-facto metadata sidecars to a note draft (core/notes.py): Calibre's ``metadata.opf`` (or
an EPUB package document), Kodi/Plex/Jellyfin ``artist.nfo`` / ``album.nfo`` and ``ComicInfo.xml``. Each
``from_*`` takes a file path (str / PathLike), the XML text, or the raw bytes, and returns a `NoteConversion`:
``draft`` is a `NoteDraft` (front matter + Markdown body + a source label) or None, ``message`` says why not.
Nothing here raises for bad input.

Safety: sidecar XML comes from outside, so it is parsed with the standard library's expat after REFUSING any
DTD / entity declaration (no entity expansion is possible without one: no billion-laughs, no external
entities), and input over `MAX_SIDECAR_BYTES` is refused. No new dependency (defusedxml is not required).
Namespaces are ignored (elements are matched by local name). Qt-free.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Iterable, Optional

from redactor_common.core.note_text import credit_footer, html_to_markdown
from redactor_common.core.notes import NoteFrontMatter, render_note

MAX_SIDECAR_BYTES = 2 * 1024 * 1024


@dataclass
class NoteDraft:
    """A note ready to render: `kind` is the suggested note kind ('artist', 'album', 'series', 'about')."""

    front: NoteFrontMatter
    body: str
    source: str
    kind: str = "about"

    def render(self, footer: bool = True) -> str:
        """The note text; with `footer` a closing line 'Draft. Taken from <source>.' is added."""
        body = self.body.rstrip("\n")
        if footer:
            body += "\n\n" + credit_footer([f"Taken from {self.source}"])
        return render_note(self.front, body)


@dataclass
class NoteConversion:
    """The result of a converter: a draft, or None with the reason in `message`."""

    draft: Optional[NoteDraft] = None
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.draft is not None


# ---- loading and safe parsing ------------------------------------------------------------------------------------

def _decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _load_text(source: "str | bytes | os.PathLike") -> tuple[Optional[str], str]:
    """(text, '') or (None, reason). A str that starts with '<' is the XML itself, any other str / PathLike
    is a file path."""
    try:
        if isinstance(source, (bytes, bytearray)):
            data = bytes(source)
        elif isinstance(source, str) and source.lstrip("﻿ \t\r\n").startswith("<"):
            data = source.encode("utf-8")
        else:
            path = os.fspath(source)
            if not os.path.isfile(path):
                return None, f"Not a file: {path}"
            if os.path.getsize(path) > MAX_SIDECAR_BYTES:
                return None, "The file is too large to be a metadata sidecar."
            with open(path, "rb") as handle:
                data = handle.read(MAX_SIDECAR_BYTES + 1)
    except (OSError, TypeError, ValueError) as exc:
        return None, f"Could not read the sidecar: {exc}"
    if len(data) > MAX_SIDECAR_BYTES:
        return None, "The input is too large to be a metadata sidecar."
    return _decode(data), ""


_DECLARATION = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>", re.IGNORECASE)


def _parse(source: "str | bytes | os.PathLike") -> tuple[Optional[ET.Element], str]:
    text, message = _load_text(source)
    if text is None:
        return None, message
    if _DECLARATION.search(text):
        return None, "The XML has a DTD or entity declaration, which is refused for safety."
    try:
        return ET.fromstring(_XML_DECL.sub("", text.lstrip("﻿"), count=1)), ""
    except (ET.ParseError, ValueError) as exc:
        return None, f"The XML could not be read: {exc}"
    except RecursionError:
        return None, "The XML is nested too deeply."


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in element if _local(c.tag) == name.lower()]


def _first(element: ET.Element, *names: str) -> Optional[ET.Element]:
    for name in names:
        found = _children(element, name)
        if found:
            return found[0]
    return None


def _text(element: Optional[ET.Element]) -> str:
    return "" if element is None else "".join(element.itertext()).strip()


def _texts(element: ET.Element, *names: str) -> list[str]:
    out: list[str] = []
    for name in names:
        for child in _children(element, name):
            value = _text(child)
            if value and value not in out:
                out.append(value)
    return out


def _attr(element: ET.Element, name: str) -> str:
    for key, value in element.attrib.items():
        if _local(key) == name:
            return value
    return ""


_YEAR = re.compile(r"(?<!\d)(\d{4})(?!\d)")


def _year(value: str) -> str:
    """The first plausible 4-digit year in `value` (1000-2999) or ''."""
    for match in _YEAR.finditer(value or ""):
        if 1000 <= int(match.group(1)) <= 2999:
            return match.group(1)
    return ""


def _split_list(value: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,;]", value) if p.strip()]


def _lines(pairs: Iterable[tuple[str, str]]) -> str:
    """'**Label:** value' lines joined with Markdown hard breaks, for the non-empty values."""
    return "  \n".join(f"**{label}:** {value}" for label, value in pairs if value)


def _join_blocks(*blocks: str) -> str:
    return "\n\n".join(b.strip("\n") for b in blocks if b and b.strip()) + "\n"


def _fail(message: str) -> NoteConversion:
    return NoteConversion(None, message)


def _index(value: str) -> str:
    """A series index as a short number: '3.0' -> '3', '2.5' stays."""
    value = value.strip()
    try:
        number = float(value)
    except ValueError:
        return value
    return str(int(number)) if number == int(number) else value


# ---- OPF (Calibre metadata.opf, EPUB package document) -------------------------------------------------------------

def from_opf(source: "str | bytes | os.PathLike") -> NoteConversion:
    """A draft 'about' note for a book folder from a Calibre ``metadata.opf`` / EPUB ``.opf``: title, authors,
    description (HTML turned into Markdown-ish text), subjects -> genres, publisher, date -> years, and the
    series with its number in the body (Calibre ``calibre:series`` or EPUB 3 ``belongs-to-collection``)."""
    root, message = _parse(source)
    if root is None:
        return _fail(message)
    if _local(root.tag) != "package":
        return _fail("Not an OPF package document.")
    metadata = _first(root, "metadata")
    if metadata is None:
        return _fail("The OPF has no metadata.")
    title = (_texts(metadata, "title") or [""])[0]
    if not title:
        return _fail("The OPF has no title.")
    creators = []
    for creator in _children(metadata, "creator"):
        role, name = _attr(creator, "role").lower(), _text(creator)
        if name and role in ("", "aut") and name not in creators:
            creators.append(name)
    if not creators:
        creators = _texts(metadata, "creator")
    description = html_to_markdown("\n\n".join(_texts(metadata, "description")))
    series, series_index = "", ""
    for meta in _children(metadata, "meta"):
        name, prop = _attr(meta, "name"), _attr(meta, "property")
        if name == "calibre:series":
            series = _attr(meta, "content").strip()
        elif name == "calibre:series_index":
            series_index = _index(_attr(meta, "content"))
        elif prop == "belongs-to-collection" and not series:
            series = _text(meta)
            ident = _attr(meta, "id")
            for other in _children(metadata, "meta"):
                if ident and _attr(other, "refines") == f"#{ident}" and _attr(other, "property") == "group-position":
                    series_index = _index(_text(other))
    publisher = (_texts(metadata, "publisher") or [""])[0]
    year = _year((_texts(metadata, "date") or [""])[0])
    series_text = f"{series} (#{series_index})" if series and series_index else series
    body = _join_blocks(
        f"# {title}",
        _lines([("Author", ", ".join(creators)), ("Series", series_text), ("Publisher", publisher),
                ("Published", year)]),
        description)
    front = NoteFrontMatter(title=title, years=year, genres=_texts(metadata, "subject"))
    return NoteConversion(NoteDraft(front, body, "metadata.opf", "about"))


# ---- NFO (Kodi / Plex / Jellyfin artist.nfo and album.nfo) -----------------------------------------------------------

def from_nfo(source: "str | bytes | os.PathLike") -> NoteConversion:
    """A draft 'artist' or 'album' note from an ``artist.nfo`` / ``album.nfo`` (the root element decides).
    Artist: name, biography/overview, genre + style -> genres, formed/born .. disbanded/died -> years,
    members, label. Album: title, artist, review/overview, genre + style, year/releasedate, label, tracklist."""
    root, message = _parse(source)
    if root is None:
        return _fail(message)
    kind = _local(root.tag)
    if kind == "artist":
        return _artist_nfo(root)
    if kind == "album":
        return _album_nfo(root)
    return _fail("Not an artist.nfo or album.nfo document.")


def _members(root: ET.Element) -> list[str]:
    out: list[str] = []
    for member in _children(root, "member") + _children(root, "groupmember"):
        name = _text(_first(member, "name")) or _text(member)
        name = re.sub(r"\s+", " ", name).strip()
        if name and name not in out:
            out.append(name)
    return out


def _tracks(root: ET.Element) -> list[tuple[str, str]]:
    out = []
    for track in _children(root, "track"):
        title = _text(_first(track, "title"))
        if title:
            out.append((title, _text(_first(track, "duration"))))
    return out


def _artist_nfo(root: ET.Element) -> NoteConversion:
    name = _text(_first(root, "name", "title"))
    if not name:
        return _fail("The artist.nfo has no name.")
    formed, born = _text(_first(root, "formed")), _text(_first(root, "born"))
    disbanded, died = _text(_first(root, "disbanded")), _text(_first(root, "died"))
    start, end = formed or born, disbanded or died
    first, last = _year(start), _year(end)
    years = f"{first}-{last}" if first and last else (f"{first}-" if first else last)
    text = html_to_markdown(_text(_first(root, "biography", "overview", "review", "description", "profile")))
    members = _members(root)
    genres = _texts(root, "genre", "style")
    body = _join_blocks(
        f"# {name}", text,
        _lines([("Members", ", ".join(members)), ("Formed", formed), ("Born", born), ("Disbanded", disbanded),
                ("Died", died), ("Label", _text(_first(root, "label")))]))
    return NoteConversion(NoteDraft(NoteFrontMatter(title=name, years=years, genres=genres), body,
                                    "artist.nfo", "artist"))


def _album_nfo(root: ET.Element) -> NoteConversion:
    title = _text(_first(root, "title", "name"))
    if not title:
        return _fail("The album.nfo has no title.")
    artist = _text(_first(root, "artist", "albumartist")) or ", ".join(_texts(root, "artistdesc"))
    released = _text(_first(root, "releasedate", "year"))
    year = _year(_text(_first(root, "year"))) or _year(released)
    text = html_to_markdown(_text(_first(root, "review", "overview", "biography", "description")))
    tracks = _tracks(root)
    tracklist = ""
    if tracks:
        tracklist = "## Tracklist\n\n" + "\n".join(
            f"{n}. {t}{' ' + d if d else ''}" for n, (t, d) in enumerate(tracks, start=1))
    body = _join_blocks(
        f"# {title}",
        _lines([("Artist", artist), ("Released", released), ("Label", _text(_first(root, "label")))]),
        text, tracklist)
    return NoteConversion(NoteDraft(NoteFrontMatter(title=title, years=year, genres=_texts(root, "genre", "style")),
                                    body, "album.nfo", "album"))


# ---- ComicInfo.xml -----------------------------------------------------------------------------------------------------

_COMIC_CREDITS = (("Writer", "Writer"), ("Penciller", "Penciller"), ("Inker", "Inker"), ("Colorist", "Colorist"),
                  ("Letterer", "Letterer"), ("CoverArtist", "Cover artist"), ("Editor", "Editor"))


def from_comicinfo(source: "str | bytes | os.PathLike") -> NoteConversion:
    """A draft SERIES-level 'series' note from a ``ComicInfo.xml``: Series (the title), Summary, the credits
    (Writer, Penciller, ...), Publisher, Genre, Year, Web links. The issue's own Title / Number are named in
    the last line, since a series note is built from one issue of it."""
    root, message = _parse(source)
    if root is None:
        return _fail(message)
    if _local(root.tag) != "comicinfo":
        return _fail("Not a ComicInfo.xml document.")
    series = _text(_first(root, "series"))
    if not series:
        return _fail("The ComicInfo.xml has no Series.")
    year = _year(_text(_first(root, "year")))
    summary = html_to_markdown(_text(_first(root, "summary")))
    credits = [(label, _text(_first(root, tag))) for tag, label in _COMIC_CREDITS]
    number, issue_title = _text(_first(root, "number")), _text(_first(root, "title"))
    taken = ""
    if number or issue_title:
        taken = "*Taken from issue " + " ".join(p for p in (f"#{number}" if number else "",
                                                           f"“{issue_title}”" if issue_title else "") if p) + ".*"
    body = _join_blocks(f"# {series}", summary,
                        _lines(credits + [("Publisher", _text(_first(root, "publisher")))]), taken)
    links = [u for u in re.split(r"[\s,]+", _text(_first(root, "web"))) if re.match(r"(?i)https?://", u)]
    front = NoteFrontMatter(title=series, years=year, genres=_split_list(_text(_first(root, "genre"))), links=links)
    return NoteConversion(NoteDraft(front, body, "ComicInfo.xml", "series"))

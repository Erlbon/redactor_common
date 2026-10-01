"""
redactor_common/core/musicbrainz_schema.py

What the MusicBrainz database dump looks like, as machine-readable
constants -- app-neutral shared knowledge for any recipe that reads the
core dump (`mbdump.tar.bz2`) with core/dump_import.iter_tar_members() +
iter_pgcopy_records().

Sources, checked 2026-10-01:
  * https://musicbrainz.org/doc/MusicBrainz_Database/Download  (which archives exist, licences)
  * https://github.com/metabrainz/musicbrainz-server, branch master:
      admin/sql/CreateTables.sql        column order of every table (below)
      admin/ExportAllTables             what goes into which archive
      lib/MusicBrainz/Script/MBDump.pm  archive layout, TIMESTAMP / SCHEMA_SEQUENCE files
      lib/MusicBrainz/Server/Constants.pm  @CORE_TABLE_LIST / @DERIVED_TABLE_LIST
      lib/DBDefs.pm.sample              DB_SCHEMA_SEQUENCE
  * https://data.metabrainz.org/pub/musicbrainz/data/fullexport/<date>/ (directory listing only)

Format facts (all verified against those sources, none by downloading the dump):
  * The dump is `COPY <table> TO stdout` output: PostgreSQL's default TEXT
    format, one file per table, `mbdump/<table>`, tab-separated, no header,
    `\\N` for NULL, backslash escapes. Columns come in table-definition order
    (MusicBrainz's own MBImport.pl loads them with a plain COPY ... FROM,
    so CreateTables.sql order IS the file order).
  * `mbdump.tar.bz2` (about 7 GB) is the CORE dump, licence CC0 (public
    domain, COPYING-PublicDomain). `mbdump-derived.tar.bz2` (annotations,
    tags, `*_meta` ratings, medium_index ...) is CC BY-NC-SA 3.0 -- a
    recipe that wants to stay free of the non-commercial licence must not
    read it. The other archives (edit, editor, stats, cover-art-archive,
    ...) are BY-NC-SA too. Everything in this module's TABLES is core.
  * Each archive starts, at its ROOT, with TIMESTAMP, COPYING, README,
    REPLICATION_SEQUENCE and SCHEMA_SEQUENCE (in that order, "so MBImport
    can quickly find them"), then the tables under `mbdump/`. TIMESTAMP is
    a PostgreSQL timestamptz text ("2026-09-30 00:22:22.1+00"),
    SCHEMA_SEQUENCE and REPLICATION_SEQUENCE are a single integer + newline.
    Compression is bz2 on data.metabrainz.org (the exporter can also write
    xz/gz); the JSON dumps (`release-group.tar.xz`, `artist.tar.xz`, ...,
    in .../json-dumps/<date>/) are xz tars of one JSON object per line.
  * SCHEMA_SEQUENCE changes whenever MusicBrainz changes the table layout;
    the dump's value must equal the one the recipe was written for.
"""

from __future__ import annotations

# DBDefs.pm.sample's DB_SCHEMA_SEQUENCE on master on the date above. A dump
# made by an older server may carry an older number: the recipe's owner
# must re-check the column lists below against CreateTables.sql (or the
# tagged schema-change notes) and bump this when MusicBrainz moves on.
SCHEMA_SEQUENCE_EXPECTED = 31

CORE_ARCHIVE = "mbdump.tar.bz2"            # CC0
DERIVED_ARCHIVE = "mbdump-derived.tar.bz2"  # CC BY-NC-SA 3.0 -- avoid

# Member name of each table inside the archive.
TABLE_MEMBER_PREFIX = "mbdump/"

# The archive-root bookkeeping files, in archive order.
INFO_MEMBERS = ("TIMESTAMP", "COPYING", "README", "REPLICATION_SEQUENCE", "SCHEMA_SEQUENCE")

# Tables in the DERIVED archive that a recipe may be tempted by (ratings and
# first-release dates live in *_meta, tags in *_tag). Not CC0: never read.
DERIVED_TABLES = frozenset({
    "annotation", "area_annotation", "area_tag", "artist_annotation", "artist_meta", "artist_tag",
    "event_annotation", "event_meta", "event_tag", "instrument_annotation", "instrument_tag",
    "label_annotation", "label_meta", "label_tag", "medium_index", "place_annotation", "place_meta",
    "place_tag", "recording_annotation", "recording_meta", "recording_tag", "release_annotation",
    "release_group_annotation", "release_group_meta", "release_group_tag", "release_meta",
    "release_tag", "series_annotation", "series_tag", "tag", "tag_relation", "work_annotation",
    "work_meta", "work_tag",
})

# Column order of each table's file, from CreateTables.sql. Foreign keys are
# plain integer ids (artist_credit -> artist_credit.id, release_group ->
# release_group.id, ...); `gid` is the public UUID. Dates are split into
# *_year/_month/_day columns (NULL where unknown); `length` is milliseconds.
TABLES: dict[str, tuple[str, ...]] = {
    "area": (
        "id", "gid", "name", "type", "edits_pending", "last_updated",
        "begin_date_year", "begin_date_month", "begin_date_day",
        "end_date_year", "end_date_month", "end_date_day", "ended", "comment",
    ),
    "artist": (
        "id", "gid", "name", "sort_name",
        "begin_date_year", "begin_date_month", "begin_date_day",
        "end_date_year", "end_date_month", "end_date_day",
        "type", "area", "gender", "comment", "edits_pending", "last_updated", "ended",
        "begin_area", "end_area",
    ),
    "artist_alias": (
        "id", "artist", "name", "locale", "edits_pending", "last_updated", "type", "sort_name",
        "begin_date_year", "begin_date_month", "begin_date_day",
        "end_date_year", "end_date_month", "end_date_day", "primary_for_locale", "ended",
    ),
    "artist_credit": ("id", "name", "artist_count", "ref_count", "created", "edits_pending", "gid"),
    # An artist credit's artists in order: `name` is the credited spelling,
    # `join_phrase` what follows it (" & ", " feat. ", "").
    "artist_credit_name": ("artist_credit", "position", "artist", "name", "join_phrase"),
    "artist_type": ("id", "name", "parent", "child_order", "description", "gid"),
    "gender": ("id", "name", "parent", "child_order", "description", "gid"),
    "release_group": ("id", "gid", "name", "artist_credit", "type", "comment", "edits_pending", "last_updated"),
    "release_group_primary_type": ("id", "name", "parent", "child_order", "description", "gid"),
    "release_group_secondary_type": ("id", "name", "parent", "child_order", "description", "gid"),
    "release_group_secondary_type_join": ("release_group", "secondary_type", "created"),
    "release": (
        "id", "gid", "name", "artist_credit", "release_group", "status", "packaging", "language", "script",
        "barcode", "comment", "edits_pending", "quality", "last_updated",
    ),
    "release_status": ("id", "name", "parent", "child_order", "description", "gid"),
    "release_packaging": ("id", "name", "parent", "child_order", "description", "gid"),
    # One row per release and country; `country` is an area id (see country_area / iso_3166_1).
    "release_country": ("release", "country", "date_year", "date_month", "date_day"),
    # Release dates with no country ("[worldwide]" / unknown).
    "release_unknown_country": ("release", "date_year", "date_month", "date_day"),
    "release_label": ("id", "release", "label", "catalog_number", "last_updated"),
    "label": (
        "id", "gid", "name",
        "begin_date_year", "begin_date_month", "begin_date_day",
        "end_date_year", "end_date_month", "end_date_day",
        "label_code", "type", "area", "comment", "edits_pending", "last_updated", "ended",
    ),
    "medium": ("id", "release", "position", "format", "name", "edits_pending", "last_updated", "track_count", "gid"),
    "medium_format": ("id", "name", "parent", "child_order", "year", "has_discids", "description", "gid"),
    "track": (
        "id", "gid", "recording", "medium", "position", "number", "name", "artist_credit", "length",
        "edits_pending", "last_updated", "is_data_track",
    ),
    "recording": ("id", "gid", "name", "artist_credit", "length", "comment", "edits_pending", "last_updated", "video"),
    "language": ("id", "iso_code_2t", "iso_code_2b", "iso_code_1", "name", "frequency", "iso_code_3"),
    "script": ("id", "iso_code", "iso_number", "name", "frequency"),
    "country_area": ("area",),
    "iso_3166_1": ("area", "code"),
}


def table_columns(table: str) -> tuple[str, ...]:
    """The verified column order of `table`'s dump file; KeyError names the
    tables this module doesn't know (add them from CreateTables.sql)."""
    try:
        return TABLES[table]
    except KeyError:
        raise KeyError(f"No verified column list for MusicBrainz table {table!r}") from None


def table_member(table: str) -> str:
    """The archive member name holding `table`: 'mbdump/<table>'."""
    return TABLE_MEMBER_PREFIX + table

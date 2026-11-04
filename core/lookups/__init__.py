"""
redactor_common/core/lookups/

The online lookup clients that more than one Redactor uses (promoted 2026-11-04 for the Folder Redactor, which takes
over the folder notes while mp3redactor and videoredactor keep their Look Up menus):

  musicbrainz.py    MusicBrainz releases + matching files to tracks   (from mp3redactor core/musicbrainz_lookup.py)
  discogs.py        Discogs releases, token handling                  (from mp3redactor core/discogs_lookup.py)
  tmdb.py           TMDB films and shows                              (from videoredactor core/tmdb_client.py)
  release_name.py   scene/release file names -> title, year, S/E      (from videoredactor core/release_name_parser.py)
  cover_finder.py   the album-art image beside the files              (from mp3redactor core/cover_art.py)

The Wikipedia client is `redactor_common.core.wikipedia` (mp3redactor's music page finder folded into it).

Rules: nothing here imports Qt (tests/test_lookups_no_qt.py imports every module with PyQt6 blocked), so the folder
can become its own package unchanged the day a Qt-free program needs one of these clients. What differs per app (the
User-Agent, where a key or token is stored) is set once by the app with each module's `configure()`.
"""

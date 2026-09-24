# The Redactor suite — overview

A single-file orientation to the whole suite, written for the "The
Redactor suite" Claude Project and for anyone new to the code. It
summarizes; the repos are the source of truth. Last updated 2026-09-24.

## What it is

Four PyQt6 Windows desktop apps for bulk-editing file metadata in the
style of mp3tag (load a folder into a table, select many files, edit
fields for all of them at once), plus the library they share. The
public site is [erlbon.github.io](https://erlbon.github.io). A
Linux/macOS port is a planned goal.

| Repo | What it edits | Version |
|---|---|---|
| [epubredactor](https://github.com/Erlbon/epubredactor) | EPUB metadata, aimed at preparing books for Kobo e-readers | 2026-09-23#02 |
| [mp3redactor](https://github.com/Erlbon/mp3redactor) | MP3 (ID3v2) tags, plus integrity/BPM/key/loudness checks | 2026-09-23#03 |
| [videoredactor](https://github.com/Erlbon/videoredactor) | MP4/M4V and MKV metadata | 2026-09-23#02 |
| [cbzredactor](https://github.com/Erlbon/cbzredactor) | `ComicInfo.xml` inside CBZ comic archives | 2026-09-23#02 |
| [redactor_common](https://github.com/Erlbon/redactor_common) | Shared GUI and core code, pip-installed by every app | 2026-09-23#02 (tag `2026-09-23-02`) |

Supporting repos: [redactor-build-tools](https://github.com/Erlbon/redactor-build-tools)
(Docker toolchain for Linux builds, not yet tested end to end) and
[erlbon.github.io](https://github.com/Erlbon/erlbon.github.io) (the
landing page).

## The apps

### epubredactor
Bulk EPUB metadata: title, authors, author sort, series and series
number (written both as Calibre's `calibre:series` and as EPUB3
collections), collection, genre, publisher, date, ISBN, DDC, language,
description, cover. Also: validation and fixes on load, Rename/Export by
pattern, Parse Filename → Metadata (with cross-checks against other
files and saved folder metadata), metadata lookups via Google Books, Open
Library and the user's own Calibre install, cover generation and a
"junk cover" flag, Import to EPUB and Polish Book (Calibre
`ebook-convert`/`ebook-polish`), Open with Sigil, Send to Kobo (USB) and
to an eReader (wireless), content scan, missing-space detection,
manifest and navigation repair, image compression. Built for libraries
of 15,000+ books: lazy cover thumbnails and progress dialogs everywhere.
Optional external tools: Calibre, Sigil.

### mp3redactor
Bulk ID3 tag editing (title, artist, album artist, album, track, disc,
year, genre, composer, comment, language, sort fields, AcoustID,
iTunes advisory), with checks: file integrity (`mp3val`), deep decode
check and loudness/ReplayGain (ffmpeg/ffprobe), BPM (`aubio`, in
process), musical key (`keyfinder-cli`), lyrics fetch (LRCLIB) and edit.
Cover art: Cover column with lazy thumbnails, resizable panel preview,
set from an image file or from each file's folder image (`cover.jpg`,
`folder.jpg`, ...), remove, export. Also Import & Convert to MP3,
Rename/Export, Parse Filename, Auto-Numbering. Tags via `mutagen`.
Releases do not bundle the external tools.

### videoredactor
MP4/MKV metadata with a Content Type filter (Movie / TV / Music Video /
Clip / Misc) that shows only the relevant fields. TMDB lookup (movie and
TV, every match confirmed by hand, with a season/episode picker),
TheTVDB lookup, subtitle download from OpenSubtitles (hash match first),
Remux to MP4, Convert to MP4 (H.264/AAC), Import & Convert other video
formats. Rename/Export, Import from Filename, Case Conversion,
Search/Replace, Auto-Numbering, undo/redo, async thumbnail preview.
External tools, not bundled: ffmpeg and MKVToolNix (found via PATH, a
bundled `tools/` folder, or the default install folder). API keys for
TMDB/TheTVDB/OpenSubtitles are entered by the user.

### cbzredactor
Edits `ComicInfo.xml` (ComicRack/Anansi schema) inside CBZ archives
without touching the page images; creates it on save if missing. Reads
CBR/CBT/CB7 and offers to convert them to CBZ. Lookups via Comic Vine
(needs a free API key; ranked candidates with runner-ups), Bedetheque
and the GCD. Table cover thumbnails (page 1, loaded lazily), async panel
preview, in-process image resizing, Rename/Export, Parse Filename,
Search/Replace, Case Conversion, Auto-Numbering, overwrite review.

## Architecture

Each app is laid out the same way: `core/` holds pure logic with no Qt
(unit-tested without a display), `gui/` holds the PyQt6 windows and
dialogs, `main.py` is the entry point, and tests live in `tests/`
(mp3, video) or at the repo root (epub, cbz).

Each app pins redactor_common to a release tag in `requirements.txt`:

```
redactor_common @ git+https://github.com/Erlbon/redactor_common.git@2026-09-23-02
```

Settings live in an app-prefixed `.ini` next to the executable (or the
project root when run from source), never the Windows registry. Frozen
builds use PyInstaller with a `.spec` file (`build_exe.bat`).

## What redactor_common provides

**core/** (no Qt):
- `subprocess_utils.run_tool()`: the one way to run an external tool.
  No console window, `stdin=DEVNULL`, UTF-8 output, a timeout.
- `tool_locator.find_tool()`: override, bundled `tools/`, PATH, then
  well-known install folders.
- `app_paths`, `crash_log`, `version_bump`: per-app paths, crash
  logging, and the `YYYY-MM-DD#NN` version bump.
- Rename and parse engines: `rename_pattern`, `filename_parser`.
- Text operations: `search_replace`, `case_conversion`, `auto_number`,
  `series_numbering`.
- Lists and settings: `managed_list` (hideable defaults plus custom
  entries), `pattern_history`, `table_settings`, and `languages`, one
  ISO 639 table.
- `lookup_client`: HTTP for metadata lookups.
- `undo`, `folder_refresh`, `save_errors`, `error_summary`, `os_utils`.

**gui/** (PyQt6):
- Window shell: `app_bootstrap.run_app()` (startup: crash log, theme,
  icon), `menu_builder` (File / Import / Operations / Settings / Help),
  `standard_shortcuts`, `theme`, `about_dialog`, `context_menu`,
  `column_menu`, `column_settings_dialog`, `collapsible_splitter`,
  `zoom_toolbar`.
- Tables: `sortable_table`, and `visible_rows` + `async_icon_cache`
  (lazy table thumbnails).
- Images: `async_preview` (background image loading), `image_decode`,
  and `image_pane` (the resizable image area in every side panel).
- Progress: `progress` (`run_with_progress`, `ProgressReporter`).
- Shared dialogs: rename/parse, search/replace, case conversion,
  auto-numbering, overwrite review, lookup (`LookupDialogBase`), quick
  pick, list management, single-file rename.

The module tables in the README, along with its adoption matrix
(which app uses what), give the full detail.

## Conventions (from the redactor-conventions skill)

1. **Sync first.** Other sessions and machines edit these repos
   concurrently. Before editing and again right before pushing: `git
   fetch origin -q; git status --porcelain -b`.
2. **Version bump at check-in.** Every commit that changes real code
   runs `python bump_version.py` and adds a `CHANGELOG.md` entry in the
   same check-in. Docs-only commits are exempt.
3. **Progress feedback for any per-file loop**, via `run_with_progress()`
   or `ProgressReporter`. `cancellable=False` for read-only scans, `True`
   for applies that change things. The only exception is a live-typing
   preview whose per-item work is cheap string logic.
4. **Promote to redactor_common first.** Anything useful to more than
   one app is built there, version-bumped and tagged, then each app bumps
   its pin, version and changelog.
5. **Landing page honesty.** erlbon.github.io only claims features that
   have shipped, checked against real menus and file filters.
6. **Cross-platform.** No unguarded Windows-only code; guard with
   `sys.platform == "win32"`.
7. **PowerShell 5.1 release-script pitfalls.** Redirect only stdout
   when checking a native command's exit code, and treat "tag exists,
   release doesn't" as a resumable state.

## Release process

For redactor_common:
1. `python bump_version.py`, update the README's "Currently:" line.
2. Commit, then `git tag YYYY-MM-DD-NN` (the `#` becomes `-`).
3. Push main and the tag.

Then in each app:
1. Bump the pin.
2. Run the tests.
3. `python bump_version.py`, add a changelog entry.
4. Commit and push.

Always push redactor_common and its tag before the apps, or a fresh
install of an app fails.

Building and publishing GitHub Releases uses `release.ps1` /
`release-<app>.ps1` / `release-all.ps1` in a machine-local
`_shared-tools` folder that is deliberately not in git, together with a
hand-placed `upx.exe`.

## Testing

`pytest` in each repo. GUI tests run headless with
`QT_QPA_PLATFORM=offscreen`. As of 2026-09-24, some tests fail for
reasons that predate recent work and depend on the machine:
- cbz: 2 scroll-wheel tests.
- epub: 1 dialog-layout test.
- mp3: 5 lyrics-fetcher tests and 2 "mp3val missing" tests. These
  depend on installed packages and tools.

Everything else passes:

| Repo | Passing tests |
|---|---|
| redactor_common | 99 |
| cbz | 205 |
| epub | 519 |
| mp3 | 185 |
| video | 327 |

## Open items and known gaps

- epub's Parse Filename dialog runs on the shared engine but keeps its
  own dialog for its cross-file checks; promoting those into the shared
  dialog is the natural next step.
- video's Genre/Language editor is a simpler design than the shared
  list-management dialog; choose one deliberately.
- video's TMDB/TheTVDB dialogs are an interactive search-and-pick flow,
  not the shared batch lookup dialog (their HTTP code is shared).
- "Hiding a column also hides its panel field" exists separately in cbz
  and video.
- mp3 has no Search/Replace or Case Conversion yet.
- Embedding a large cover image into many MP3s isn't downscaled; an
  optional resize on import would help.
- The erlbon.github.io mp3 section should gain a cover-art line once
  that version is released.
- videoredactor has no README.md (it has ABOUT.md and BUILD.md).
- redactor-build-tools' Linux build hasn't been verified end to end.

## Where to look

- Suite-wide conventions: `redactor_common/.claude/skills/redactor-conventions/SKILL.md`
- Shared-code detail and adoption: `redactor_common/README.md`
- Per-app setup and domain notes: each repo's `CLAUDE.md`
- Per-app features: `README.md` (epub, mp3, cbz), `ABOUT.md` (video)
- History: each repo's `CHANGELOG.md`

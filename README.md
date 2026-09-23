# redactor_common

Shared interface/UX package for the "Redactor" family of tools (epub,
video, mp3, and future ones). Built by comparing the projects'
independently-built implementations of the same mechanisms and
promoting the best-of-breed version of each, generalized to work on
any item type via accessor callables rather than being tied to one
project's data model.

## Installing (as a consuming project)

A real pip dependency now, not a folder you copy in -- one source of
truth instead of three vendored copies quietly drifting out of sync
(which is exactly what happened before this: the same bug sat fixed
here while three separate hand-copied copies kept shipping it broken).
In a consuming project's `requirements.txt`:

```
redactor_common @ git+https://github.com/Erlbon/redactor_common.git@2026-09-23-01
```

Pin to a tag (see "Releasing a new version" below), not `@main` --
floating on the branch means one bad push here instantly breaks every
consuming project's next `pip install -r requirements.txt`, with no
review step in between. Bumping the pin is a deliberate, visible
one-line diff in that project's own `requirements.txt` instead.

Import paths are unchanged either way: `from redactor_common.gui.menu_builder
import ...` etc. still work exactly as they did when this was a
vendored folder -- pip installs it under the same `redactor_common`
name, just from site-packages instead of a sibling directory.
PyInstaller's default static-import analysis picks it up automatically
there too, same as any other pip dependency (PyQt6 included) -- no
spec-file changes needed.

## Releasing a new version

```
python bump_version.py     # keeps core/version.py and pyproject.toml's version in lockstep
git add -A && git commit -m "..."
git tag <the version bump.py just printed, e.g. 2026-09-04-10>
git push origin main --tags
```

Then in each consuming project that needs the change: bump the tag in
that project's `requirements.txt`, `pip install -r requirements.txt`,
run its test suite, commit.

## Versioning

`redactor_common` carries two version markers that `bump_version.py`
keeps in lockstep: `core/version.py`'s `REDACTOR_COMMON_VERSION`
(`YYYY-MM-DD#NN`, same convention every consuming project's own
`bump_version.py` uses -- this is what each project's `AboutDialog`
shows under its own version line, via `component_versions`) and
`pyproject.toml`'s `version` (the same date, PEP 440-formatted for pip:
`YYYY.M.D.NN`).

Currently: `2026-09-23#01`.

## core/ — pure logic, no PyQt6 dependency, unit-tested

| Module | What it does | Source |
|---|---|---|
| `table_settings.py` | Field-name-based column visibility/order persistence | video (more robust than epub's index-based original) |
| `rename_pattern.py` | `%field%` pattern → filename. Optional `(...)`/`[...]`/`{...}` groups (dropped when every field inside is empty), legacy token `aliases`, reserved device names checked before the first dot (`CON.mp4`), 150-char cap, `unique_path()`, `rename_file_on_disk()` | epub, generalized off `EpubMetadata` to a plain `dict[str, str]`; optional groups + aliases merged back from epub 2026-09-23, dotted reserved-name check from video |
| `filename_parser.py` | filename → `%field%` values (reverse of the above). Per-field regex shapes (`field_patterns`: `SERIES_INDEX_FIELD_PATTERN` with ranges/ordinals, `MONTH_FIELD_PATTERN` with month names, `YEAR_FIELD_PATTERN`, ...), per-field `normalizers`, optional groups, `field_value_counts()`. Whitespace is matched strictly first and only loosened if nothing matches -- the loose rule alone split "Jean-Paul Sartre - Nausea" on the inner hyphen | epub, same generalization; epub's later additions merged back 2026-09-23 |
| `search_replace.py` | plain/regex search & replace | epub, already generic |
| `case_conversion.py` | UPPER/lower/Title/Sentence case. Title case capitalizes after a colon/dash ("Star Wars: A New Hope") and the first letter rather than first character ("(The End)"); video's short mode names accepted too | epub; video's clause rules merged 2026-09-23 |
| `save_errors.py` | Windows path-too-long detection & messaging | epub, already generic |
| `error_summary.py` | Bounded preview string for a list of error messages | epub, already generic |
| `tool_locator.py` | External CLI tool lookup: override → bundled `tools/` dir → PATH → well-known install folders (`install_dirs`, `windows_program_dirs()`), for installers that don't add themselves to PATH (MKVToolNix, Calibre, Sigil). `which` injectable | mp3 (tiers 1-3); tier 4 from epub's Calibre/Sigil lookup, 2026-09-23 |
| `auto_number.py` | `generate_auto_number()` / `apply_auto_number_to_text_field()` — sequential-number generation for Auto-Numbering | video, already generic |
| `series_numbering.py` | `generate_series_numbers()` — decimal-capable (`Decimal`, not `int`) sequential-value generation, for a "start here, +step per row" quick numbering of a single field (a novella slotted in at "3.5", a comic special issue) | epub, already generic |
| `os_utils.py` | `reveal_in_file_manager()` — cross-platform "show this file in Explorer/Finder" | epub, already generic |
| `lookup_client.py` | `fetch_json()`/`fetch_bytes()` -- injectable-`fetch` HTTP (a URL or a `urllib` Request from `build_request()`: query params, API-key/bearer headers, JSON POST), HTTPError/URLError/timeout/decode-error → friendly-message translation, per-code `status_messages`, `make_default_fetch()` for a fixed-User-Agent fetch | cbzredactor; request building + status messages added 2026-09-23 for video's TMDB/TheTVDB/OpenSubtitles and epub's Google Books/Open Library clients |
| `undo.py` | `UndoManager` — bounded in-memory undo/redo stack (bulk edits, search/replace, case conversion, lookup-apply, ...), generic via caller-supplied `snapshot_fn`/`restore_fn`. Redo (2026-09-13) works by having `undo()`/`redo()` snapshot the item's current state onto the opposite stack before overwriting it, via the same `snapshot_fn` passed to `push()` — pass it to `undo()`/`redo()` too to get redo; omit it for the old undo-only behavior | epub, generalized off its original version (which snapshotted `EpubBook`/`EpubMetadata` fields directly) once cbzredactor needed the same "last N in-memory edits" undo behavior |
| `folder_refresh.py` | `find_new_files_in_loaded_folders()` — the logic behind "Refresh List" (F5/Ctrl+R): re-scans the folder(s) already-loaded paths live in via a caller-supplied `find_files_in_folder(folder)` (each project's own existing file-discovery function, bound to non-recursive) and reports whichever paths aren't already loaded. Doesn't discover a brand-new folder nothing's been loaded from at all -- only folders already represented get scanned | epub, generalized off its `refresh_list()` -- cbzredactor had independently rewritten the same behavior from scratch rather than sharing code; mp3/video had neither |
| `subprocess_utils.py` | `run_tool()` -- one way to shell out: no console window, `stdin=DEVNULL` (ffmpeg can hang on an inherited stdin), UTF-8 output decoding (the locale default mangled ffprobe/mkvmerge JSON: "Amélie" → "AmÃ©lie", then saved back), and a timeout; `popen_tool()`, `no_window_kwargs()` | 2026-09-23; epub/mp3/video each had their own no-console helper, and video's calls had neither stdin nor UTF-8 nor timeouts |
| `app_paths.py` | `base_dir()`/`tools_dir()`/`asset_path()`/`settings_ini_path()`/`crash_log_path()` -- frozen-vs-dev resolution, given the project's own root (this package lives in site-packages) | 2026-09-23; four copies before (epub/mp3/cbz `core/app_paths.py`, video `config._app_dir()`) |
| `crash_log.py` | `install(log_path, also_call=...)` -- excepthook writing timestamped tracebacks + faulthandler for native crashes, trimmed by whole entries | epub, 2026-09-23 (cbz had an identical port, mp3 a copy that cut entries mid-way, video had none) |
| `managed_list.py` | The model behind `manage_list_dialog.py`: merge/hide/add/remove over "hideable defaults + custom entries" (names or `(code, name)` pairs) + tolerant JSON (de)serialization | 2026-09-23; cbz, epub and mp3 each had the same ~150 lines |
| `pattern_history.py` | `dedupe_and_trim()` + `encode_history()`/`decode_history()` (reads JSON or video's `\x1f` format) for the Rename/Parse pattern history | 2026-09-23; four copies before |
| `version_bump.py` | The `YYYY-MM-DD#NN` bump (`bump_version_file()`, `pep440_from()`, `main()`), called by every repo's own few-line `bump_version.py` | 2026-09-23; five copies before |
| `languages.py` | One ISO 639 table (639-1, 639-2/T, 639-2/B, English name): `convert()`, `name_for()`, `language_pairs(codes, style)` for each app's quick-pick list in its format's code style (EPUB/ComicInfo 2-letter, ID3 3-letter, Matroska bibliographic "ger") | 2026-09-23 |

`pytest` from this folder runs everything (`tests/`): the core modules
need no Qt; the GUI tests run headless under `QT_QPA_PLATFORM=offscreen`.

## gui/ — PyQt6 widgets/dialogs

Tested headless (`QT_QPA_PLATFORM=offscreen`): `tests/test_gui_widgets.py`
covers the image/preview/visible-rows/progress modules and constructs
the shared Rename and Parse Filename dialogs; `lookup_dialog.py`,
`quick_pick_dialog.py` and `manage_list_dialog.py` are exercised through
the consuming apps' own suites (cbz, epub). A dialog that's only imported,
never constructed, isn't tested -- a missing constructor parameter got
past exactly that on 2026-09-23.

| Module | What it does |
|---|---|
| `action_factory.py` | `make_action()` — one QAction, shared between menu + toolbar |
| `colors.py` | Shared row-tint colors (dirty/error/etc) plus a current-cell focus outline for a `QTableWidget` — standardized on epub's scheme; mp3/video had each independently picked their own. Does NOT set selected-row colors (that's `theme.py`'s job now — see its 2026-09-07 fix note) | epub |
| `menu_builder.py` | Declarative **File / Import / Operations / Settings / Help** builder — enforces identical top-level shape and mnemonics across projects; project-specific menus (e.g. epub's Kobo) insert via `extra_menus`. `populate_menu()` (public) fills any QMenu from the same declarative item list — what `context_menu.py`/`column_menu.py` build their right-click menus on |
| `context_menu.py` | Shared table right-click menu: selection-fix (right-click outside the selection replaces it, matching Explorer) + generic "Open Containing Folder"/"Copy Path", with each project's own actions layered on via `extra_items` | epub, generalized (mp3 and video had no equivalent, or a much thinner one) |
| `column_menu.py` | Shared column-header right-click menu: inline show/hide checklist + a link to `column_settings_dialog.py` | video, generalized (epub only had a "Hide `<this column>`" quick action; mp3 has no column-visibility system to hang this on yet) |
| `image_label.py` | `AspectRatioImageLabel` — a QLabel that rescales its pixmap to fit on every resize | epub, already generic |
| `collapsible_splitter.py` | `SplitterPaneCollapser` (window-side resize/restore logic) + `CollapseToggleButton` (the panel's own "◀"/"▶" button) for a collapsible side-panel splitter | epub, generalized (video had the same 2-pane splitter shape but no collapse mechanism at all; mp3 has no side panel) |
| `grid_utils.py` | `absorb_extra_row_space()` — stops a fixed-row QGridLayout (the bulk-edit tag panels) from spreading leftover vertical space evenly into every row's gap on resize; collects it as blank space below instead | fixes a bug reported on mp3; epub's identically-structured grid had the same latent issue |
| `zoom_toolbar.py` | The +/− table-font-zoom control (epub had it, video didn't — now shared) |
| `column_settings_dialog.py` | "Add/Remove Columns" dialog, built on `core/table_settings.py` |
| `progress.py` | `run_with_progress()` -- threshold-gated progress dialog (small batches don't flicker one), fixed width + elided per-item `label_for` text so it never jitters in size. `ProgressReporter` -- the same dialog for work that drives its own loop: `on_progress`/`should_cancel` callbacks for a core function, or `set_label`/`set_value`/`connect_cancel` for a worker thread | epub/mp3; `ProgressReporter` 2026-09-23, replacing five hand-rolled dialogs in mp3/video |
| `async_icon_cache.py` | `AsyncIconCache` — caches a computed `QIcon` per item, keyed by identity (`IdentityWeakDict`, so unhashable dataclasses work), invalidated when its `source` changes (raw bytes by identity, or a small version key like `(path, mtime)` by equality); a `loader` callable fetches the bytes on the worker thread for apps that don't keep covers in memory (cbz); a request already in flight isn't queued twice and decodes/scales a cache miss off the main thread via `QThreadPool`, so a table full of these never blocks its own population on image decoding. Decodes via `QImageReader.setScaledSize()` (lets the format's own decoder downscale during decode, e.g. JPEG's DCT-domain downscaling) rather than a full-resolution decode + separate scale -- measured 2.67x faster for a real cover image (2026-09-17); falls back to the slower `QImage.fromData()` + `.scaled()` path only if the reader can't determine a size upfront. **Important finding from that same investigation, worth knowing before leaning on this module's threading for more than UI responsiveness: PyQt6's QImage decode does not release the GIL, so `QThreadPool` worker threads do NOT parallelize real throughput for this work -- measured 0.96x with 8 threads vs 1 (i.e. no speedup at all). The async design still keeps the main thread's event loop responsive during decoding, which is a real and worthwhile benefit, but it does not reduce the total wall-clock time to decode everything -- for that, only avoiding unnecessary decode calls in the first place (e.g. epub's lazy/viewport-based icon loading, decoding only for visible rows) or true multi-process parallelism would help.** | epub, generalized (its table cover-icon re-decoded from scratch on every single rebuild, even for a cover that hadn't changed) |
| `async_hash_cache.py` | `AsyncHashCache` — same shape as `async_icon_cache.py` but for a SHA-256 hex digest instead of a `QIcon`; hashes a cache miss off the main thread | epub's Junk Cover column (2026-09-17): identifying a book's cover as junk means hashing the full cover image, once per book, on every table rebuild — real seconds of synchronous hashing for a large library with real cover art, found profiling a reported "Updating list is still slow" regression |
| `qmessagebox_style.py` | App-wide `QMessageBox` max-width fix (one call in `main.py`) |
| `about_dialog.py` | Shared About/Changelog/Credits dialogs (Markdown-rendering, logo, version header with optional `component_versions` + link-back `repo_url`/`component_repo_urls`) — promoted from epub's version |
| `preview_table.py` | Shared "before/after + Apply checkbox" table controller (epub built this pattern twice independently for Search/Replace and Case Conversion — now once). Gained an optional grouping column (`group_column_label`) and a per-row `default_checked` state via `PreviewRow` (2026-09-10, promoted out of cbzredactor's per-file/per-field overwrite-review dialog) |
| `search_replace_dialog.py`, `case_conversion_dialog.py` | Generalized dialogs built on `preview_table.py`. Case Conversion's preview (re-run on a dropdown change) goes through `run_with_progress` for large libraries; Search/Replace's live-typing preview is the family rule's documented exception |
| `overwrite_review_dialog.py` | `OverwriteReviewDialog` + `build_overwrite_review_rows()` + `resolve_overwrite_conflicts()` — the per-file, per-field "review before overwrite" confirmation, built on `preview_table.py`'s grouping support. `resolve_overwrite_conflicts()` is the one call a consuming project's MainWindow needs: it checks whether a batch of changes would clobber anything, skips the dialog entirely if not, and otherwise shows every touched field with a blank-field-starts-ticked/real-overwrite-starts-unticked default | cbzredactor, promoted with zero code changes (its own version had no project-specific dependencies to begin with — duck-types on `.path`/`.metadata`) |
| `auto_numbering_dialog.py` | Generalized Auto-Numbering dialog (field picker + start/increment/zero-pad/separator + preview), built on `preview_table.py` | video, generalized (epub's "Number Series" is a narrower, single-field version of the same idea and is unaffected; mp3/cbz had neither) |
| `quick_series_number.py` | `prompt_and_generate_series_numbers()` — the one-prompt "starting value, +1 per row" quick numbering for a table right-click menu, no field picker/preview (that's what `auto_numbering_dialog.py` is for) | epub, generalized (its `quick_number_series()` right-click handler) |
| `pattern_field_panel.py` | The ▼ recent-patterns menu + always-visible recent list + clickable placeholder-code side panel (epub v51/v54 UX) |
| `rename_pattern_dialog.py`, `parse_filename_dialog.py` | Generalized Rename/Export and Parse-Filename dialogs built on the above |
| `rename_single_file.py` | `rename_single_file()` — quick, direct rename of one file (QInputDialog prompt, current stem pre-filled, extension kept automatically), for fixing a typo without the batch pattern tool above; wraps `core/rename_pattern.py`'s already-generic `rename_file_on_disk()` | mp3, generalized off its own copy — itself independently re-derived from epub's original, which now uses this too (2026-09-23) |
| `sortable_table.py` | `NumericTableWidgetItem` (compares numerically when it can — an optional explicit `sort_value` covers a suffixed display like "128.5 LUFS"/"44100 Hz" whose text alone isn't a bare number — falling back to normal text comparison otherwise) + `suspend_sorting(table)` (a context manager disabling `setSortingEnabled()` for a bulk `setItem()` populate loop, restoring whatever state was in effect before — REQUIRED around one, since Qt re-sorts as items land and can relocate an earlier row's items before a later row is even written) | epub, generalized off nine hand-written copies of the same `was_sorting = table.isSortingEnabled(); ...` block at each of its own bulk-repopulate call sites. Only safe to pair with `Qt.UserRole`-based row→item mapping, NOT list-index-based mapping — see cbzredactor's own deliberate non-native sort implementation, which exists specifically because native sort doesn't fit its architecture |
| `manage_list_dialog.py` | `ManageListDialog` — Add/Remove screen over a hideable-defaults-plus-custom-entries list (Add/Remove Genres, Add/Remove Languages) | epub, already generic (promoted once cbzredactor needed the same pattern) |
| `lookup_dialog.py` | `LookupDialogBase` + `LookupResult` — table (File/Found/Apply) on the left, a detail panel on the right with the selected row's existing/"Current" cover shown side by side with the source's "Found" one (`get_local_cover`, optional -- so a mismatch is obvious at a glance instead of only surfacing after Apply), an editable per-row query-correction form (`query_fields` + "Search This Item", re-runs just that row with the corrected values), and an optional "Other Matches Found" picker (`LookupAlternative` + `resolve_alternative`, opt-in -- a subclass whose own search can return several plausible candidates for one row lists the runners-up there instead of requiring the query text be corrected and re-searched to get a different result) | cbzredactor, generalized off its Comic Vine/GCD lookup dialogs; epub's Google Books/Calibre/Open Library dialogs moved onto it 2026-09-23 (with `auto_search=False` for Calibre's locate-the-tool step, and `accepted_rows()` for applying found covers); the alternatives picker added 2026-09-19 for Comic Vine specifically, after its loose free-text search kept surfacing the wrong release as the top hit |
| `quick_pick_dialog.py` | `QuickPickDialog` — a searchable, fixed-size list-picker popup (filter box + internally-scrolling list + OK/Cancel always visible) for a field's "+" quick-pick button; single- or multi-select, with an optional "Add Custom..." callback | cbzredactor, replacing a flat `QMenu` that overflowed the screen once enough custom genres piled up ("the genre list gets too long to see the apply button") -- epub's Genre/Language pickers moved onto it 2026-09-23 |
| `theme.py` | `apply_theme(app)` — Fusion style + an explicit, WCAG-contrast-verified light/dark QPalette (auto-detected from the OS via `QStyleHints.colorScheme()`), so selection is actually visible in dark mode and looks identical across every app that calls it at startup | cbzredactor ("can't see what is selected in dark mode... want uniform behaviour across the apps") -- wired into epub/mp3/video's own `main.py` too, one line each, since "uniform" was the explicit ask |
| `standard_shortcuts.py` | Canonical shortcut-string constants (`LOAD_FILES`, `SAVE_AS`, `RENAME_SINGLE_FILE`, `RENAME_EXPORT_BY_PATTERN`, `PARSE_FILENAME_TO_METADATA`, `REDO`, `HELP`, ...) for every action shape all four apps share, matching Qt's own `QKeySequence::StandardKey` Windows bindings where one exists (verified via `QKeySequence.keyBindings()`, not assumed) — a project imports these instead of repeating literal key strings. `RENAME_EXPORT_BY_PATTERN`/`PARSE_FILENAME_TO_METADATA` are a deliberate Ctrl+E/Ctrl+I export/import mnemonic pair (changed same-day from an initial Ctrl+Shift+R/Ctrl+E pairing once that pairing was explicitly requested). See its own module docstring for the full rationale and the 2026-09-13 audit that produced it (mp3 missing Ctrl+O entirely, Parse Filename squatting on F3, three different "Save As" keys, videoredactor's Exit bound to a StandardKey that doesn't work on Windows) | 2026-09-13, consumed by cbz/epub/mp3/video |
| `app_bootstrap.py` | `run_app(app_name, window_factory, crash_log_path, app_user_model_id, icon_path)` -- the whole startup sequence: crash logging + "Unexpected Error" dialog, Windows taskbar AppUserModelID, QApplication + icon, `apply_theme()`, `apply_message_box_style()` | 2026-09-23; every `main.py` was a variation of this (video had no crash log or icon) |
| `image_decode.py` | `decode_scaled(bytes, size)` -- decode straight to a target size via `QImageReader.setScaledSize()`, never upscaling; safe on a worker thread | split out of `async_icon_cache.py`, 2026-09-23 |
| `async_preview.py` | `AsyncPreviewLoader` -- ONE preview image (the selected file's cover/thumbnail) loaded + decoded off the GUI thread; debounced, only the latest request delivered | 2026-09-23: cbz decoded full-resolution comic pages on the GUI thread per selection; video ran ffmpeg there |
| `visible_rows.py` | `VisibleRowsWatcher` -- reports the on-screen rows (+ buffer), debounced, on scroll/resize/sort/row changes: the lazy half of epub's lazy cover loading (~126s → ~2.6s for a 15k-book rebuild) | epub, 2026-09-23; now also cbz's table covers |

## Adoption (as of 2026-09-23)

Every app pins `2026-09-23-01` and uses the shared menu bar, shortcuts,
theme, About dialogs, context/column menus, collapsible side panel,
progress dialogs, app bootstrap (crash log, icon, theme), app paths,
subprocess/tool lookup where it shells out, pattern history and the
version bump. Beyond that:

| Module | cbz | epub | mp3 | video |
|---|---|---|---|---|
| Rename/Export + Parse Filename dialogs | ✓ | Rename ✓; Parse keeps its richer local dialog on the shared engine | ✓ | ✓ |
| Search/Replace, Case Conversion dialogs | ✓ | ✓ | — | ✓ |
| Auto-Numbering dialog | ✓ | — | ✓ | ✓ |
| `undo` | ✓ | ✓ | ✓ | ✓ |
| `rename_single_file`, `folder_refresh` | ✓ | ✓ | ✓ | ✓ |
| `sortable_table` | own sort (list-position rows) | ✓ | ✓ | ✓ |
| `column_settings_dialog` (field-key) | ✓ | ✓ (index-based settings migrated once) | ✓ | ✓ |
| `manage_list_dialog` + `managed_list` | ✓ | ✓ | ✓ | own vocabulary editor |
| `quick_pick_dialog` | ✓ | ✓ | ✓ | — |
| `lookup_client` | ✓ | ✓ | — | ✓ |
| `lookup_dialog` | ✓ | ✓ | — | own search/episode pickers |
| Table covers (`visible_rows` + `async_icon_cache`) | ✓ | ✓ | — | — |
| Panel preview (`async_preview`) | ✓ | — | — | ✓ |

History of how the earlier promotions landed is in each app's own
CHANGELOG.md; the two write-ups below are kept for the reasoning they
record.

### The tool_locator promotion, specifically

mp3's original `find_tool()` checked override → bundled `tools/` copy
→ PATH. video's `external_tools.py` only checked override → PATH —
no bundled-dir tier at all, so there was no way to offer video as a
fully portable, no-install-needed distribution the way mp3 could, even
though both projects have the identical "shell out to a real CLI tool"
philosophy (ffmpeg/MKVToolNix for video, mp3val/keyfinder-cli for mp3).

Promoted the three-tier lookup into `core/tool_locator.py`, generalized
so it doesn't need to know anything about a project's own frozen-vs-
dev-mode path resolution (each project still supplies its own
`tools_dir`). Wired video's `get_executable_path()` /
`is_executable_available()` onto it, and added the matching optional
`tools\` → `dist\tools` copy step to video's `build_exe.bat` (mirroring
mp3's) so the new capability is actually reachable, not just present in
code with no way to populate it.

Caught and fixed a real regression while wiring this in: video's
`get_executable_path()` originally returned a configured override
string verbatim, with no existence check at that layer (existence-
gating was `is_executable_available()`'s separate job — a stale
override should fail loudly via the subprocess call itself, not
silently substitute something else). Routing straight through the
shared `find_tool()` broke that, since `find_tool()`'s override
handling gates on existence. Fixed by keeping the "return override
as-is" short-circuit in `get_executable_path()` itself, only handing
off to `find_tool()` for the bundled-dir/PATH fallback when no override
is set. Caught by video's own existing test suite
(`test_override_takes_priority_in_resolved_path`), which is exactly
the point of running it rather than assuming the generalization was
safe. Added two new tests (`TestBundledToolsDir`) proving the new tier
genuinely works — found without PATH or an override, using a real
temp-directory bundled copy, not a mock.

### CollapseToggleButton's missing minimum width (found 2026-09-05)

`CollapseToggleButton.__init__` called `setMaximumWidth(width)` but
never `setMinimumWidth(width)` -- Qt's auto-computed `minimumSizeHint()`
for a `QPushButton` is based on style padding around its glyph, which
on some styles is well over the button's intended ~26px visual width.
A `QSplitter` clamps `setSizes()` against each pane's minimum size, so
`SplitterPaneCollapser.toggle()`'s requested `collapsed_width` (e.g.
32) silently got overridden back up to that larger, invisible floor:
the pane still visibly shrank (so the bug was easy to miss at a
glance), but never actually reached `collapsed_width`, so
`is_collapsed()` never reported `True` and the button got stuck,
unable to toggle back open.

Found while wiring cbzredactor's own side panel onto this (its cover
thumbnail's 60px minimum width made the mismatch large enough to
notice immediately), but the root cause is in this shared button
itself -- every consuming project's collapse toggle is affected until
its pin is bumped past this fix. Also worth checking each project's
own `collapsed_width` constant against whatever its actual panel
content's true minimum width turns out to be (a panel with a wide
minimum-width child, like a cover preview, may need a larger
`collapsed_width` than 32 to actually be reachable -- see cbzredactor's
own `PANEL_COLLAPSED_WIDTH` for the reasoning) -- fixing this button
alone doesn't guarantee 32 is achievable for every panel shape.

## Build scripts

All four projects' `build_exe.bat` now share the same shape: CRLF line
endings (only video's was previously correct for a `.bat` file),
`.spec`-file-based PyInstaller invocation (`python -m PyInstaller
<name>.spec --noconfirm` — epub and mp3 previously used long inline CLI
flag lists), the same failure-message wording (including a PyQt6-install
hint on PyInstaller failure), and the same "this script does NOT bump
the version — run `bump_version.py` yourself first" discipline note
(previously only in video's).

`requirements.txt` dependency floors standardized across all of them:
`PyQt6>=6.6`, `pyinstaller>=6.3` (mp3's had no version pins at all
before).

## Still open

Deliberately left as they are after the 2026-09-23 consolidation --
each is a design decision rather than a copy to delete:

- **epub's Parse Filename dialog** runs on the shared parsing engine but
  keeps its own dialog: it cross-checks each assignment for repetition
  across the batch, sibling files and saved folder metadata, which the
  shared `ParseFilenameDialog` has no equivalent of. Promoting those
  checks into the shared dialog is the natural next step.
- **video's Genre/Language editor** (`gui/vocabulary_editor_dialog.py`)
  is one flat editable list, a simpler model than `ManageListDialog`'s
  hideable-defaults-plus-custom split. Two competing designs for one
  feature; pick one deliberately rather than forcing either.
- **video's TMDB/TheTVDB dialogs** are an interactive search → pick →
  episode-picker flow, not `LookupDialogBase`'s batch "search every row,
  tick Apply" shape. Their HTTP code is on `lookup_client`.
- **"Hiding a column also hides its panel field"** exists twice (cbz's
  form-layout panel, video's grid panel) with different widget
  architectures; sharing it needs design work first.
- **mp3** has no Search/Replace or Case Conversion (no obvious tag use
  case yet) and no cover art -- cover art is next on its roadmap, and
  `visible_rows` + `async_icon_cache` + `async_preview` are ready for it.

## License

Licensed under the [GNU General Public License v3.0 or later](LICENSE).
This package's `gui/` module depends on PyQt6, which Riverbank
Computing licenses under GPL v3 (or a paid commercial license) -- this
project, and every app that consumes it, ships under GPL-compatible
terms to match.

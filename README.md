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
redactor_common @ git+https://github.com/Erlbon/redactor_common.git@2026-09-23-02
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

Currently: `2026-10-09#01`.

## "Move into folders" (third mode of the Rename/Export dialog)

Next to Rename and Export, `RenamePatternDialog` has a third radio that moves files into a
folder tree under a library root. The pattern may contain `/` or `\`:
`%author%/%series%/%title%`, `%albumartist%/%album%/%track% - %title%`,
`%series%/Season %season%/%title%`. Separators are split off *before* tokens are substituted, so a
`/` inside a value never makes a folder; empty folder parts are dropped; `..`, reserved names and
over-long paths are neutralized; a destination that resolves outside the root (symlink/junction) is
blocking and disables Apply. Rename and Export keep stripping separators as before.

```python
dlg = RenamePatternDialog(items, placeholders, get_values, get_path, history, default,
                          library_root=settings.value("move/root", ""),
                          on_library_root_changed=lambda p: settings.setValue("move/root", p),
                          parent=self)
if dlg.exec() and dlg.is_move_mode():
    summary = run_planned_moves(self, dlg.planned_moves(), copy=False,
                                rename_log=self.rename_log, label="Move into folders")
    for item, old, new in summary.done:
        item.path = new          # point the app's own items at the new paths
```

Cross-volume moves copy, verify (size + content fingerprint) and only then send the original to the
Recycle Bin (`send2trash` must be in the app's requirements); if that fails the original is kept.
Undo Last Rename moves files back and offers to remove the folders the move created; a cross-volume
move whose original is in the Bin is reported rather than undone.

## Parse metadata from the folder path

The mirror of "Move into folders": `ParseFilenameDialog` and `core/path_parser.py` read metadata back
out of a file's *folders*. A pattern containing `/` or `\` is a path pattern:
`%genre%/%author%/%series%/%title%`, `%albumartist%/%album%/%track% - %title%`,
`%series%/Season %season%/%title%`. The last segment matches the file stem, earlier ones the parent
folders. Matching runs right to left: a pattern with fewer segments than the path ignores the outer
folders; with more, the outermost pattern segments are reported in `missing_segments` and their
fields stay empty. Each segment uses the normal filename machinery (optional groups, `field_patterns`,
several fields per segment). Patterns without a separator behave exactly as before.

```python
dlg = ParseFilenameDialog(items, placeholders, get_path, history, default, valid_fields,
                          library_root=settings.value("parse/root", ""),
                          on_library_root_changed=lambda p: settings.setValue("parse/root", p),
                          normalizers={"author": author_sort_to_display},   # optional
                          parent=self)
if dlg.exec():
    changes = dlg.accepted_changes()          # {item index: {field: value}}, unchanged contract
    if dlg.is_path_mode():
        conf = {i: r.confidence for i, r in dlg.parse_results().items()}

# headless, e.g. a Redact step:
from redactor_common.core.path_parser import parse_path_detailed, HIGH_CONFIDENCE
r = parse_path_detailed(path, "%genre%/%author%/%title%", library_root, valid_fields,
                        corroborate=lambda field, value: counts.get((field, value.casefold()), 0))
if r.matched and r.confidence >= HIGH_CONFIDENCE:
    apply(r.values)
```

Values stay raw (`Tolkien, J.R.R.` stays as is); an app that wants display names plugs an
author-sort normalizer into `normalizers`. If the path is not under the library root, the last
*n* segments (n = pattern length) are used, never the drive or share name.

Confidence is 0..1: the mean over the pattern's segments, each 1.0 when matched and 0.0 when missing
or non-matching. A folder segment that is only a bare `%field%` (matches any name) scores 0.75; a
segment needing the optional-whitespace retry is scaled by 0.9; `corroborate(field, value)` (how many
items in the batch share that folder value) lifts a folder segment by 0.2 when two or more agree. A
Redact step should auto-apply only results at or above `HIGH_CONFIDENCE` (0.9); the dialog ticks rows
from 0.5 and shows the confidence and matched segments. `parse_path()` returns just the values (None
when the file stem does not match); `parse_path_detailed()` returns a `PathParseResult(values,
confidence, matched_segments, missing_segments, notes)`.

History: path patterns share the pattern history with filename patterns; tell them apart with
`is_path_pattern(pattern)` or `split_pattern_history(history)`.

## core/ — pure logic, no PyQt6 dependency, unit-tested

| Module | What it does | Source |
|---|---|---|
| `table_settings.py` | Field-name-based column visibility/order persistence | video (more robust than epub's index-based original) |
| `rename_pattern.py` | `%field%` pattern → filename. Optional `(...)`/`[...]`/`{...}` groups (dropped when every field inside is empty), legacy token `aliases`, reserved device names checked before the first dot (`CON.mp4`), 150-char cap, `unique_path()`, `rename_file_on_disk()` | epub, generalized off `EpubMetadata` to a plain `dict[str, str]`; optional groups + aliases merged back from epub 2026-09-23, dotted reserved-name check from video |
| `move_plan.py` | "Move into folders" engine: `render_relative_path()` (pattern split on `/` and `\` *before* substitution, each segment sanitized, `..` impossible), `plan_moves()` → `PlannedMove` (collision numbering, folders to create, blocking warning if the destination resolves outside the root or the path is too long), `execute_move()` (same-volume rename; cross-volume verified copy then original to the Recycle Bin; never overwrites), `prune_empty_dirs()` | 2026-09-30 |
| `filename_parser.py` | filename → `%field%` values (reverse of the above). Per-field regex shapes (`field_patterns`: `SERIES_INDEX_FIELD_PATTERN` with ranges/ordinals, `MONTH_FIELD_PATTERN` with month names, `YEAR_FIELD_PATTERN`, ...), per-field `normalizers`, optional groups, `field_value_counts()`. Whitespace is matched strictly first and only loosened if nothing matches -- the loose rule alone split "Jean-Paul Sartre - Nausea" on the inner hyphen | epub, same generalization; epub's later additions merged back 2026-09-23 |
| `path_parser.py` | folder-path parsing, the mirror of `move_plan`: `split_path_pattern()`, `relative_segments()`, `parse_path()` / `parse_path_detailed()` → `PathParseResult` (right-to-left segment matching, confidence, optional corroboration callback), `is_path_pattern()`, `split_pattern_history()`. See "Parse metadata from the folder path" | 2026-09-30 |
| `search_replace.py` | plain/regex search & replace | epub, already generic |
| `case_conversion.py` | UPPER/lower/Title/Sentence case. Title case capitalizes after a colon/dash ("Star Wars: A New Hope") and the first letter rather than first character ("(The End)"); video's short mode names accepted too | epub; video's clause rules merged 2026-09-23 |
| `save_errors.py` | Windows path-too-long detection & messaging | epub, already generic |
| `error_summary.py` | Bounded preview string for a list of error messages | epub, already generic |
| `tool_locator.py` | External CLI tool lookup: override → bundled `tools/` dir → PATH → well-known install folders (`install_dirs`, `windows_program_dirs()`), for installers that don't add themselves to PATH (MKVToolNix, Calibre, Sigil). `which` injectable | mp3 (tiers 1-3); tier 4 from epub's Calibre/Sigil lookup, 2026-09-23 |
| `auto_number.py` | `generate_auto_number()` / `apply_auto_number_to_text_field()` — sequential-number generation for Auto-Numbering | video, already generic |
| `series_numbering.py` | `generate_series_numbers()` — decimal-capable (`Decimal`, not `int`) sequential-value generation, for a "start here, +step per row" quick numbering of a single field (a novella slotted in at "3.5", a comic special issue) | epub, already generic |
| `duplicates.py` | `DuplicateGroup`/`DuplicateMember`, tiers, `sort_groups()`, `DismissStore` + `JsonDismissStore`/`InMemoryDismissStore` for the Find Duplicates review dialog; see "Find Duplicates review dialog" | video, generalized 2026-10-01 |
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
| `secret_store.py` | `get_secret()`/`set_secret()`/`delete_secret()`/`secret_source()`/`keyring_available()` + `migrate_legacy_secret()` -- API keys and passwords in the OS credential store (via optional `keyring`), never scrambled; see "Secrets" below | 2026-09-30; cbz kept Comic Vine/GCD credentials in its ini (password XOR-"scrambled"), video kept TMDB/TVDB/OpenSubtitles keys in plaintext |
| `settings_bundle.py` | Export/Import Settings file: `SettingsAdapter`, `build_bundle()`/`parse_bundle()`/`diff_bundle()`/`apply_bundle()`, secret-key guard; see "Export/Import Settings" below | 2026-09-30; new, no app had it |

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
| `image_label.py` | `AspectRatioImageLabel` -- a QLabel that rescales its pixmap to fit on every resize. Its size hints ignore the pixmap (2026-09-23), so a pane that scaled an image up can still be dragged smaller again | epub, already generic |
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
| `move_runner.py` | `run_planned_moves(parent, planned, copy, rename_log, label)` — executes the Rename dialog's "Move into folders" plan under a cancellable progress dialog (per-file error isolation, `summarize_errors`), records one `RenameLog` batch (so Undo Last Rename restores the moves and offers to remove the folders the move created), then asks once whether to remove the now-empty source folders | 2026-09-30 |
| `rename_single_file.py` | `rename_single_file()` — quick, direct rename of one file (QInputDialog prompt, current stem pre-filled, extension kept automatically), for fixing a typo without the batch pattern tool above; wraps `core/rename_pattern.py`'s already-generic `rename_file_on_disk()` | mp3, generalized off its own copy — itself independently re-derived from epub's original, which now uses this too (2026-09-23) |
| `sortable_table.py` | `NumericTableWidgetItem` (compares numerically when it can — an optional explicit `sort_value` covers a suffixed display like "128.5 LUFS"/"44100 Hz" whose text alone isn't a bare number — falling back to normal text comparison otherwise) + `suspend_sorting(table)` (a context manager disabling `setSortingEnabled()` for a bulk `setItem()` populate loop, restoring whatever state was in effect before — REQUIRED around one, since Qt re-sorts as items land and can relocate an earlier row's items before a later row is even written) | epub, generalized off nine hand-written copies of the same `was_sorting = table.isSortingEnabled(); ...` block at each of its own bulk-repopulate call sites. Only safe to pair with `Qt.UserRole`-based row→item mapping, NOT list-index-based mapping — see cbzredactor's own deliberate non-native sort implementation, which exists specifically because native sort doesn't fit its architecture |
| `manage_list_dialog.py` | `ManageListDialog` — Add/Remove screen over a hideable-defaults-plus-custom-entries list (Add/Remove Genres, Add/Remove Languages) | epub, already generic (promoted once cbzredactor needed the same pattern) |
| `lookup_dialog.py` | `LookupDialogBase` + `LookupResult` — table (File/Found/Apply) on the left, a detail panel on the right with the selected row's existing/"Current" cover shown side by side with the source's "Found" one (`get_local_cover`, optional -- so a mismatch is obvious at a glance instead of only surfacing after Apply), an editable per-row query-correction form (`query_fields` + "Search This Item", re-runs just that row with the corrected values), and an optional "Other Matches Found" picker (`LookupAlternative` + `resolve_alternative`, opt-in -- a subclass whose own search can return several plausible candidates for one row lists the runners-up there instead of requiring the query text be corrected and re-searched to get a different result) | cbzredactor, generalized off its Comic Vine/GCD lookup dialogs; epub's Google Books/Calibre/Open Library dialogs moved onto it 2026-09-23 (with `auto_search=False` for Calibre's locate-the-tool step, and `accepted_rows()` for applying found covers); the alternatives picker added 2026-09-19 for Comic Vine specifically, after its loose free-text search kept surfacing the wrong release as the top hit |
| `duplicates_dialog.py` | `DuplicatesDialog` + `run_find_duplicates()` -- the shared Find Duplicates review dialog (groups with tier + reason, nothing pre-selected, Reveal/Open/Select in list/Not duplicates/Move to Recycle Bin behind a confirm); see "Find Duplicates review dialog" |
| `quick_pick_dialog.py` | `QuickPickDialog` — a searchable, fixed-size list-picker popup (filter box + internally-scrolling list + OK/Cancel always visible) for a field's "+" quick-pick button; single- or multi-select, with an optional "Add Custom..." callback | cbzredactor, replacing a flat `QMenu` that overflowed the screen once enough custom genres piled up ("the genre list gets too long to see the apply button") -- epub's Genre/Language pickers moved onto it 2026-09-23 |
| `theme.py` | `apply_theme(app)` — Fusion style + an explicit, WCAG-contrast-verified light/dark QPalette (auto-detected from the OS via `QStyleHints.colorScheme()`), so selection is actually visible in dark mode and looks identical across every app that calls it at startup | cbzredactor ("can't see what is selected in dark mode... want uniform behaviour across the apps") -- wired into epub/mp3/video's own `main.py` too, one line each, since "uniform" was the explicit ask |
| `standard_shortcuts.py` | Canonical shortcut-string constants (`LOAD_FILES`, `SAVE_AS`, `RENAME_SINGLE_FILE`, `RENAME_EXPORT_BY_PATTERN`, `PARSE_FILENAME_TO_METADATA`, `REDO`, `HELP`, ...) for every action shape all four apps share, matching Qt's own `QKeySequence::StandardKey` Windows bindings where one exists (verified via `QKeySequence.keyBindings()`, not assumed) — a project imports these instead of repeating literal key strings. `RENAME_EXPORT_BY_PATTERN`/`PARSE_FILENAME_TO_METADATA` are a deliberate Ctrl+E/Ctrl+I export/import mnemonic pair (changed same-day from an initial Ctrl+Shift+R/Ctrl+E pairing once that pairing was explicitly requested). See its own module docstring for the full rationale and the 2026-09-13 audit that produced it (mp3 missing Ctrl+O entirely, Parse Filename squatting on F3, three different "Save As" keys, videoredactor's Exit bound to a StandardKey that doesn't work on Windows) | 2026-09-13, consumed by cbz/epub/mp3/video |
| `image_pane.py` | `ImagePreviewBox` (titled image area that fills and rescales to its space, with room for a caption/buttons) + `ImagePanelSplitter` (fields on top, image below, draggable divider; both panes wrapped so the fields scroll instead of squashing and the group box's title can't block panel collapse) -- the resizable cover/thumbnail area of every side panel | 2026-09-23; epub, video and cbz each had their own copy of the splitter, and mp3's cover started out fixed-height |
| `app_bootstrap.py` | `run_app(app_name, window_factory, crash_log_path, app_user_model_id, icon_path)` -- the whole startup sequence: crash logging + "Unexpected Error" dialog, Windows taskbar AppUserModelID, QApplication + icon, `apply_theme()`, `apply_message_box_style()` | 2026-09-23; every `main.py` was a variation of this (video had no crash log or icon) |
| `image_decode.py` | `decode_scaled(bytes, size)` -- decode straight to a target size via `QImageReader.setScaledSize()`, never upscaling; safe on a worker thread | split out of `async_icon_cache.py`, 2026-09-23 |
| `async_preview.py` | `AsyncPreviewLoader` -- ONE preview image (the selected file's cover/thumbnail) loaded + decoded off the GUI thread; debounced, only the latest request delivered | 2026-09-23: cbz decoded full-resolution comic pages on the GUI thread per selection; video ran ffmpeg there. 2026-09-29#04: deleting the owner window mid-load (quitting the app, or pytest's final gc after failing tests) deadlocked on the GIL -- pools are no longer Qt children; `shutdown()` added |
| `visible_rows.py` | `VisibleRowsWatcher` -- reports the on-screen rows (+ buffer), debounced, on scroll/resize/sort/row changes: the lazy half of epub's lazy cover loading (~126s → ~2.6s for a 15k-book rebuild) | epub, 2026-09-23; now also cbz's table covers |

## Building a database from a dump

`core/dump_import.py` + `core/local_db.py` turn a big source dump into a compact, indexed SQLite file
that an app queries offline. Layers: `open_dump()` (streams plain/.gz/.bz2/.xz/.zip with progress) ->
a reader (`iter_xml_records`, `iter_tsv_records`, `iter_jsonl_records`) -> the app's *recipe* (which
records to keep, how they map to tables) -> `SqliteBuilder`. **Never download a dump automatically;
the user supplies the file** (they are many GB). Tests use small synthetic fixtures only.

**Line readers.** `iter_tsv_records(stream, columns, *, delimiter="\t", json_columns=(), min_columns=None,
progress=None, cancelled=None, on_bad_line=None, stats=None, max_line_bytes=64 MiB)` yields dicts
(tuples when `columns` is None); `json_columns` (names, or positions when `columns` is None) are parsed
with `json.loads` (empty -> None). `iter_jsonl_records(stream, *, ...same keywords)` yields one dict per
line. Both take a `DumpStream` (progress = its byte counter) or any file-like with `readline()`; read one
line at a time (flat memory), tolerate CRLF and blank lines, and cope with huge lines (a line over
`max_line_bytes` is skipped and drained in chunks, not loaded). A bad line (broken JSON, too few columns,
not an object, over-long) is skipped and counted in `ReadStats` (`lines/records/blank/bad/first_error`) and
reported to `on_bad_line(lineno, reason, preview)` -- never aborts. A *systematic* mismatch raises
`DumpImportError` instead: most of the first 50 lines with the wrong column count, over 20% of the first
1000 unreadable, or nothing readable at all -- so a changed dump format fails loudly, not as an empty
database. `cancelled()` raises `ImportCancelled`, like `iter_xml_records`.

**Header + null marker (IMDb-style TSV).** `iter_tsv_records(..., header=False|True|"auto", null=None)`
(defaults unchanged). `header=True`: the first line names the columns (a UTF-8 BOM is ignored; the line
isn't counted in `ReadStats`); fields are found BY NAME, so reordered columns work and new extra columns
are ignored, but if any of `columns` is missing from the header (renamed/dropped) it raises
`DumpImportError` before the first record. With `columns=None` it yields dicts keyed by the header names.
`"auto"`: a header when at least half of `columns` appear in the first line (then validated strictly).
`null="\\N"` (backslash + N, as IMDb writes it): a field equal to it becomes `None`; a field merely
containing it is untouched. Example: `iter_tsv_records(dump, ["tconst", "titleType", "startYear"],
header=True, null="\\N")` over `title.basics.tsv.gz`.

**Tar archives + PostgreSQL COPY (MusicBrainz).**
`iter_tar_members(path_or_stream, wanted=None, *, progress=None, cancelled=None)` streams a
`.tar/.tar.gz/.tar.bz2/.tar.xz` (compression sniffed from the data) and yields `(member_name, binary file-like)`
one at a time -- nothing is extracted to disk or loaded into memory. Streaming is forward-only: consume a
member (or ignore it) before asking for the next. `wanted` ("mbdump/artist" or just "artist") skips the others
without reading them into Python and stops at the last wanted member; progress follows the compressed bytes
and `cancelled` is polled while skipping, so cancelling doesn't wait out a multi-GB member. Wrap in
`contextlib.closing()` if you may stop early. `read_small_member(src, name, max_bytes=1 MiB)` returns one small
member's bytes (or None); `read_archive_info(src)` -> `ArchiveInfo(timestamp, schema_sequence,
replication_sequence)` reads the root bookkeeping files and stops where the tables begin;
`check_schema_sequence(info, expected)` (int or several accepted) raises `DumpImportError` "the dump was made
with schema N but this recipe expects M -- update the app" (also when the archive has no SCHEMA_SEQUENCE).
`iter_pgcopy_records(stream, columns, *, null=r"\N", exact=False, progress=None, cancelled=None,
on_bad_line=None, stats=None, max_line_bytes=64 MiB)` reads a PostgreSQL `COPY ... TO` text file (tab-separated,
no header): dicts (tuples when `columns` is None) of text values, `\N` -> None, empty string stays "", escapes
`\t \n \r \\ \b \f \v \NNN \xHH` resolved exactly like PostgreSQL (any other `\c` -> `c`, a trailing lone
backslash is kept; `unescape_pgcopy()` is the helper), `\.` ends the data. Lines without backslashes take a
plain-split fast path. Fewer columns than `columns` = bad line; extra trailing ones are dropped, or bad with
`exact=True` (pass the full verified list + `exact=True` so a changed table fails loudly). Same tolerance and
"format changed" tripwire as `iter_tsv_records`; invalid UTF-8 is replaced and counted in
`ReadStats.replaced`. Usage:

```python
from contextlib import closing
from redactor_common.core import musicbrainz_schema as mb
from redactor_common.core.dump_import import (check_schema_sequence, iter_pgcopy_records, iter_tar_members,
                                              read_archive_info)

check_schema_sequence(read_archive_info(path), mb.SCHEMA_SEQUENCE_EXPECTED)   # the user's mbdump.tar.bz2
with closing(iter_tar_members(path, [mb.table_member("artist")], progress=progress, cancelled=cancelled)) as tar:
    for name, member in tar:
        for rec in iter_pgcopy_records(member, mb.table_columns("artist"), exact=True, cancelled=cancelled):
            ...  # rec["gid"], rec["name"], rec["begin_date_year"] (text or None)
```

**MusicBrainz facts** (verified 2026-10-01 from musicbrainz.org/doc/MusicBrainz_Database/Download and the
musicbrainz-server repo: `admin/sql/CreateTables.sql`, `admin/ExportAllTables`, `lib/MusicBrainz/Script/MBDump.pm`,
`lib/MusicBrainz/Server/Constants.pm`, `lib/DBDefs.pm.sample`; recorded in `core/musicbrainz_schema.py`,
which also holds the verified column list of ~30 tables). The core dump `fullexport/<date>/mbdump.tar.bz2`
(about 7 GB) holds one file per table at `mbdump/<table>`, written by `COPY <table> TO stdout` (default text
format, columns in CreateTables.sql order -- MusicBrainz's own importer relies on that). The archive's ROOT
starts with `TIMESTAMP`, `COPYING`, `README`, `REPLICATION_SEQUENCE`, `SCHEMA_SEQUENCE` (single integer),
then the tables. **Licence:** `mbdump.tar.bz2` is CC0 (public domain); `mbdump-derived.tar.bz2` (annotations,
tags, `*_meta` ratings, `medium_index`...) and the edit/editor/stats/cover-art archives are CC BY-NC-SA
3.0 -- recipes read the core archive only (`mb.DERIVED_TABLES` lists what to avoid). The JSON dumps
(`json-dumps/<date>/release-group.tar.xz`, `artist.tar.xz`, ...) are xz tars of one JSON object per line
(`iter_tar_members` + `iter_jsonl_records`). Guard against schema changes with `check_schema_sequence` (and
`exact=True` column counts). Still the rule: **the user supplies the dump file; never download it.**

**MySQL dumps** (`core/dump_mysql.py`; ISFDB's backup). `iter_mysql_dump(stream, tables, *, progress=None,
cancelled=None, on_bad_line=None, stats=None, max_line_bytes=64 MiB)` streams a `mysqldump` .sql file (`open_dump`
reads it from a .zip/.gz/.bz2/.xz too) and yields `(table, {column: text-or-None})` for the rows of the wanted
`tables` (`{table: [columns wanted]}`); `iter_mysql_table(stream, table, columns)` is the single-table form.
Column names come from the dump's own `CREATE TABLE` blocks, so a recipe names only the columns it wants (any
order) and a table that gains columns keeps working; a wanted table or column that is missing, rows before their
CREATE, or a row whose width differs from its table fail loudly or are bad lines (same tolerance and
"format changed" tripwire as the other readers; `MysqlReadStats.rows` counts rows per table). Handles the default
extended inserts (`INSERT INTO t VALUES (..),(..);` is one line), `--complete-insert`, `\' \" \\ \n \r \t \0 \b \Z`
and doubled-quote escapes, NULL -> None, everything else is text (convert numbers and dates yourself); no
`_binary`/`0x` decoding. Verified on the real ISFDB backup `backup-MySQL-55-2025-12-27.zip` (1.5 GB, 68 tables, UTF-8
text although the tables declare latin1): the nine tables epubredactor reads (8.4 M rows) stream in about 70 s with
no bad line. ISFDB's data is CC BY; its account tables (`mw_user`, `emails`, `web_api_users`) must never be named
in a recipe.

**Writing.** `SqliteBuilder(dest, tables, indexes=(), batch=5000, page_size=8192, cache_mb=200)`: bulk-load
pragmas (journal off, synchronous off, exclusive lock, big cache), `add()`/`add_many()` in executemany
batches, indexes + ANALYZE only after the load, `.partial` renamed at the end. `finish()` still returns the
row counts; afterwards `builder.sizes` has the bytes per table/index and the file total (`"(file)"`).
`create_fts_index(table, columns, name=None, tokenize="unicode61 remove_diacritics 2", *, contentless=False,
prefix=(), optimize=False, progress=None, cancelled=None, chunk=50000)` builds a prebuilt FTS5 index after
the last `add()` (external-content by default: the text isn't stored twice; `contentless=True` is smaller
still, rowid only), filled by rowid range so a progress bar works on millions of rows; recorded in the
info table as `fts.<name>`. The per-session in-memory `NameIndex` doesn't scale to Open Library; use this.

**Querying.** `LocalDatabase.has_table(name)`, `.table_columns(name)`, and
`fts_query(db, fts_table, text, limit=50, *, prefix=True, key=None, from_table=None, columns=None)`:
builds a safe MATCH string (`fts_match_string`: words of `normalize_words(text)`, quoted, AND-ed, prefix
on the last), returns rowids -- or `key` values (an FTS column, or with `from_table` a column of the source
table joined on rowid) -- best bm25 first. Index normalized text if you need `&`/punctuation folded exactly
like `normalize_words`.

**ISBNs.** `core/isbn_norm.py`: `clean_isbn`, `is_valid_isbn10/13` (checksums, `X`), `isbn10_to_13`,
`isbn13_to_10` (None for 979), `normalize_isbn(text, strict=True)` -> canonical 13 digits or None,
`isbn_variants(text)` -> `[isbn13, isbn10]`.

```python
from redactor_common.core.dump_import import SqliteBuilder, open_dump, iter_tsv_records, ReadStats
from redactor_common.core.isbn_norm import normalize_isbn

TABLES = {"edition": ["id integer primary key", "key text", "title text", "isbn13 text"]}
stats = ReadStats()
with SqliteBuilder(dest, TABLES, ["create index ed_isbn on edition(isbn13)"]) as out, open_dump(path) as dump:
    n = 0
    for rec in iter_tsv_records(dump, ["type", "key", "rev", "modified", "json"], json_columns=["json"],
                                progress=progress, cancelled=cancelled, stats=stats):
        j = rec["json"]
        for isbn in j.get("isbn_13", []) + j.get("isbn_10", []):
            if normalized := normalize_isbn(isbn):
                n += 1
                out.add("edition", (n, rec["key"], j.get("title", ""), normalized))
    out.create_fts_index("edition", ["title"], progress=progress)
    out.finish({"source": "Open Library", "bad_lines": stats.bad})
# later: fts_query(db, "edition_fts", "dune frank", 20, key="isbn13", from_table="edition")
```

**Open Library format** (checked 2026-10-01 against openlibrary.org/developers/dumps and the `/type/edition`
definition in the openlibrary repo; nothing downloaded). Dumps: `ol_dump_editions_YYYY-MM-DD.txt.gz`,
`ol_dump_works_...`, `ol_dump_authors_...` (the page links `..._latest.txt.gz` aliases), the all-types
`ol_dump_latest.txt.gz` (editions+works+authors+redirects, ~12 GB), and the complete-history
`ol_cdump_latest.txt.gz` (note `cdump`, not `ol_dump_`). Each line is 5 tab-separated columns: `type`
(`/type/edition`), `key` (`/books/OL…M`), `revision`, `last_modified`, `JSON` (the page's own DuckDB
example addresses it as `column4`, i.e. 0-based fifth). Edition fields (per the type definition):
`title`, `subtitle`, `authors` (list of `{key}`), `works` (list of `{key}`), `publishers`, `publish_date`
(free text), `isbn_10`, `isbn_13`, `number_of_pages` (int), `languages` (`{key: "/languages/eng"}`),
`series`, `subjects`, `by_statement`, `ocaid`, `oclc_numbers`, `lccn`, `publish_places`, `source_records`,
`description`/`notes` (a string or `{type, value}`). Not confirmed from a primary source: `covers` (ids;
absent from the type definition but in the public JSON), `identifiers{}`, the works `authors[{author:{key}}]`
shape, author `name`/`alternate_names`/`birth_date`, `first_publish_date` -- the readers don't care, but
a recipe should use `.get()` everywhere. Other dumps (ratings, reading-log, covers_metadata, ...) have their
own column layouts; pass their `columns`.

## Find Duplicates review dialog

`core/duplicates.py` (Qt-free) + `gui/duplicates_dialog.py`, promoted from videoredactor's Find
Duplicates dialog (2026-10-01) so epub, mp3 and video share one review screen. **Policy: duplicates are
not errors.** The same book can legitimately exist twice (two editions, a wrongly assigned ISBN) and one
recording sits on several releases, so the dialog is a *review aid*: it groups candidates, shows **why**
each group matched (a tier label in words -- Identical / Strong match / Possible match / Weak match -- plus
a reason sentence), **selects nothing by default**, never changes a file unless the user picks an action,
and lets the user mark a group "Not duplicates" so it stops appearing. The apps do the finding; they
return `DuplicateGroup`s.

- `DuplicateMember(item, path, fields, fingerprint)`: `item` is handed back to the app unchanged,
  `fields` maps column key -> display text, `fingerprint` is a **content** identity (hash of the bytes /
  audio / frame data) so a dismissal survives a rename or move. No fingerprint -> the path is used (a
  rename then makes the group reappear, the safe direction).
- `DuplicateGroup(key, tier, reason, members)`; tiers `TIER_IDENTICAL > TIER_STRONG > TIER_POSSIBLE >
  TIER_WEAK` (`tier_label()`, `tier_strength()`); `sort_groups()` orders strongest tier, then larger group.
- Dismissal: `DismissStore` (`is_dismissed`, `dismiss`, `undismiss_all`, `count`, plus an optional
  `undismiss` that enables the per-group "Show This Group Again" button), keyed by `dismissal_key()` = hash
  of the *sorted member fingerprints*. It covers exactly the set reviewed: when a third copy joins (or a
  member leaves) the group is a different set and reappears. `JsonDismissStore(path, max_entries=5000)`
  (lazy, tolerant load; atomic write via temp file + `replace_with_retry`; oldest dropped past the cap; a
  failed write keeps the in-memory state and sets `last_error`) and `InMemoryDismissStore`. An app can
  also implement the four methods over its own settings.
- `DuplicatesDialog(groups, columns, parent, *, title, dismiss_store=None, on_select_in_list=None,
  on_trashed=None, trash=move_to_trash, allow_trash=True, intro_text="")`: tree of groups (group row =
  tier label + reason + file count, member rows = the `columns` values). Buttons: Reveal in Folder, Open
  (also double-click/Enter), Select These in the List (only with `on_select_in_list`; closes the dialog),
  Not Duplicates (Hide This Group) / Show This Group Again plus a "Show N hidden groups" checkbox (only
  with a `dismiss_store`), Move Selected to Recycle Bin... (only when `allow_trash`). Trashing lists the
  files in a confirm that defaults to No, refuses to remove every file of a group (one must stay),
  isolates failures per file, and touches only the selected files. After `exec()`: `.to_select`, `.trashed`.
- `run_find_duplicates(parent, items, find_fn, columns, *, ..., cancellable=True)` runs
  `find_fn(items, progress, cancelled)` on a worker thread under a progress dialog (`progress(done,
  total=None, label=None)` is thread-safe; `cancelled()` is True after Cancel), then opens the dialog.
  Returns the dialog, or `None` if cancelled / nothing found (an info box says so) / `find_fn` raised.

```python
store = JsonDismissStore(str(app_data_dir / "dismissed_duplicates.json"))
COLUMNS = [("name", "File"), ("folder", "Folder"), ("size", "Size")]

def find_fn(books, progress, cancelled):
    groups = []
    for i, (isbn, same) in enumerate(group_by_isbn(books)):
        if cancelled():
            break
        progress(i + 1, len(books), f"Comparing {same[0].path.name}")
        groups.append(DuplicateGroup(
            f"isbn:{isbn}", TIER_STRONG, f"same ISBN {isbn}",
            [DuplicateMember(b, str(b.path), {"name": b.path.name, "folder": str(b.path.parent),
                                              "size": format_size(b.size)}, content_hash(b.path))
             for b in same]))
    return groups

run_find_duplicates(window, loaded_books, find_fn, COLUMNS, dismiss_store=store,
                    on_select_in_list=window.reselect_books, on_trashed=window.remove_books)
```

## Secrets

`core/secret_store.py` keeps API keys and passwords out of settings files.
Resolution order: env var (if you name one) > OS keyring (Windows Credential
Manager / macOS Keychain / Linux Secret Service, service `redactor/<app>`) >
opt-in fallback file > your `legacy` value. There is no scrambling: with no
usable keyring `set_secret()` raises `SecretStoreUnavailable`, and only an
explicit `allow_unencrypted_fallback=True` (ask the user) writes a plainly
named `redactor_<app>_secrets_UNENCRYPTED.json` (0600 on POSIX) in the
per-user config folder.

```python
from redactor_common.core import secret_store as ss

key = ss.get_secret("mp3redactor", "discogs_token", env_var="DISCOGS_TOKEN",
                    legacy=lambda: settings.value("discogs/token", ""))
# once, at startup: move an old cleartext/scrambled value into the store
ss.migrate_legacy_secret("mp3redactor", "discogs_token",
                         read_legacy=lambda: settings.value("discogs/token", ""),
                         clear_legacy=lambda: settings.remove("discogs/token"))
try:
    ss.set_secret("mp3redactor", "discogs_token", new_value)
except ss.SecretStoreUnavailable:
    ...  # tell the user; if they agree: set_secret(..., allow_unencrypted_fallback=True)
```

`gui/secret_field.py`'s `SecretField(app, name, env_var=None)` is the
settings-dialog row: masked edit that never shows the value (empty = keep),
Remove button, and a label naming the source; call `apply()` on OK.

`keyring` is an OPTIONAL dependency (`pip install "redactor_common[secrets]"`),
and the package imports fine without it. Each app should list `keyring` in
its own `requirements.txt` (and bundle it when freezing).

## Export/Import Settings

One `<app>-settings.json` per app (File > Export Settings... / Import
Settings...). `core/settings_bundle.py` owns the format and logic,
`gui/settings_bundle_dialogs.py` the dialogs. The app only writes an adapter:

```python
from redactor_common.core import settings_bundle as sb
from redactor_common.gui.settings_bundle_dialogs import (
    export_settings, import_settings, settings_menu_actions)

class CbzSettings(sb.SettingsAdapter):
    app_slug = "cbzredactor"
    app_version = APP_VERSION

    def sections(self):
        return [sb.SectionSpec("columns", "Column layout"),
                sb.SectionSpec("patterns", "Rename patterns"),
                sb.SectionSpec("tools", "External tool paths", portable=False)]

    def read_section(self, key):   # ALL supported keys, current or default value
        return {"columns": lambda: {"order": s.value("cols/order", [])},
                "patterns": lambda: {"rename": s.value("rename/pattern", "")},
                "tools": lambda: {"ffmpeg": s.value("tools/ffmpeg", "")}}[key]()

    def write_section(self, key, values):   # only known, non-secret, changed keys
        ...

    def redetect_tools(self):      # optional: offered after import
        self.window.locate_tools()

# File menu:  settings_menu_actions(lambda: export_settings(self, adapter),
#                                   lambda: import_settings(self, adapter, on_applied=self.reload_settings))
```

`read_section()` defines which keys an imported file may touch: unknown
sections and keys in a file are ignored. `write_section()` failing only
fails that section. Dropping the adapter's `redetect_tools` disables the
"Re-detect tools" question.

**Portable** (default, ticked): recipes, rename patterns + history +
default patterns, column layout/visibility/order, view options, field
defaults (default language, zero-pad choices), lookup preferences.
**Machine-specific** (`portable=False`, unticked, explicit opt-in): external
tool paths, last-used folders, local database paths, window geometry. On
import, prefer `redetect_tools` over copying another computer's paths.

**Secrets are never included.** Keys named like api_key, password, token,
secret, pin, bearer, credential are dropped on export, on parse, in the
diff and on apply, even if an adapter offers them (`looks_secret()`).

## Preferences dialog

`core/preferences.py` (Qt-free) + `gui/preferences_dialog.py`: one Preferences
dialog for the whole family, storage-agnostic. An app declares its settings as
`PrefSpec`s grouped in `PrefSection`s, implements `PreferencesBackend` (`get(key)`
and `set_many(dict)`) over its own storage, and opens `PreferencesDialog`. The
dialog builds a page per section (checkbox / spin box / combo / text / path with
Browse / editable language combo), shows the plain-language `help` under each
control, greys a control while its `depends_on` checkbox is off, offers OK /
Cancel / Apply and "Reset to Defaults" for the current page, and writes ONLY the
changed keys in one `set_many` call. Cancel writes nothing. More than six pages
switch from tabs to a list. `coerce(spec, raw)` turns whatever the storage returns
(an ini gives strings) into the spec's type and falls back to the default when
the value is missing, malformed or out of range.

Standard sections (settings that are the same idea in several apps), each
accepting subset flags and `overrides={key: {field: value}}`:

- `filenames_section(ascii_filenames=, zero_pad=, auto_number=, width_min=, width_max=)`
- `language_section(style="alpha2"|"alpha3"|"alpha3b", codes=, include_enabled=)`

Shared keys (constants in `core/preferences.py`, use them in the backend and in
the Export/Import adapter):

| Constant | Key | Type | Default |
| --- | --- | --- | --- |
| `KEY_ASCII_FILENAMES` | `ascii_filenames` | bool | off |
| `KEY_ZERO_PAD_NUMBERS` | `zero_pad_numbers` | bool | off |
| `KEY_ZERO_PAD_WIDTH` | `zero_pad_width` | int 1-9 | 2 |
| `KEY_AUTO_NUMBER_PADDING` | `auto_number_padding` | int 1-9 | 2 |
| `KEY_BLANK_LANGUAGE_ENABLED` | `blank_language_enabled` | bool | on |
| `KEY_DEFAULT_LANGUAGE` | `default_language` | language code | en (in the app's code style) |

Settings only one app has (mp3's delete-backups, epub's text wrapping, video's
transcode options, cbz's conversion/resize defaults, tool paths) are declared as
extra `PrefSpec` sections or as `extra_pages=[(title, widget_factory)]`, which
come after the shared sections and manage themselves.

```python
from redactor_common.core.preferences import (
    KEY_ASCII_FILENAMES, KEY_ZERO_PAD_NUMBERS, KEY_ZERO_PAD_WIDTH, KEY_AUTO_NUMBER_PADDING,
    CallbackBackend, filenames_section, language_section)
from redactor_common.gui.preferences_dialog import PreferencesDialog, preferences_menu_action

# --- QSettings-backed app (epub, cbz): map shared keys to the app's own ini keys
_QKEYS = {KEY_ASCII_FILENAMES: "rename/ascii_only", KEY_ZERO_PAD_NUMBERS: "rename/zero_pad_enabled",
          KEY_ZERO_PAD_WIDTH: "rename/zero_pad_width", KEY_AUTO_NUMBER_PADDING: "rename/auto_number_padding"}

def _get(key):  return app_settings._settings().value(_QKEYS[key])      # raw string is fine
def _set(values):
    s = app_settings._settings()
    for key, value in values.items():
        s.setValue(_QKEYS[key], value)

dlg = PreferencesDialog([filenames_section()], CallbackBackend(_get, _set), parent=self)
if dlg.exec():
    self.reload_settings()

# --- ini/dataclass-backed app (mp3, video): one atomic save per set_many
_FIELDS = {KEY_ASCII_FILENAMES: "ascii_filenames", KEY_ZERO_PAD_NUMBERS: "rename_zero_pad",
           KEY_ZERO_PAD_WIDTH: "rename_zero_pad_width", KEY_AUTO_NUMBER_PADDING: "auto_number_padding"}

def _get(key):  return getattr(self.settings, _FIELDS[key])
def _set(values):
    self.settings = dataclasses.replace(self.settings, **{_FIELDS[k]: v for k, v in values.items()})
    save_settings(self.settings)

# Tools menu entry (label, Ctrl+, and the Preferences role come from the family constants):
preferences_menu_action(self.open_preferences)
```

A class with `get` / `set_many` methods works as well as `CallbackBackend`.
`dlg.result_values()` is what was written; `dlg.applied` (signal) fires after each
Apply with the keys just written.

## Redact pipeline

`core/pipeline.py` (engine, Qt-free) + `gui/redact_dialog.py` (dialogs) are the shared "Redact" button: run an ordered **recipe** of steps on the selected files with no operator input. Output is in place (`commit_in_place()` keeps the original until the new file is verified, then sends it to the Recycle Bin; a failed trash keeps a `<stem>.redact-orig<ext>` backup and reports it). Deterministic steps return `StepResult.applied(...)`; a *guess* returns `StepResult.suggestion(value, confidence, reason)` and is applied via the step's `apply_suggestion()` only at confidence >= the recipe threshold (default 0.9), otherwise it lands in the report's NEEDS REVIEW list. A step that raises is recorded as FAILED and later steps still run, unless the step is `required` (then that file is aborted and left untouched). Shortcut: `standard_shortcuts.REDACT` (`Ctrl+Shift+E`).

**Lock retry.** Every rename/replace step of `commit_in_place()` (set the original aside, put the new file in place, the rollbacks, the `new_path` rename) and `RenameLog`'s write is retried for about 1.6 s on a lock error (any `PermissionError`, or on Windows winerror 5/32/33: antivirus, indexer, preview or sync client holding the just-written file) before it counts as failed. `FileExistsError` and `FileNotFoundError` are never retried, and the no-overwrite rule still holds. A `CommitError` caused by a lock that never cleared says "the file may be locked by another program (antivirus, sync client, preview)". The helpers are in `core/os_utils.py`: `retry_on_lock(func, *args, attempts=6, delay=0.15, sleep=None, **kw)`, `replace_with_retry(src, dst)`, `rename_with_retry(src, dst)` (no-clobber) and `is_lock_error(exc)`.

```python
from redactor_common.core.pipeline import OptionSpec, Recipe, Step, StepResult, commit_in_place
from redactor_common.gui.redact_dialog import RecipeEditorDialog, redact_menu_action, run_redact

class FixTitle(Step):                       # deterministic
    key, label = "fix_title", "Tidy title"
    description = "Trim and collapse whitespace in the title."
    def run(self, ctx):                     # ctx = whatever make_context(item) returns
        new = " ".join(ctx.book.title.split())
        if new == ctx.book.title:
            return StepResult.nothing()
        old, ctx.book.title = ctx.book.title, new
        return StepResult.applied(f"title {old!r} -> {new!r}")

class GuessLanguage(Step):                  # a guess with a confidence
    key, label = "language", "Detect language"
    options = (OptionSpec("min_chars", "Minimum text length", "int", 200, 50, 5000),)
    def run(self, ctx):
        lang, conf = detect(ctx.book, ctx.step_options["min_chars"])
        return StepResult.suggestion(lang, conf, "based on the first pages")
    def apply_suggestion(self, ctx, result):
        ctx.book.language = result.value
        return f"language = {result.value}"

CATALOGUE = [FixTitle(), GuessLanguage()]   # default order; default_enabled per step
recipe = Recipe.from_json(settings.value("redact/recipe", ""))   # "" -> defaults
# Operations menu: redact_menu_action(self.on_redact)  ->  MenuAction (Ctrl+Shift+E)
def on_redact(self):
    run_redact(self, self.selected_books(), recipe, CATALOGUE,
               make_context=lambda book: BookCtx(book), describe=lambda b: b.filename)
def on_edit_recipe(self):
    dlg = RecipeEditorDialog(CATALOGUE, recipe, self)
    if dlg.exec():
        settings.setValue("redact/recipe", dlg.recipe().to_json())
```

A step producing a corrected file writes it beside the original and calls `commit_in_place(path, temp_path, verify=...)`; it raises `CommitError` (original intact) on failure. The context may define `close()`; the engine calls it after each file.

**Notes, skips, final save, ordering** (added 2026-09-30#10, all optional and backward compatible):

```python
StepResult.nothing(note="no cover art found")     # FYI -> report's NOTES section
StepResult.skipped("unsaved edits")               # file deliberately not processed:
                                                  # FileStatus.SKIPPED, its own SKIPPED section,
                                                  # remaining steps + finalize don't run

def save_once(ctx, file_report):                  # runs after all steps, unless the file was
    if not file_report.applied:                   # aborted/skipped; returns StepResult | None
        return None                               # (failure -> file FAILED, label "Final save")
    ctx.book.save()
    return StepResult.applied("saved")
run_redact(self, books, recipe, CATALOGUE, make_context=BookCtx,
           finalize=save_once, finalize_label="Save")      # also run_recipe / run_recipe_on_item

class Prefix(Step):
    key, label, position = "prefix", "Add prefix", "last"   # "last" steps always run after the
    options = (OptionSpec("text", "Prefix", "str", "", max_length=20),)  # normal ones; the editor pins them
    def run(self, ctx):
        text = self.options_for(ctx)["text"]              # supported option accessor
        ...
Prefix(default_enabled=False)                             # per-instance default
RedactResultsDialog(report, parent, title, header="Done.", extra_notes=["3 files skipped"])
```

`NOTES` and `SKIPPED` sections are appended to `to_text()` only when non-empty, so a report that uses neither is byte-identical to before. A recipe saved by an older app version gets steps it lacks inserted at their catalogue position (right after the nearest preceding catalogue step), not appended at the end.

**Engine pass 2** (added 2026-09-30#14, all optional and backward compatible). Per file the phases are: `first` steps, normal steps (recipe order), `last` steps, `finalize` (the save), `after_save` steps. The editor pins each group in place.

```python
class Guard(Step):                       # position="first": runs before every normal step
    key, label, position = "guard", "Check file", "first"   # (unsaved edits, load error, DRM)
    def run(self, ctx):
        return StepResult.skipped("unsaved edits") if ctx.dirty else StepResult.nothing()

def save(ctx, rep):                      # finalize may report where the file ended up
    return StepResult.applied("saved", new_path=ctx.new_path)   # -> ctx.saved_path, FileReport.saved_path

class Rename(Step):                      # position="after_save": runs only if the file was saved
    key, label, position = "rename", "Rename", "after_save"     # (skipped + note when unchanged,
    def run(self, ctx):                                         #  failed, skipped; run_when_unchanged=True opts out)
        new = do_rename(ctx.saved_path)  # ctx.saved_path: the current path (None until a step reports one)
        return StepResult.applied(f"renamed to {new}", new_path=new)
class Move(Step):
    key, label, position = "move", "Move into folders", "after_save"
    after = ("rename",)                  # must run after these keys: enforced in ordered_keys()/resolve(),
                                         # the editor refuses the illegal Move up/down and snaps a drag back
```

- **after_save failures**: the file is FAILED under the step's own label, but what was saved stays in `applied` (it *was* saved). A `SKIPPED` result there is only a note; a `required` failure stops the remaining after_save steps without ABORTING the file. `position="last"` keeps its old meaning (before the save).
- **`Step.after`**: keys this step must run after; unknown keys are ignored, a key in a *later* position group or a cycle raises `ValueError`. Recipe order is honoured inside each group.
- **`Step.hidden = True`**: the engine always runs it (even if a hand-edited recipe disables it), the editor doesn't list it and `Recipe.default_for`/the editor never store it.
- **Batch pre-pass**: `Step.prepare(items, env)` is called once per run for each enabled step, before the first file (`run_redact(..., env=...)`, `run_recipe(..., env=...)`, or `begin_run(items, resolved, env)` for your own loop with `run_recipe_on_item(..., run=run)`). A non-None return value lands in `ctx.batch[step.key]`; `ctx.batch` (a dict) and `ctx.run_context` (`RunContext`: `items`, `env`, `batch`, `unavailable`, `notes`) are set on each file's context unless it already defines those names. If `prepare` raises, the step is *unavailable for this run* (skipped for every file, one line in the report's RUN NOTES), never aborting the run. Keep it cheap (use data already loaded), it has no progress dialog of its own. Building e.g. folder-value counts is left to the apps (no generic helper).
- **Clean reports**: when `finalize` fails or skips, the changes steps had applied move from `applied` to `FileReport.not_saved` and the report gets a `NOT SAVED` section (only when non-empty; an app that already clears `applied` itself duplicates nothing). Aborted files and skipped-by-a-step files still list earlier changes under CHANGES as before.
- **Results dialog**: `run_redact(..., header="", extra_notes=None)` passes them to `RedactResultsDialog`.
- **`Step.options_for(ctx)`** is the supported way to read a step's options (every declared option present); `StepResult.nothing(note=...)` adds an FYI to NOTES.

```python
# A different final path (CBR -> CBZ, MKV -> MP4): the verified temp takes the new name WITHOUT
# overwriting anything, then the original goes to the bin/backup; same rollback guarantees.
res = commit_in_place(orig, temp, trash=trash, verify=check, new_path=orig_stem + ".cbz")
res.final_path, res.new_path       # new_path is None for an ordinary same-path commit
# verify may return bool, (bool, "reason") or raise VerifyFailed("reason"); CommitError.reason carries it
def check(temp):
    return (False, "3 pages missing") if pages(temp) < expected else True
```

`commit_in_place(new_path=...)` refuses (CommitError, nothing touched) when `new_path` already exists or its folder is missing; if the rename fails the original is untouched and the temp is left for the caller to clean up; if the original can't be set aside the new file is moved back to the temp name; a failed trash keeps the `.redact-orig` backup and says so in `CommitResult`. A `new_path` equal to the original (case-insensitive on Windows) is the ordinary commit.

**Pattern trail** (rename / move / path-tag patterns; all optional, backward compatible). A saved recipe keeps its stored pattern as saved: later changes to the app's Rename/Export pattern never silently change what Redact does, and the user never has to recreate the recipe. An **empty** stored value means "follow the fallback". The recipe JSON is unchanged (still just the string). Extra `OptionSpec` fields for `kind="str"`:

- `suggestions: Callable[[], list[str]]` -- the app's pattern history, newest first. Its presence makes the editor show an editable combo instead of a plain line edit (de-duplicated; filename patterns first, then path patterns containing `/`).
- `fallback: Callable[[], str]` + `fallback_label: str` -- the pattern used while the stored value is empty, and how to describe it.
- `preview: Callable[[str], str]` -- renders an example result; shown as a `Preview:` line when given.

The editor always captions the option `In effect: <pattern> -- <source>` (updating as you type), where the source is `set in this recipe`, `follows: <fallback_label>` or `default` (`effective_option_source(spec, stored)` returns that pair, for use in your own step code), and a **Use fallback** button clears the field to follow again.

```python
OptionSpec("pattern", "Filename pattern", "str", "",
           suggestions=lambda: rename_history(),              # ["{author} - {title}", ...]
           fallback=lambda: settings.value("rename/pattern", "{title}"),
           fallback_label="the last Rename/Export pattern",
           preview=lambda p: render_pattern(p, SAMPLE_BOOK))
# in the step: value, _src = effective_option_source(spec, self.options_for(ctx)["pattern"])
```

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
| Table covers (`visible_rows` + `async_icon_cache`) | ✓ | ✓ | ✓ | — |
| Panel preview (`async_preview`) | ✓ | — | ✓ | ✓ |

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
  case yet).

## Standard menu skeleton

Every app's menu bar is `File, Edit, View, <app menus>, Tools, Help` (6-8 headings, no Window
menu), with the shared actions under one label, one mnemonic and one shortcut. It is built by
`gui/standard_menus.py` next to the old `menu_builder.build_menu_bar()` (File/Import/Operations/
Settings/Help), which keeps working unchanged until an app migrates.

```python
from redactor_common.core import labels
from redactor_common.gui.menu_builder import MenuAction
from redactor_common.gui.standard_menus import (
    AppMenu, StandardMenuSpec, build_standard_menu_bar, get_action_registry, look_up_submenu,
    standard_edit_items, standard_file_items, standard_help_items, standard_tools_items,
    standard_view_items)
from redactor_common.gui.command_palette import add_command_palette

spec = StandardMenuSpec(
    file=standard_file_items(open_files=self.add_files, open_folder=self.add_folder,
                             save=self.save_selected, save_all=self.save_all, save_as=self.save_as,
                             rename_file=self.rename_one, rename_export_move=self.rename_dialog,
                             remove_from_list=self.remove_files, clear_list=self.clear,
                             exit_slot=self.close),          # slot None = greyed, never hidden
    edit=standard_edit_items(undo=self.undo, redo=self.redo, apply=self.apply_selected,
                             search_replace=self.search_replace),
    view=standard_view_items(show_metadata_panel=self.toggle_panel, refresh_list=self.refresh),
    app_menus=[AppMenu(labels.MENU_METADATA, [
        MenuAction("parse", labels.PARSE_FILENAME, self.parse, shortcut=standard_shortcuts.PARSE_FILENAME),
        look_up_submenu([MenuAction("lu_cv", "Comic &Vine…", self.look_up_cv)])])],
    tools=standard_tools_items(api_keys=self.api_keys, columns=self.columns),
    help=standard_help_items("Comic Redactor", self.changelog, self.credits, self.about))
menus = build_standard_menu_bar(self, spec)        # dict 'File' -> QMenu ...
registry = get_action_registry(self)                # key -> QAction (toolbar, palette, lint)
add_command_palette(self, registry)                 # Ctrl+K
```

- **Labels** (`core/labels.py`, Qt-free): `OPEN_FILES`, `OPEN_FOLDER`, `SAVE`, `SAVE_ALL`,
  `RENAME_EXPORT_MOVE`, `EXPORT_SETTINGS` (`Ex&port Settings…`), `IMPORT_SETTINGS`, `REMOVE_FROM_LIST`,
  `SEARCH_REPLACE`, `LOOK_UP`, `COMMAND_PALETTE`, `CHANGELOG`, `CREDITS`, `about(app_name)`,
  `apply_to_selected(n)`, the `MENU_*` headings, ... Import them, never retype a shared label. Title Case
  for menus; sentence case is for dialogs and tooltips. `strip_mnemonic`, `plain_label`,
  `mnemonic_letter` are the helpers.
- **Builder behaviour**: standard blocks are `standard_{file,edit,view,tools,help}_items()`. Core
  actions with no slot are present but disabled; optional ones (`save_as`, `delete_files`,
  `import_and_convert`, `auto_number`, `filter_list`, every Tools entry) are omitted when `None`. Roles:
  `about` AboutRole, `preferences` PreferencesRole, `exit` QuitRole, NoRole on everything else. `hidden=[...]`
  registers palette-only actions. `set_apply_count(action, n)` keeps 'Apply to N Selected' current.
- **Command palette** (Ctrl+K, `gui/command_palette.py`): lists every menu action as
  `Title   Menu ▸ Submenu   [shortcut]`, substring/fuzzy filter, Enter triggers, disabled actions greyed.
- **Shortcuts**: new constants in `gui/standard_shortcuts.py` (`OPEN_FILES`, `OPEN_FOLDER`, `SAVE_AS`,
  `SAVE_ALL`, `DELETE_FILES`, `APPLY`, `FILTER_LIST`, `COMMAND_PALETTE`, `RESET_ZOOM`, `PREFERENCES`, ...);
  F1 is Help contents, never About.
- **Lint** - call it from each app's test suite:

  ```python
  from redactor_common.gui.menu_lint import lint_menu_bar
  def test_menu_skeleton(qapp):
      assert lint_menu_bar(MainWindow()) == []       # or lint_menu_bar(spec); check_canonical=False while migrating
  ```

  It checks heading order and count (max 8), unique mnemonics per menu, duplicate shortcuts, the
  platform-standard shortcuts (Ctrl+Shift+S is Save As, F1 is not About...) and canonical labels.
- **Migration order** (one check-in per app per step, each with its version bump): (1) bump the pin, add
  the lint test with `check_canonical=False`; (2) label-only pass using the constants; (3) move items
  into the skeleton with shortcuts unchanged; (4) shortcut fixes, keeping each old key as a secondary
  alias for one release with `with_aliases(action, *old_shortcuts)` (`QAction.setShortcuts`); (5) drop
  the old five-menu `build_menu_bar()` call. cbz's former Collection menu becomes a `Collection ▸`
  submenu in Tools.

## License

Licensed under the [GNU General Public License v3.0 or later](LICENSE).
This package's `gui/` module depends on PyQt6, which Riverbank
Computing licenses under GPL v3 (or a paid commercial license) -- this
project, and every app that consumes it, ships under GPL-compatible
terms to match.

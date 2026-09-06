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
redactor_common @ git+https://github.com/Erlbon/redactor_common.git@2026-09-04-10
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

Currently: `2026-09-06#06`.

## core/ — pure logic, no PyQt6 dependency, unit-tested

| Module | What it does | Source |
|---|---|---|
| `table_settings.py` | Field-name-based column visibility/order persistence | video (more robust than epub's index-based original) |
| `rename_pattern.py` | `%field%` pattern → filename | epub, generalized off `EpubMetadata` to a plain `dict[str, str]` |
| `filename_parser.py` | filename → `%field%` values (reverse of the above) | epub, same generalization |
| `search_replace.py` | plain/regex search & replace | epub, already generic |
| `case_conversion.py` | UPPER/lower/Title/Sentence case | epub, already generic |
| `save_errors.py` | Windows path-too-long detection & messaging | epub, already generic |
| `error_summary.py` | Bounded preview string for a list of error messages | epub, already generic |
| `tool_locator.py` | Three-tier external CLI tool lookup: override → bundled `tools/` dir → PATH | mp3, generalized off its `bundled_tool_path()` to a plain `tools_dir` parameter |
| `os_utils.py` | `reveal_in_file_manager()` — cross-platform "show this file in Explorer/Finder" | epub, already generic |
| `lookup_client.py` | `fetch_json()`/`fetch_bytes()` — injectable-`fetch` HTTP GET (JSON-parsed, or raw for e.g. a cover image) + HTTPError/URLError/decode-error → friendly-message translation, plus `make_default_fetch()` for a fixed-User-Agent fetch callable | cbzredactor, generalized off its Comic Vine/GCD lookup modules (which had independently duplicated the identical translation logic twice each — once for the JSON API calls, once for the cover-image download) |
| `undo.py` | `UndoManager` — bounded in-memory undo stack (bulk edits, search/replace, case conversion, lookup-apply, ...), generic via caller-supplied `snapshot_fn`/`restore_fn` | epub, generalized off its original version (which snapshotted `EpubBook`/`EpubMetadata` fields directly) once cbzredactor needed the same "last N in-memory edits" undo behavior |

Run `python3 tests/test_core.py` from this folder's parent to exercise
all of the above (no PyQt6 required). All passing as of this build.

## gui/ — PyQt6 widgets/dialogs

Not runtime-tested in this sandbox (no PyQt6 available) — syntax-checked
and reviewed only, same caveat the source projects already carried.

| Module | What it does |
|---|---|
| `action_factory.py` | `make_action()` — one QAction, shared between menu + toolbar |
| `colors.py` | Shared color palette (row-tint colors, selection highlight) — standardized on epub's scheme; mp3/video had each independently picked their own | epub |
| `menu_builder.py` | Declarative **File / Import / Operations / Settings / Help** builder — enforces identical top-level shape and mnemonics across projects; project-specific menus (e.g. epub's Kobo) insert via `extra_menus`. `populate_menu()` (public) fills any QMenu from the same declarative item list — what `context_menu.py`/`column_menu.py` build their right-click menus on |
| `context_menu.py` | Shared table right-click menu: selection-fix (right-click outside the selection replaces it, matching Explorer) + generic "Open Containing Folder"/"Copy Path", with each project's own actions layered on via `extra_items` | epub, generalized (mp3 and video had no equivalent, or a much thinner one) |
| `column_menu.py` | Shared column-header right-click menu: inline show/hide checklist + a link to `column_settings_dialog.py` | video, generalized (epub only had a "Hide `<this column>`" quick action; mp3 has no column-visibility system to hang this on yet) |
| `image_label.py` | `AspectRatioImageLabel` — a QLabel that rescales its pixmap to fit on every resize | epub, already generic |
| `collapsible_splitter.py` | `SplitterPaneCollapser` (window-side resize/restore logic) + `CollapseToggleButton` (the panel's own "◀"/"▶" button) for a collapsible side-panel splitter | epub, generalized (video had the same 2-pane splitter shape but no collapse mechanism at all; mp3 has no side panel) |
| `grid_utils.py` | `absorb_extra_row_space()` — stops a fixed-row QGridLayout (the bulk-edit tag panels) from spreading leftover vertical space evenly into every row's gap on resize; collects it as blank space below instead | fixes a bug reported on mp3; epub's identically-structured grid had the same latent issue |
| `zoom_toolbar.py` | The +/− table-font-zoom control (epub had it, video didn't — now shared) |
| `column_settings_dialog.py` | "Add/Remove Columns" dialog, built on `core/table_settings.py` |
| `progress.py` | Threshold-gated progress dialog helper (small batches don't flicker a dialog) |
| `qmessagebox_style.py` | App-wide `QMessageBox` max-width fix (one call in `main.py`) |
| `about_dialog.py` | Shared About/Changelog/Credits dialogs (Markdown-rendering, logo, version header with optional `component_versions` + link-back `repo_url`/`component_repo_urls`) — promoted from epub's version |
| `preview_table.py` | Shared "before/after + Apply checkbox" table controller (epub built this pattern twice independently for Search/Replace and Case Conversion — now once) |
| `search_replace_dialog.py`, `case_conversion_dialog.py` | Generalized dialogs built on `preview_table.py` |
| `pattern_field_panel.py` | The ▼ recent-patterns menu + always-visible recent list + clickable placeholder-code side panel (epub v51/v54 UX) |
| `rename_pattern_dialog.py`, `parse_filename_dialog.py` | Generalized Rename/Export and Parse-Filename dialogs built on the above |
| `manage_list_dialog.py` | `ManageListDialog` — Add/Remove screen over a hideable-defaults-plus-custom-entries list (Add/Remove Genres, Add/Remove Languages) | epub, already generic (promoted once cbzredactor needed the same pattern) |
| `lookup_dialog.py` | `LookupDialogBase` + `LookupResult` — table (File/Found/Apply) on the left, a detail panel on the right with the selected row's existing/"Current" cover shown side by side with the source's "Found" one (`get_local_cover`, optional -- so a mismatch is obvious at a glance instead of only surfacing after Apply) and an editable per-row query-correction form (`query_fields` + "Search This Item", re-runs just that row with the corrected values) | cbzredactor, generalized off its Comic Vine/GCD lookup dialogs, which had the same shape as epub's own Google Books/Calibre/Open Library dialogs (not yet migrated onto this -- see "Still open") |
| `quick_pick_dialog.py` | `QuickPickDialog` — a searchable, fixed-size list-picker popup (filter box + internally-scrolling list + OK/Cancel always visible) for a field's "+" quick-pick button; single- or multi-select, with an optional "Add Custom..." callback | cbzredactor, replacing a flat `QMenu` that overflowed the screen once enough custom genres piled up ("the genre list gets too long to see the apply button") -- not yet migrated onto epub's own Genre/Language "+" menus (same flat-`QMenu` shape, same latent issue) -- see "Still open" |
| `theme.py` | `apply_theme(app)` — Fusion style + an explicit, WCAG-contrast-verified light/dark QPalette (auto-detected from the OS via `QStyleHints.colorScheme()`), so selection is actually visible in dark mode and looks identical across every app that calls it at startup | cbzredactor ("can't see what is selected in dark mode... want uniform behaviour across the apps") -- wired into epub/mp3/video's own `main.py` too, one line each, since "uniform" was the explicit ask |

## What's wired in so far

- **epub**: menu bar rebuilt on `menu_builder.build_menu_bar()`; Kobo
  kept as a project-specific extra menu. About/Changelog now use the
  shared `about_dialog.py` (its own local `gui/about_dialog.py` was
  deleted). `.spec`-based build (`epubredactor.spec`, new).
- **video**: menu bar rebuilt the same way; gained the +/− zoom toolbar
  control it never had. `core/table_settings.py` is now a thin wrapper
  delegating to `redactor_common.core.table_settings` — confirmed via
  the project's own 17-test suite, all passing against the delegated
  implementation. About/Changelog swapped from a plain
  `QMessageBox.about()` (which never rendered `ABOUT.md` despite the
  file existing) and a local `MarkdownViewerDialog` (deleted) onto the
  shared dialogs. `core/external_tools.py` gained the bundled-`tools/`
  lookup tier it never had, via the promoted `tool_locator.py` — see
  below. `build_exe.bat` gained the matching optional `tools\` →
  `dist\tools` copy step.
- **mp3**: menu bar rebuilt on the shared shape (Import is present but
  genuinely empty for now — v1 has no external-metadata-source actions
  yet, see the module docstring in its `main_window.py`). Gained
  About/Changelog dialogs it never had at all before, now showing
  `component_versions`. Its three near-identical hand-rolled
  `QProgressDialog` blocks were consolidated onto `progress.py`'s
  `run_with_progress()` — as a side effect this **fixed a real bug**:
  the original dialogs' "Cancel" button did nothing (nothing in the
  code ever checked `wasCanceled()`); it's now genuinely functional.
  `core/tool_locator.py` is now a thin wrapper delegating to
  `redactor_common.core.tool_locator` — confirmed against its own
  5-test suite (run manually; `pytest` isn't installable in this
  sandbox, no network). `.spec`-based build (`mp3redactor.spec`, new).
- **cbzredactor** (new project, 2026-09-05): its Comic Vine and GCD
  lookup modules were the trigger for promoting `lookup_client.py` and
  `lookup_dialog.py` in the first place -- both `core/comicvine_lookup.py`
  and `core/gcd_lookup.py` now build on `fetch_json()`/`make_default_fetch()`
  instead of each keeping its own copy of the HTTPError/URLError
  translation, and both `gui/comicvine_lookup_dialog.py` and
  `gui/gcd_lookup_dialog.py` are now thin `LookupDialogBase` subclasses
  supplying only `search_one()`. Later the same day, real user feedback
  on cbzredactor ("the image preview needs to be much bigger" / "we
  need a way to manually correct the data sent through the lookup")
  drove `lookup_dialog.py`'s bigger redesign: `search_one`'s signature
  gained a `query_override` parameter and `LookupResult` gained
  `used_query`, so a subclass can report what it actually searched
  with and accept a corrected retry for just one row, and the cramped
  in-table cover icon became a large per-row preview in a proper detail
  panel alongside the table.

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

All three projects' `build_exe.bat` now share the same shape: CRLF line
endings (only video's was previously correct for a `.bat` file),
`.spec`-file-based PyInstaller invocation (`python -m PyInstaller
<name>.spec --noconfirm` — epub and mp3 previously used long inline CLI
flag lists), the same failure-message wording (including a PyQt6-install
hint on PyInstaller failure), and the same "this script does NOT bump
the version — run `bump_version.py` yourself first" discipline note
(previously only in video's).

`requirements.txt` dependency floors standardized across all three:
`PyQt6>=6.6`, `pyinstaller>=6.3` (mp3's had no version pins at all
before).

## Still open (not yet wired)

- epub's three existing lookup dialogs (`google_books_dialog.py`,
  `calibre_lookup_dialog.py`, `open_library_dialog.py`) are the exact
  shape `lookup_dialog.py` was generalized from, but haven't been
  migrated onto it -- deliberately deferred (per the user's own call
  when this was scoped) to avoid regression risk in a repo actively
  developed elsewhere, until cbzredactor's usage has proven the
  abstraction out. A natural next candidate once that's confirmed.
- Both epub's and video's `open_search_replace_dialog` /
  `open_case_conversion_dialog` / `open_rename_dialog` /
  `open_filename_parse_dialog` call sites still construct each
  project's own local dialog classes rather than the shared ones in
  `gui/`. The shared versions are ready to drop in (they take accessor
  callables instead of a concrete item type), but swapping each call
  site is its own pass, best done with the ability to actually run
  each dialog afterward rather than blind in a sandbox with no PyQt6.
- epub's `ColumnSettingsDialog` still uses its original index-based
  scheme, not yet upgraded to the shared field-name-based
  `column_settings_dialog.py` — that upgrade also touches epub's
  column-index bookkeeping elsewhere in `main_window.py`, so it's a
  larger, riskier change than the menu-bar swap.
- `core/app_paths.py`'s "frozen? exe's dir : project root two levels
  up" logic is independently reimplemented a third time in each
  project (epub's `core/app_paths.py`, mp3's `core/app_paths.py`,
  video's private `core/config._app_dir()`) — spotted while wiring the
  bundled-tools-dir lookup into video (which needed its own copy of
  this to know where to look), not yet consolidated. A natural next
  candidate, same shape as the other promotions here.
- mp3 has no configurable columns or tag editing yet (deferred per its
  own roadmap until a bulk-edit panel lands), so `table_settings.py`
  and the Search/Replace/Rename/Case-Conversion dialogs aren't wired
  into it — nothing to consolidate there yet, but they're ready and
  waiting once that panel exists.
- epub's own Genre/Language "+" quick-pick menus (`tag_panel.py`,
  `_populate_genre_menu`/`_populate_language_menu`) are the exact same
  flat-`QMenu` shape `quick_pick_dialog.py` replaced in cbzredactor,
  and would hit the same "too long to see past it" problem once epub's
  genre list grows enough -- deliberately not migrated yet, same
  regression-risk-in-a-live-repo reasoning as the lookup dialogs above.

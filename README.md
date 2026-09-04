# redactor_common

Shared interface/UX package for the "Redactor" family of tools (epub,
video, mp3, and future ones). Built from a comparison of the epub and
video projects' independently-built implementations of the same
mechanisms — each module here is the best-of-breed version, generalized
to work on any item type via accessor callables rather than being tied
to one project's data model.

Drop this folder as a sibling of `core/`, `gui/`, and `main.py` in a
project's source tree (that's how it's wired into both the epub and
video projects already). PyInstaller's default static-import analysis
picks it up automatically — no spec-file changes needed as long as it
sits there.

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

Run `python3 tests/test_core.py` from this folder's parent to exercise
all of the above (no PyQt6 required). All passing as of this build.

## gui/ — PyQt6 widgets/dialogs

Not runtime-tested in this sandbox (no PyQt6 available) — syntax-checked
and reviewed only, same caveat both source projects already carried.

| Module | What it does |
|---|---|
| `action_factory.py` | `make_action()` — one QAction, shared between menu + toolbar |
| `menu_builder.py` | Declarative **File / Import / Operations / Settings / Help** builder — enforces identical top-level shape and mnemonics across projects; project-specific menus (e.g. epub's Kobo) insert via `extra_menus` |
| `zoom_toolbar.py` | The +/− table-font-zoom control (epub had it, video didn't — now shared) |
| `column_settings_dialog.py` | "Add/Remove Columns" dialog, built on `table_settings.py` |
| `progress.py` | Threshold-gated progress dialog helper (small batches don't flicker a dialog) |
| `qmessagebox_style.py` | App-wide `QMessageBox` max-width fix (one call in `main.py`) |
| `about_dialog.py` | Shared About/Changelog dialogs (Markdown-rendering, logo, version header) — promoted from epub's version |
| `preview_table.py` | Shared "before/after + Apply checkbox" table controller (epub built this pattern twice independently for Search/Replace and Case Conversion — now once) |
| `search_replace_dialog.py`, `case_conversion_dialog.py` | Generalized dialogs built on `preview_table.py` |
| `pattern_field_panel.py` | The ▼ recent-patterns menu + always-visible recent list + clickable placeholder-code side panel (epub v51/v54 UX) |
| `rename_pattern_dialog.py`, `parse_filename_dialog.py` | Generalized Rename/Export and Parse-Filename dialogs built on the above |

## What's wired in so far

- **epub**: menu bar rebuilt on `menu_builder.build_menu_bar()`; Kobo kept
  as a project-specific extra menu; "About" folded into "Help" to match
  the shared shape.
- **video**: menu bar rebuilt the same way; gained the +/− zoom toolbar
  control it never had; `core/table_settings.py` is now a thin wrapper
  delegating to `redactor_common.core.table_settings` (binds this
  project's `PROTECTED_COLUMNS`, keeps every existing call site
  unchanged) — confirmed via the project's own 17-test suite, all
  passing against the delegated implementation.
- **mp3**: menu bar rebuilt on the shared File/Import/Operations/
  Settings/Help shape (Import is present but genuinely empty for now —
  v1 has no external-metadata-source actions yet, see the module
  docstring in its `main_window.py`); gained About/Changelog dialogs it
  never had at all before (built on `about_dialog.py`, pointed at its
  existing `README.md`/`CHANGELOG.md`); its three near-identical
  hand-rolled `QProgressDialog` blocks were consolidated onto
  `progress.py`'s `run_with_progress()` — as a side effect this also
  **fixed a real bug**: the original dialogs displayed a "Cancel"
  button that did nothing (nothing in the code ever checked
  `wasCanceled()`); it's now genuinely functional.

## Still open (not yet wired)

- Both projects' `open_search_replace_dialog` / `open_case_conversion_dialog`
  / `open_rename_dialog` / `open_filename_parse_dialog` call sites still
  construct each project's own local dialog classes rather than the
  shared ones in `gui/`. The shared versions are ready to drop in (they
  take accessor callables instead of a concrete item type), but swapping
  each call site is its own pass, best done with the ability to actually
  run each dialog afterward rather than blind in a sandbox with no PyQt6.
- epub's `ColumnSettingsDialog` still uses its original index-based
  scheme, not yet upgraded to the shared field-name-based
  `column_settings_dialog.py` — that upgrade also touches epub's
  column-index bookkeeping elsewhere in `main_window.py`, so it's a
  larger, riskier change than the menu-bar swap.
- mp3 project not yet touched at all.

# redactor_common

Shared interface/UX package for the "Redactor" family of tools (epub,
video, mp3, and future ones). Built by comparing the projects'
independently-built implementations of the same mechanisms and
promoting the best-of-breed version of each, generalized to work on
any item type via accessor callables rather than being tied to one
project's data model.

Drop this folder as a sibling of `core/`, `gui/`, and `main.py` in a
project's source tree (that's how it's wired into all three projects
already). PyInstaller's default static-import analysis picks it up
automatically — no spec-file changes needed as long as it sits there.

## Versioning

`redactor_common` is versioned independently of any consuming
project's own `APP_VERSION` — see `core/version.py`
(`REDACTOR_COMMON_VERSION`, same `YYYY-MM-DD#NN` convention each
project's own `bump_version.py` uses). Bump it whenever this package's
code changes. Every project's `AboutDialog` shows it under the app's
own version line (via `component_versions`) — that's the one place to
notice at a glance that a project is running an older vendored copy
than its siblings.

Currently: `2026-09-04#05`.

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

Run `python3 tests/test_core.py` from this folder's parent to exercise
all of the above (no PyQt6 required). All passing as of this build.

## gui/ — PyQt6 widgets/dialogs

Not runtime-tested in this sandbox (no PyQt6 available) — syntax-checked
and reviewed only, same caveat the source projects already carried.

| Module | What it does |
|---|---|
| `action_factory.py` | `make_action()` — one QAction, shared between menu + toolbar |
| `menu_builder.py` | Declarative **File / Import / Operations / Settings / Help** builder — enforces identical top-level shape and mnemonics across projects; project-specific menus (e.g. epub's Kobo) insert via `extra_menus` |
| `zoom_toolbar.py` | The +/− table-font-zoom control (epub had it, video didn't — now shared) |
| `column_settings_dialog.py` | "Add/Remove Columns" dialog, built on `core/table_settings.py` |
| `progress.py` | Threshold-gated progress dialog helper (small batches don't flicker a dialog) |
| `qmessagebox_style.py` | App-wide `QMessageBox` max-width fix (one call in `main.py`) |
| `about_dialog.py` | Shared About/Changelog dialogs (Markdown-rendering, logo, version header, optional `component_versions` line) — promoted from epub's version |
| `preview_table.py` | Shared "before/after + Apply checkbox" table controller (epub built this pattern twice independently for Search/Replace and Case Conversion — now once) |
| `search_replace_dialog.py`, `case_conversion_dialog.py` | Generalized dialogs built on `preview_table.py` |
| `pattern_field_panel.py` | The ▼ recent-patterns menu + always-visible recent list + clickable placeholder-code side panel (epub v51/v54 UX) |
| `rename_pattern_dialog.py`, `parse_filename_dialog.py` | Generalized Rename/Export and Parse-Filename dialogs built on the above |

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

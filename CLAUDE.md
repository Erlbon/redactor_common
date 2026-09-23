# redactor_common

Shared interface/UX package for the Redactor apps (epub, mp3, video, cbz). Contains the best-of-breed version of each shared mechanism, generalized via accessor callables rather than tied to one app's data model. Consumed as a pip dependency pinned to a tag: `redactor_common @ git+https://github.com/Erlbon/redactor_common.git@<tag>`. Never pin `@main`.

## Commands
- Test: `pytest` (tests in `tests/`)
- Release a version: `python bump_version.py` (keeps `core/version.py` `REDACTOR_COMMON_VERSION` and `pyproject.toml` in lockstep, format `YYYY-MM-DD#NN`), update the README's "Currently:" line, commit, `git tag YYYY-MM-DD-NN` (the `#` becomes `-`), then `git push origin main --tags`.
- Then, in each consuming app that needs it: bump the `requirements.txt` pin, run its tests, bump its own `APP_VERSION`, add a `CHANGELOG.md` entry, commit.

## Layout
`core/` (e.g. `tool_locator.py`, `folder_refresh`, `version.py`), `gui/` (e.g. `progress.py` with `run_with_progress()`, `sortable_table`, `menu_builder`, `LookupDialogBase`), `tests/`, `.claude/skills/redactor-conventions/`.

The project skill in `.claude/skills/redactor-conventions/SKILL.md` holds the full family conventions; the summary below is the same content.
## Family conventions (apply to every Redactor repo)
1. **Sync first.** Other Claude sessions, sometimes on other machines, edit these repos concurrently. Before editing, and again right before every `git push`: `git fetch origin -q; git status --porcelain -b`. Confirm you match origin and the tree is clean. Fast-forward if behind; resolve if diverged.
2. **Version bump at check-in.** Any commit that changes real code must, in the same check-in, run `python bump_version.py` and add a matching `CHANGELOG.md` entry, before pushing, without being asked. This was missed twice on epubredactor (2026-09-14, 2026-09-17). `release.ps1` does not bump versions for you.
3. **Progress feedback for any per-book loop.** Any loop over more than a handful of books, including a dialog's preview or scan step, must go through `redactor_common.gui.progress.run_with_progress()`. `cancellable=False` for read-only scans, `True` for applies that mutate. Only exception: a live-typing preview (search/replace, rename pattern) whose per-item work is cheap pure string logic, confirmed by a timing check at large N.
4. **Promote to redactor_common first.** A fix or feature useful to more than one app is built and verified in `redactor_common`, version-bumped, tagged, and pushed. Then each app bumps its `requirements.txt` pin plus its own `APP_VERSION` and `CHANGELOG.md`. Flag a stale pin rather than ignoring it.
5. **Landing page honesty.** erlbon.github.io's Formats table must match the app's real menu actions and file-picker filters, not what the underlying library could theoretically do.
6. **Cross-platform goal.** A Linux/Mac port is planned. Avoid new unguarded Windows-only code (registry, hardcoded `C:\` paths, Windows APIs without a `sys.platform` guard). Follow the existing patterns: PATH-based tool lookup (`redactor_common/core/tool_locator.py`), `QSettings` with `IniFormat` and an explicit path, `sys.platform == "win32"` guards.
7. **PowerShell 5.1 pitfall in release scripts.** Under `$ErrorActionPreference = "Stop"`, a native command's stderr becomes a terminating error. Redirect only stdout (`cmd | Out-Null`), never `2>&1`, and reset `$LASTEXITCODE` after reading it. Also, the release script tags and pushes before `gh release create`, so "tag exists, release doesn't" is a resumable state; check with `gh release view <tag>`.

Full text of these rules: `.claude/skills/redactor-conventions/SKILL.md` in redactor_common.

## Machine-local, not in git
The release scripts (`release.ps1`, `release-<project>.ps1`, `release-all.ps1`) and a hand-placed `upx.exe` lived in a machine-local `_shared-tools` folder that is deliberately not in git. They are not in this repo and must be recreated or copied over by hand.


---
name: redactor-conventions
description: Family-wide conventions for the Redactor apps (epubredactor, mp3redactor, videoredactor, cbzredactor) and their shared redactor_common library -- git sync discipline across repos multiple Claude Code sessions edit concurrently, the promote-to-redactor_common-first pattern, the bump_version.py -> CHANGELOG -> release pipeline (including two real bugs already found and fixed in it, worth not reintroducing), keeping the public landing page's feature claims honest, and the Linux/Mac portability goal. Use this whenever working in any Redactor family repo (C:\Dev\redactor_common, epubredactor, mp3redactor, videoredactor, cbzredactor, redactor-build-tools, or the erlbon.github.io landing page) -- before editing code, committing, bumping a version, building a release, or touching the shared build/release scripts -- even if the user doesn't mention these conventions by name.
---

# Redactor family conventions

The Redactor family is five sibling PyQt6 Windows desktop apps --
**epubredactor**, **mp3redactor**, **videoredactor**, **cbzredactor** --
sharing one library, **redactor_common**, plus two supporting repos:
**redactor-build-tools** (a Docker-based Linux build toolchain, still
untested end-to-end) and the public landing page at erlbon.github.io. All
are real repos under `Erlbon/` on GitHub, cloned at `C:\Dev\<name>`.

**The fact that shapes everything below: other Claude Code sessions edit
these same repos concurrently, sometimes from a different machine.** Never
trust that a working copy on disk still reflects the last thing done here --
verify fresh, every time.

## Sync discipline

Before touching anything in one of these repos:
```
git fetch origin -q
git status --porcelain -b
```
(if `-b` doesn't show an ahead/behind count, no upstream tracking ref is
set on this clone -- compare `git rev-parse HEAD` against
`git rev-parse origin/main` directly instead). Confirm local matches
origin and the tree is clean before editing.

Do this exact check again -- fresh, not from memory of the first one --
immediately before every `git push`. Another session may have pushed
while you were working. If local is now behind, fast-forward merge before
pushing; if it's genuinely diverged, resolve that first.

## Version bump is mandatory at check-in, not just before release

**Any commit to one of the four apps that changes real code (not a
docs-only or version-bump-only commit) must bump that app's own
`APP_VERSION` (`python bump_version.py` in its repo) and add a matching
`CHANGELOG.md` entry, in the same check-in -- before pushing, every
time, without being asked.** This is a hard rule, not a reminder to
consider: treat "I made a real code change" and "I bump the version"
as one atomic action.

Why this is mandatory rather than just good practice: it was missed
TWICE on epubredactor within the same few days (2026-09-14, discovered
only when `release-epubredactor.ps1` hard-errored with "Tag and
Release both already exist" on a version that had never actually been
released with those changes in it; 2026-09-17, a second batch of real
commits landed with no version bump at all, caught only because the
user noticed and had to ask for it explicitly). Waiting until someone
runs the release script -- or until the user has to ask -- means the
version history and `CHANGELOG.md` silently fall behind what's
actually on `main`, which defeats the entire point of both.

**How to apply:** before ending any turn that committed real code to
one of these four repos, check: did this batch of commits bump
`APP_VERSION` and add a `CHANGELOG.md` entry? If not, do it now, as
its own commit, before considering the work done -- don't defer it to
"whenever a release happens next."

## The promotion pattern

If a fix or feature belongs in more than one app, it belongs in
`redactor_common`, not copy-pasted into each app separately -- avoiding
exactly that kind of drift is the entire reason this package exists (see
its own README for the vendored-copies history that motivated the
switch). The sequence:

1. Build and verify the change in `redactor_common` itself.
2. Bump its version: `python bump_version.py` in that repo (writes
   `core/version.py`'s `REDACTOR_COMMON_VERSION`, format `YYYY-MM-DD#NN`).
3. Update `README.md` -- its "Currently:" version line, plus a table
   row or section for the change if it's something a consuming project
   would need to know about.
4. Commit, tag (`git tag YYYY-MM-DD-NN`, matching the version with `#`
   turned into `-`), push commit and tag.
5. In each app that needs the change: bump its `requirements.txt` pin
   (`redactor_common @ git+https://github.com/Erlbon/redactor_common.git@<tag>`),
   bump that app's own `core/version.py` `APP_VERSION` via its own
   `bump_version.py`, add a `CHANGELOG.md` entry, commit, push.

A version pin left behind after `redactor_common` moves on isn't
automatically wrong -- it just means that app hasn't picked up the newer
shared code yet. Worth checking `requirements.txt` against
`redactor_common`'s latest tag when working in an app, and flagging a
lag rather than silently ignoring it.

## Release pipeline

`C:\Dev\_shared-tools\release.ps1` (generic engine) and
`release-<project>.ps1` (per-app wrappers) build the exe, tag the
commit, and publish a GitHub Release. `release-all.ps1` runs all four
in one command, skipping any app already released at its current
version. This tooling is machine-local (`_shared-tools` is
deliberately not git-tracked), unlike `redactor-build-tools`.

`release.ps1` reads whatever `core/version.py` currently says -- it does
NOT bump it for you, and it doesn't detect "there are new commits but
the version string wasn't touched," it only compares the version
string against existing tags/releases. If the mandatory check-in-time
bump above is actually being followed, this should never come up in
practice; it hard-errors with "Tag vX and its GitHub Release both
already exist" if it does (a version that was never actually released
with the pending changes in it).

Two real bugs already found and fixed here (2026-09-14) -- worth
knowing before touching `release.ps1` again, so they don't come back:

- **The tag gets created and pushed *before* the step that can actually
  fail** (`gh release create`). A failure there (an expired `gh` auth
  token did this for real) leaves a tag on origin with no release
  behind it. Treat "tag exists, release doesn't" as a resumable state
  -- check via `gh release view <tag>`, not just tag existence -- and
  skip straight to build+publish rather than refusing the whole run.
- **PowerShell 5.1 wraps a native command's stderr as a terminating
  error under `$ErrorActionPreference = "Stop"`**, even for an
  expected, handled nonzero exit code (e.g. checking whether a release
  exists yet). Redirect only stdout when deliberately checking a
  native command's exit code (`cmd | Out-Null`, never `cmd 2>&1` or
  `cmd *> $null`), and reset `$LASTEXITCODE` right after reading it --
  otherwise it leaks into the whole script's own final exit status.

## Keep the public landing page honest

erlbon.github.io's Formats table (what each app reads/writes/converts)
has been wrong in both directions before: claiming a capability that
was only theoretically possible via the underlying library but never
actually wired into the app's GUI (videoredactor's "any source ->
H.264/AAC MP4" implied arbitrary-format import before that import
dialog existed), and lagging behind a real shipped feature (cbzredactor
gaining CBT/CB7 support). Before describing what an app does there,
verify against the app's actual menu actions and file-picker filters,
not what the backend library could theoretically be made to do.

## Cross-platform goal

A Linux/Mac port is a real intended future goal, not hypothetical --
avoid introducing new unguarded Windows-only assumptions (registry
access, hardcoded `C:\...` paths, a Windows-only API called without a
`sys.platform` guard). The existing patterns are already good models to
follow: `redactor_common/core/tool_locator.py` (PATH-based external
tool resolution, no registry), `QSettings` forced to `IniFormat` with
an explicit path rather than the registry-backed native format, and
`sys.platform == "win32"` guards around anything that's genuinely
Windows-only (with a harmless no-op elsewhere).

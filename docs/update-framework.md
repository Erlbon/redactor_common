# The update framework (approved 2026-11-07)

Decided 2026-11-07: **67A** no manifest signature (GitHub's digests over HTTPS are the trust anchor);
**68B** the gate runs the real update and the uninstall, not only `--check-update`; **69A** `redactor_common`
and Accipio first, here, then each other app in its own session after Accipio's gated release.

One way for every app we build (the Redactor apps, Dossier, Accipio and whatever comes next) to update
itself, with one promise: **an installed or portable copy either updates itself correctly, or says exactly why
it cannot.** A release cannot become "Latest" until the previous release's real updater has installed it.

## Why (what went wrong on 2026-11-06)

Accipio 2026-11-06-04 and -13 could not update: the shared code had learned to spot installed copies and
built the file name `accipio-setup-setup.exe`, which no release had, and the update box quietly offered only
"Open Download Page". Behind that one bug sat four weaknesses:

1. **The file to download was guessed** from two separate pieces (the app's slug plus "am I installed?"),
   and the release had to happen to contain a file of that exact name.
2. **"Installed" was guessed** from Inno Setup's `unins000.exe` sitting beside the exe.
3. **Failure was silent**: no reason, just the download page.
4. **Nothing tested the real path**: no old installed copy was ever asked what it would do with a new release.

And two that would have bitten next: the family has **two incompatible restart switches** (`/RESTARTAPP=1`,
which only Dossier's installer reads, and Accipio's `/relaunch=yes`), and the **release scripts live only on
one laptop** (`C:\Dev\_shared-tools`, not in git). Three apps also announce a Linux download that their
latest release does not have.

## The design

### 1. A manifest in every release

Every release carries one more file under a name that never changes, `update-manifest.json`:

```json
{
  "schema": 1,
  "app": "accipio",
  "tag": "v2026-11-06-14",
  "version": "2026-11-06#14",
  "min_client": 1,
  "packages": {
    "windows-installer": {"file": "Accipio-2026-11-06-14-windows-x64-setup.exe", "size": 36839029, "sha256": "…"},
    "windows-zip":       {"file": "Accipio-2026-11-06-14-windows-x64.zip",       "size": 44330153, "sha256": "…"}
  }
}
```

- The app reads the manifest and **never builds a file name**. Each package is listed under its **channel**
  (below), with its exact file, size and SHA-256.
- The manifest is checked against GitHub's own digest for it, and every package it names must be an asset of
  the same release with the same size and digest (a manifest cannot point outside its release).
- `schema` lets the format grow; a client meeting a newer schema than it knows says so plainly.
  `min_client` lets a release say "updaters older than N cannot install me; use the download page".

### 2. The channel is declared, never guessed

Every build says how it was put on the PC, in a one-line file `update-channel` beside the exe:

| Channel | Written by | How it updates itself |
|---|---|---|
| `windows-installer` | the Inno Setup installer, at install time | runs the new installer, silently, and starts again |
| `windows-exe` | the one-file build (Redactor apps, Dossier's portable exe) | swaps the exe (`.old` kept until the next start) and restarts |
| `windows-zip` | the folder build in the zip | cannot replace itself: says so and opens the release page |
| `linux-tarball` | the Linux build | cannot replace itself yet: says so and opens the release page |
| `source` | no file, not frozen | "update it with git pull" |

The PyInstaller build bundles its default channel; the installer overwrites the file beside the exe with
`windows-installer`. A frozen copy with no channel file says "this copy cannot tell how it was installed"
instead of guessing.

### 3. One implementation, one installer contract

- **All updating lives in `redactor_common`**: fetching, verifying, the update box, the exe swap and the
  installer run (`core/update_manifest.py`, `core/selfupdate.py` `decide()`, `gui/update_controller.py`).
  Apps pass settings, not code. Accipio's own update module is gone. An app whose window does not end the
  program when closed passes `quit_app` (its File > Exit) to `UpdateController`; Accipio does not need it,
  since its `close()` from code quits for real.
- **One installer contract** for every Inno Setup installer, `installer/update-contract.iss`, which each
  app's `.iss` includes at its end (`#include UpdateContract`; the build passes the path from
  `python -m redactor_common.release contract-path`), so it cannot drift per app:
  - the switch is `/relaunch=yes` (Dossier's installer accepts the old `/RESTARTAPP=1` for one transition);
  - the installer writes `update-channel` = `windows-installer`;
  - before replacing files it asks the running program to stop (`#define UpdateQuitArgs "--quit"`, guarded
    by an optional `UpdateQuitCheck` function), waits up to 30 seconds until the exe is free, and only then
    lets CloseApplications close what is left. It uses Inno Setup 6 event attributes, which run before the
    app's own event functions, so the app keeps its own `PrepareToInstall` and `CurStepChanged`.
- The update itself: download to the temp folder, check size and SHA-256, start the installer detached with
  the contract's switches, then quit the app normally (so it saves its state). The installer starts the new
  version when it is done.

### 4. Loud failures

Every "cannot update" has its own message in the update box, saying what was looked for and what to do, plus
a "Copy details" button (version, channel, release, reason) for a bug report. The reasons:

- the release has no `update-manifest.json` (a release made before this framework);
- the manifest's schema is newer than this copy knows, or `min_client` is above it;
- the release has no package for this copy's channel (it names the channel);
- a size or checksum does not match (the download is thrown away);
- the program folder cannot be written to (exe channel);
- the copy cannot tell how it was installed (no channel file).

"Open Download Page" is always offered too, but as a fallback next to the reason, never instead of one.

### 5. `--check-update` and `--update` from the command line

Every app gets, through the shared start-up code:

- `App.exe --check-update [--release-tag vX]` prints, as JSON, what this copy would do with the latest
  release (or with a given one, a pre-release included): its channel, the release, the action (`install`,
  `page` or `none`), the file, and the reason. Nothing is downloaded or changed.
- `App.exe --update [--release-tag vX]` does the whole update without any window: download, verify, install,
  restart. It exists for the release gate below.
- `REDACTOR_CONFIG_DIR` (any app) points the settings folder somewhere else, so a test copy never touches the
  real profile (Accipio's test runs also used to repoint Chrome's link to Accipio).

### 6. The release gate

Release tooling moves into `redactor_common` (`python -m redactor_common.release`), in git, used by every app.
Publishing a release goes like this:

1. Build, and write `update-manifest.json` from the built files (the tool refuses a release that lacks a
   package the app declares it ships, such as Linux).
2. Publish it as a **pre-release** (not yet "Latest", so nobody's copy sees it).
3. Download every asset back and check it against the manifest.
4. **Gate:** for each channel the app ships, take the **previous Latest release**, put it in a scratch folder
   with a scratch profile (installer: really install it; exe: just the file), and run its
   `--check-update --release-tag <new>`; it must say `install` with the right file. Then run its
   `--update --release-tag <new>` for real and check that the new version is what now runs. Then uninstall
   the scratch copy and remove the scratch folder.
   With `[tool.redactor-release.gate]` (`run`, `quit`, `crash_log`, `live`; added 2026-11-07, 81A) the gate
   also runs the program as a user does: the old copy is running while it updates itself and must stop by
   itself (exit 0; the installer log must show no program closed by force), and afterwards the new program
   must start, keep running for `live` seconds with no crash log, and stop when asked.
   **When the app is installed on the PC that releases** (the owner's option D, 2026-11-11), the installer
   channel uses that copy instead of a scratch install: with its own profile it runs `--check-update` and
   `--update` to the new release, the installer must finish with exit 0 and close nothing by force, and the copy
   must then report the new version and channel. It stays installed. This is how browsers test their builds on
   their developers' own copies. Before this the gate refused to run until the app was uninstalled, because a
   scratch install takes over the real one's uninstall entry, shortcuts and taskbar id. Where the app is not
   installed, the scratch install is used as before; the one-file exe is always tested in its scratch folder.
5. Only then mark the release "Latest".

If the gate fails, the release stays a pre-release and nothing reaches anyone. The first release of each app on
this framework has no previous release that speaks `--check-update`; the tool then says so and needs an
explicit `--first-framework-release` to go on (those copies are updated by hand once, as decided for Accipio
#04 and #13).

### 7. Tests

- `redactor_common`: the manifest (reading, every way it can be wrong), the channel file, each failure
  message, the update box offering the right buttons, the exe swap and the installer start (with fakes), and
  the release tool (with a fake `gh`).
- Each app: a contract test that its `.iss` includes the shared installer contract and that its build writes
  the right default channel.
- The gate above is the end-to-end test, on real builds, before every release.

## Rollout

1. `redactor_common`: everything above, tagged.
2. Accipio: moved onto it (its own update module removed), then #14 released through the gate. Copies on
   2026-11-06-04 and -13 are updated by hand (decided: no bridge file).
3. Dossier, then cbzredactor, mp3redactor, videoredactor, epubredactor and folderredactor, each moved onto it
   and released through the gate. One shared pin for the whole family at the end.

Out of scope for now: Linux copies replacing themselves (they get a clear message and the page), and Pluteus
(a server, updated with Docker).

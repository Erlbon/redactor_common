"""
redactor_common/gui/standard_shortcuts.py

Canonical keyboard shortcuts shared across every Redactor app. Import
these into a main_window.py's MenuAction specs instead of repeating
literal key-sequence strings -- that's what let the four apps drift
apart in the first place (mp3redactor missing Ctrl+O entirely, Parse
Filename squatting on F3 while cbz/epub/mp3 each spelled it out
separately, "Save As" on three different keys depending which app you
were in, videoredactor's Exit binding to a StandardKey that doesn't
actually work on Windows, ...). See redactor-family-repo-location
memory / the 2026-09-13 hotkey audit for the full before/after.

Chosen to match Qt's own QKeySequence::StandardKey platform bindings
on Windows wherever one exists -- verified directly via
`QKeySequence.keyBindings()` on this machine, not assumed from docs --
so they also match what every other Windows app's user already
expects. Where no standard exists (Load Folder, Rename/Export by
Pattern, Parse Filename), the choice is the family's own existing
convention (or, for Parse Filename, a free key picked deliberately
since its old slot, F3, is reserved for Find/Find-Next everywhere
else).

A project should use these as-is for any action listed here. A
project-specific action with no equivalent below (TMDB lookup, BPM
detection, ...) just picks its own key normally -- this module only
covers the shapes every app shares.
"""

# --- File ---
LOAD_FILES = "Ctrl+O"  # QKeySequence::Open
LOAD_FOLDER = "Ctrl+Shift+O"  # no StandardKey for "open folder specifically"; family convention
SAVE = "Ctrl+S"  # QKeySequence::Save
SAVE_AS = "Ctrl+Shift+S"  # QKeySequence::SaveAs
SAVE_ALL = "Ctrl+Shift+A"  # no StandardKey for "save all"; distinct from SAVE_AS on purpose
REMOVE_FROM_LIST = "Delete"  # QKeySequence::Delete
REFRESH_LIST = ["F5", "Ctrl+R"]  # QKeySequence::Refresh is F5; Ctrl+R kept as the family's existing bonus alt

# Quick, direct rename of the one selected file -- matches Windows
# Explorer's F2 exactly. Distinct from RENAME_EXPORT_BY_PATTERN below;
# see redactor_common.gui.rename_single_file for the shared function
# this should call.
RENAME_SINGLE_FILE = "F2"

# The pattern-based BATCH rename/export tool (was on F2 in cbz/epub/mp3
# until this shortcut audit -- F2 is Explorer's rename key and
# shouldn't be spent on a dialog instead of the direct rename above).
# videoredactor already used this key for the same shape of feature
# before this audit; the other three are being aligned onto it.
RENAME_EXPORT_BY_PATTERN = "Ctrl+Shift+R"

# The reverse direction of the above: pulling metadata OUT of a
# filename instead of building one FROM metadata. Was F3 in cbz/epub/
# mp3 -- moved because F3 is QKeySequence::FindNext everywhere else
# and a metadata tool has no business sitting on the search key.
PARSE_FILENAME_TO_METADATA = "Ctrl+E"

# --- Operations ---
UNDO = "Ctrl+Z"  # QKeySequence::Undo
REDO = "Ctrl+Y"  # QKeySequence::Redo (Ctrl+Shift+Z also works via Qt's own alt binding)
SEARCH_REPLACE = "Ctrl+H"  # QKeySequence::Replace

# --- Help ---
HELP = "F1"  # QKeySequence::HelpContents

# Deliberately NOT listed here: Exit. Alt+F4 already closes any of
# these apps' plain QMainWindow -- verified directly (launch, send
# Alt+F4, confirm the process actually exited) -- since it's Windows
# itself sending the close request to the focused window, independent
# of anything the app binds in code. Giving Exit its own explicit
# shortcut just risks it lying (videoredactor's old
# QKeySequence.StandardKey.Quit bound to nothing at all on Windows,
# confirmed via QKeySequence.keyBindings()) or drifting out of sync
# with this file for a key nobody needed. Leave shortcut=None.

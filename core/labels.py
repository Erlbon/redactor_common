"""
redactor_common/core/labels.py

Canonical menu-label constants for the shared vocabulary of the Redactor
apps (the "menu skeleton", see README "Standard menu skeleton"). Apps
import these instead of retyping a label, so "Open Files…" is spelled,
capitalised and given its mnemonic the same way in all four.

Qt-free on purpose: a plain-strings module that core/ code, tests and
lint can use without a QApplication. Mnemonics (`&`) were chosen so every
label is unique within the menu it lives in, in every app (the check is
machine-enforced by gui/standard_menus.lint_menu_bar). Title Case for menu
items; sentence case is for dialogs and tooltips only. A trailing `…`
means "opens a dialog that needs input".

Where two apps need a different letter for the same family of action
(Find Duplicates sits beside "Deduplicate Manifest IDs" in epub's Repair
menu) a second `*_ALT` constant is provided rather than a per-app retype.
"""

from __future__ import annotations

# --- Top-level headings -------------------------------------------------------
MENU_FILE = "&File"
MENU_EDIT = "&Edit"
MENU_VIEW = "&View"
MENU_METADATA = "&Metadata"
MENU_REPAIR = "&Repair"
MENU_SEND = "&Send"
MENU_ANALYZE = "&Analyze"
MENU_MEDIA = "Me&dia"
MENU_TOOLS = "&Tools"
MENU_HELP = "&Help"

# --- File ---------------------------------------------------------------------
OPEN_FILES = "&Open Files…"
OPEN_FOLDER = "Open &Folder…"
IMPORT_AND_CONVERT = "Import and Con&vert…"
SAVE = "&Save"
SAVE_AS = "Save &As…"
SAVE_ALL = "Save A&ll"
RENAME_FILE = "&Rename File…"
UNDO_LAST_RENAME = "&Undo Last Rename"
RENAME_EXPORT_MOVE = "Rename / &Export / Move…"
# The old '&Export Settings…' / 'I&mport Settings…' collided with
# Rename / &Export / Move and Re&move from List.
EXPORT_SETTINGS = "Ex&port Settings…"
IMPORT_SETTINGS = "&Import Settings…"
REMOVE_FROM_LIST = "Re&move from List"
CLEAR_LIST = "&Clear List"
DELETE_FILES = "&Delete Files…"
EXIT = "E&xit"

# --- Edit ---------------------------------------------------------------------
UNDO = "&Undo"
REDO = "&Redo"
REDACT = "Redac&t"
EDIT_REDACT_RECIPE = "&Edit Redact Recipe…"
SEARCH_REPLACE = "Search and Rep&lace…"
CHANGE_CASE = "&Change Case…"
AUTO_NUMBER = "Auto-&Number…"
FILTER_LIST = "&Filter List"


def apply_to_selected(count: int | None = None) -> str:
    """'Apply to N Selected' (dynamic text; 'Apply to Selected' when the
    count is unknown). The caller disables the action at 0."""
    return "&Apply to Selected" if count is None else f"&Apply to {count} Selected"


# --- View ---------------------------------------------------------------------
COMMAND_PALETTE = "Command &Palette…"
SHOW_METADATA_PANEL = "Show &Metadata Panel"
ZOOM_IN = "Zoom &In"
ZOOM_OUT = "Zoom &Out"
RESET_ZOOM = "&Reset Zoom"
REFRESH_LIST = "Re&fresh List"
TEXT_WRAPPING = "&Text Wrapping"

# --- Metadata / app menus -----------------------------------------------------
PARSE_FILENAME = "&Parse Filename…"
LOOK_UP = "&Look Up"  # the submenu; its entries are per-app ("Comic Vine…")
FIND_DUPLICATES = "Find &Duplicates…"
FIND_DUPLICATES_ALT = "Find D&uplicates…"  # epub Repair / mp3 Analyze
VALIDATE_AND_FIX = "&Validate and Fix…"

# --- Tools --------------------------------------------------------------------
PREFERENCES = "&Preferences…"
API_KEYS = "API &Keys…"
EXTERNAL_TOOLS = "External &Tools…"
COLUMNS = "&Columns…"
GENRES = "&Genres…"
LANGUAGES = "La&nguages…"

# --- Help ---------------------------------------------------------------------
CHANGELOG = "&Changelog…"
CREDITS = "C&redits…"


def about(app_name: str) -> str:
    """'About <App>' (AboutRole; no shortcut, F1 is Help contents)."""
    return f"&About {app_name}"


def strip_mnemonic(text: str) -> str:
    """Display text without mnemonic markers: '&Open Files…' -> 'Open Files…'.
    '&&' is a literal ampersand and survives as a single '&'."""
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "&":
            if i + 1 < len(text) and text[i + 1] == "&":
                out.append("&")
                i += 2
                continue
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def plain_label(text: str) -> str:
    """strip_mnemonic() plus no trailing ellipsis: the form the command
    palette searches ('Open Files')."""
    return strip_mnemonic(text).rstrip("…. ").rstrip()


def mnemonic_letter(text: str) -> str | None:
    """The lower-cased mnemonic letter of `text`, or None if it has none."""
    i = 0
    while i < len(text) - 1:
        if text[i] == "&":
            if text[i + 1] == "&":
                i += 2
                continue
            return text[i + 1].lower()
        i += 1
    return None

"""
redactor_common/gui/label_wrap.py

Family rule (owner, 2026-10-18): NO QLabel may be wider than 80% of the screen's available width. A label whose
natural single-line width would be MUST word-wrap, so text never runs off the window edge (trigger: a ~400-char intro
in epubredactor's Remove Running Headers dialog shown as one cut-off line).

  * enforce_label_wrap(label): wraps one label if needed (never touches short or already-wrapped labels).
  * install_label_wrap_guard(app): one application-level event filter that runs enforce_label_wrap on every QLabel
    when it is shown, so apps do not have to remember. run_app() installs it; apps with their own bootstrap call it.
  * lint_label_wrap(folder): flags inline `addWidget(QLabel("<long literal>"))` labels that never call setWordWrap.
"""

from __future__ import annotations

import re
from pathlib import Path

from PyQt6.QtCore import QEvent, QObject
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QLabel

MAX_LABEL_SCREEN_FRACTION = 0.8

_GUARD_PROPERTY = "_redactor_label_wrap_guard"


def _available_width(label: QLabel) -> int:
    """Available geometry width of the screen the label is on (primary screen as the fallback); 0 if unknown."""
    screen = None
    try:
        window = label.window()
        handle = window.windowHandle() if window is not None else None
        screen = handle.screen() if handle is not None else None
    except RuntimeError:
        screen = None
    if screen is None:
        screen = QGuiApplication.primaryScreen()
    if screen is None:
        return 0
    return screen.availableGeometry().width()


def enforce_label_wrap(label: QLabel) -> bool:
    """Turns word wrap on when the label's single-line width exceeds MAX_LABEL_SCREEN_FRACTION of the screen's
    available width. Returns True if it changed the label."""
    if label.wordWrap():
        return False
    available = _available_width(label)
    if available <= 0:
        return False
    if label.sizeHint().width() <= MAX_LABEL_SCREEN_FRACTION * available:
        return False
    label.setWordWrap(True)
    return True


class _LabelWrapGuard(QObject):
    def eventFilter(self, obj, event):  # noqa: N802 (Qt API)
        try:
            if event.type() == QEvent.Type.Show and isinstance(obj, QLabel):
                enforce_label_wrap(obj)
        except Exception:  # noqa: BLE001 - a guard must never break the app
            pass
        return False


def install_label_wrap_guard(app) -> bool:
    """Installs the application-level guard once per QApplication. Returns True if installed now, False if it
    was already there."""
    if app.property(_GUARD_PROPERTY):
        return False
    guard = _LabelWrapGuard(app)
    app.installEventFilter(guard)
    app.setProperty(_GUARD_PROPERTY, True)
    return True


# --- the lint ---------------------------------------------------------------------------------------------------

LINT_MIN_LITERAL = 100

_INLINE_LABEL = re.compile(r"""addWidget\(\s*QLabel\(\s*f?(["'])(.*?)\1""")


def lint_label_wrap(folder: Path | str) -> list[str]:
    """Problems found in the .py files under `folder` (one readable string per place): an inline
    `addWidget(QLabel("<string literal over ~100 chars>"))` whose module never calls setWordWrap. Add
    '# wrap-ok: <reason>' on the line to exempt it. Empty list = the rule holds."""
    problems: list[str] = []
    root = Path(folder)
    for path in sorted(root.rglob("*.py")):
        if any(part in {"tests", "__pycache__", "build", "dist"} for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "setWordWrap" in text:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("#") or "wrap-ok" in line:
                continue
            match = _INLINE_LABEL.search(line)
            if match and len(match.group(2)) > LINT_MIN_LITERAL:
                problems.append(
                    f"{path.name}:{number}: a long inline QLabel that never word-wraps: create it, call "
                    "setWordWrap(True), then addWidget (or add '# wrap-ok: <reason>')"
                )
    return problems

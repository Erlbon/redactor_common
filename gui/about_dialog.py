"""
redactor_common/gui/about_dialog.py

Two small read-only info dialogs: About (logo + version info + the
content of an About/README markdown file) and a Changelog viewer.
Both render real Markdown -- via Qt's own built-in Markdown support
(QTextDocument.setMarkdown(), which QTextBrowser exposes directly),
rather than showing raw ## headers and [link](url) syntax as flat text.
No editing, no external fetches, nothing more complex than reading a
bundled file and letting Qt render it.

Promoted from the epub project's version, which was already almost
fully generic -- only change here is taking app_name/version/
release_label as constructor parameters instead of importing them from
a fixed core.version module, so every project (including one with no
About dialog at all yet, like mp3) can use this directly.
"""

from __future__ import annotations

import os

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QTextBrowser, QVBoxLayout


def _read_text_file(path: str) -> str:
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _markdown_browser(markdown_text: str) -> QTextBrowser:
    browser = QTextBrowser()
    browser.setOpenExternalLinks(True)  # links open in the system browser, not in-app
    browser.setMarkdown(markdown_text or "*(nothing to show)*")
    return browser


class AboutDialog(QDialog):
    def __init__(
        self,
        app_name: str,
        app_version: str,
        release_label: str,
        icon_path: str,
        about_path: str,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"About {app_name}")
        self.resize(520, 560)

        layout = QVBoxLayout(self)

        if icon_path and os.path.exists(icon_path):
            logo_label = QLabel()
            pixmap = QPixmap(icon_path).scaled(
                96, 96, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
            logo_label.setPixmap(pixmap)
            logo_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            layout.addWidget(logo_label)

        header = QLabel(f"<h2>{app_name}</h2><p>{release_label}, ver {app_version}</p>")
        header.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        header.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(header)

        about_text = _read_text_file(about_path)
        layout.addWidget(_markdown_browser(about_text), 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


class ChangelogDialog(QDialog):
    def __init__(self, changelog_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Changelog")
        self.resize(560, 560)

        layout = QVBoxLayout(self)
        text = _read_text_file(changelog_path)
        layout.addWidget(_markdown_browser(text), 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

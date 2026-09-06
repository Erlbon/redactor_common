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

AboutDialog also takes an optional `component_versions` dict, shown
under the app's own version line -- e.g. {"redactor_common": "..."}.
redactor_common is versioned independently of any consuming project's
own APP_VERSION (see core/version.py); this is the one place a person
can glance and notice a project is running an older vendored copy than
its siblings, without having to go compare files by hand.

`repo_url` (the app's own GitHub repo) and `component_repo_urls` (same
shape as `component_versions`, e.g. {"redactor_common": "https://..."})
turn the version line into clickable links back to the source -- for a
person looking at a build wondering "where did this come from".
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
        component_versions: dict[str, str] | None = None,
        repo_url: str | None = None,
        component_repo_urls: dict[str, str] | None = None,
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

        version_line = f"{release_label}, ver {app_version}" if release_label else f"ver {app_version}"
        header_html = f"<h2>{app_name}</h2><p>{version_line}</p>"
        if repo_url:
            header_html += f'<p style="font-size: 11px;"><a href="{repo_url}">{repo_url}</a></p>'
        # component_versions surfaces shared-package versions (e.g.
        # redactor_common) separately from the app's own APP_VERSION --
        # these are tracked and bumped independently (redactor_common
        # has its own consumers, on their own update cadence), so
        # showing them together here is the one place to notice at a
        # glance that a project is running an older vendored copy than
        # its siblings. component_repo_urls (same keys) turns each one
        # into a link back to that component's own repo.
        if component_versions:
            def _component_html(name: str, version: str) -> str:
                label = f"{name} {version}"
                url = (component_repo_urls or {}).get(name)
                return f'<a href="{url}">{label}</a>' if url else label

            parts = ", ".join(_component_html(name, version) for name, version in component_versions.items())
            header_html += f'<p style="color: gray; font-size: 11px;">{parts}</p>'
        header = QLabel(header_html)
        header.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        header.setTextFormat(Qt.TextFormat.RichText)
        header.setOpenExternalLinks(True)  # links open in the system browser, not in-app
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


class CreditsDialog(QDialog):
    """Third-party libraries, tools, and APIs a project depends on --
    same bundled-markdown-file pattern as ChangelogDialog, just reading
    CREDITS.md instead. Link-heavy content (the whole point is pointing
    people at the projects being credited), so open links externally
    same as the other two dialogs."""

    def __init__(self, credits_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Credits")
        self.resize(560, 560)

        layout = QVBoxLayout(self)
        text = _read_text_file(credits_path)
        layout.addWidget(_markdown_browser(text), 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

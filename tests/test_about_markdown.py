"""gui/about_dialog: the Changelog/About/Credits Markdown viewer shows raw angle brackets as text and loses nothing."""

import os
import re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from redactor_common.gui.about_dialog import _markdown_browser  # noqa: E402

_app = QApplication.instance() or QApplication([])


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9]{3,}", text)


def test_a_bare_angle_bracket_phrase_does_not_swallow_the_rest():
    """mp3redactor's 2026-09-04#08 entry said: Shows "<multiple values>" when ...; Qt read that as an HTML tag and
    garbled the rest of the entry and every entry below it."""
    md = '## One\n\n- Shows "<multiple values>" when the selection disagrees.\n- Second item, still here.\n\n## Two\n\n- Third item.\n'
    shown = _markdown_browser(md).toPlainText()
    assert "<multiple values>" in shown
    assert "Second item, still here." in shown and "Third item." in shown


def test_everything_in_a_changelog_style_text_is_shown():
    md = (
        "# Changelog\n\n## 2026-01-01#01 -- A title\n\n"
        "- Item with `code`, **bold** and a <tag> and a <b>bold-ish</b>, then <br> more text after.\n"
        "  - Nested <not closed item and the end of the line.\n"
        "  - Next nested item.\n\n## 2026-01-01#02\n\n- Later entry text.\n"
    )
    shown = _markdown_browser(md).toPlainText()
    for word in ("Item", "code", "bold", "tag", "more", "text", "after", "Nested", "closed", "Next", "Later", "entry"):
        assert word in shown, word


def test_markdown_links_and_formatting_still_render():
    browser = _markdown_browser("A [link](https://example.com) and **bold**.")
    assert "link" in browser.toPlainText()
    assert "https://example.com" in browser.toHtml()

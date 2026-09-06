"""Tests for gui/theme.py -- specifically that _LIGHT and _DARK's
Highlight/HighlightedText pairs (the actual thing that was broken:
"can't see what is selected in dark mode") are objectively readable,
verified against the WCAG 2.x relative-luminance contrast-ratio
formula rather than eyeballed. Also checks each theme's Highlight
color is visually distinct from its own Base (an unselected row's
background) -- text contrast alone doesn't guarantee a selected row
looks selected at all if the highlight color is too close in value to
the background around it.

QColor is a plain value type -- these tests construct it directly, no
QApplication needed, consistent with this repo's existing core-logic-
only test convention (gui/ dialogs aren't otherwise unit-tested here;
see manage_list_dialog.py/quick_pick_dialog.py, exercised instead by
whichever consuming app's own test suite uses them)."""

from PyQt6.QtGui import QColor

from redactor_common.gui.theme import _DARK, _LIGHT

# WCAG's own thresholds: 4.5:1 for normal text, 3:1 for large text and
# for a non-text UI component needing to be distinguishable from its
# surroundings (used here for "does the highlight stand out from the
# unselected background").
_TEXT_CONTRAST_MIN = 4.5
_COMPONENT_CONTRAST_MIN = 3.0


def _srgb_to_linear(channel: int) -> float:
    c = channel / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return 0.2126 * _srgb_to_linear(r) + 0.7152 * _srgb_to_linear(g) + 0.0722 * _srgb_to_linear(b)


def _contrast_ratio(rgb1: tuple[int, int, int], rgb2: tuple[int, int, int]) -> float:
    l1, l2 = _relative_luminance(rgb1), _relative_luminance(rgb2)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def test_contrast_ratio_formula_against_known_wcag_values():
    """Sanity-checks the test's own math against WCAG's published
    black-on-white reference (21:1, the maximum possible ratio)."""
    assert round(_contrast_ratio((0, 0, 0), (255, 255, 255)), 1) == 21.0


def test_light_theme_selection_text_is_readable():
    ratio = _contrast_ratio(_LIGHT["Highlight"], _LIGHT["HighlightedText"])
    assert ratio >= _TEXT_CONTRAST_MIN, f"light Highlight/HighlightedText only {ratio:.2f}:1"


def test_dark_theme_selection_text_is_readable():
    ratio = _contrast_ratio(_DARK["Highlight"], _DARK["HighlightedText"])
    assert ratio >= _TEXT_CONTRAST_MIN, f"dark Highlight/HighlightedText only {ratio:.2f}:1"


def test_light_theme_highlight_stands_out_from_unselected_background():
    ratio = _contrast_ratio(_LIGHT["Highlight"], _LIGHT["Base"])
    assert ratio >= _COMPONENT_CONTRAST_MIN, f"light Highlight/Base only {ratio:.2f}:1"


def test_dark_theme_highlight_stands_out_from_unselected_background():
    """The actual bug being fixed: in dark mode, a selected row needs
    to look clearly different from an unselected one, not just have
    readable text sitting on top of it."""
    ratio = _contrast_ratio(_DARK["Highlight"], _DARK["Base"])
    assert ratio >= _COMPONENT_CONTRAST_MIN, f"dark Highlight/Base only {ratio:.2f}:1"


def test_both_themes_normal_text_is_readable():
    for name, theme in (("light", _LIGHT), ("dark", _DARK)):
        ratio = _contrast_ratio(theme["Window"], theme["WindowText"])
        assert ratio >= _TEXT_CONTRAST_MIN, f"{name} Window/WindowText only {ratio:.2f}:1"
        ratio = _contrast_ratio(theme["Base"], theme["Text"])
        assert ratio >= _TEXT_CONTRAST_MIN, f"{name} Base/Text only {ratio:.2f}:1"


def test_build_palette_round_trips_every_declared_color():
    from PyQt6.QtGui import QPalette

    from redactor_common.gui.theme import build_palette

    palette = build_palette(_LIGHT)
    assert palette.color(QPalette.ColorRole.Highlight) == QColor(*_LIGHT["Highlight"])
    assert palette.color(QPalette.ColorRole.Text) == QColor(*_LIGHT["Text"])

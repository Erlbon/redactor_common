"""Regression test for the 2026-09-07 colors.py/theme.py conflict fix:
TABLE_SELECTION_STYLESHEET must never hardcode a selected row's own
background-color/color again -- a widget-level Qt stylesheet always
wins over the QApplication palette gui/theme.py's apply_theme() sets,
so doing that silently overrides apply_theme()'s WCAG-verified,
light/dark-aware selection colors with one fixed, unverified pair on
every app that applies both. Only the current-cell focus outline
(genuinely additive -- apply_theme() doesn't provide one) belongs
here."""

from redactor_common.gui.colors import TABLE_SELECTION_STYLESHEET


def test_stylesheet_does_not_override_selected_item_colors():
    assert "item:selected" not in TABLE_SELECTION_STYLESHEET
    assert "background-color" not in TABLE_SELECTION_STYLESHEET


def test_stylesheet_still_provides_the_focus_outline():
    assert "item:focus" in TABLE_SELECTION_STYLESHEET
    assert "border" in TABLE_SELECTION_STYLESHEET

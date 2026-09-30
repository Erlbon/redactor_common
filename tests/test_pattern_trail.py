"""Tests for the Redact recipe editor's 'pattern trail': OptionSpec
suggestions/fallback/fallback_label/preview, effective_option_source(),
and the editable combo + 'In effect' caption in RecipeEditorDialog."""

import os

import pytest

from redactor_common.core.pipeline import (
    OptionSpec,
    Recipe,
    Step,
    StepResult,
    effective_option_source,
)


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class Renamer(Step):
    key, label = "rename", "Rename"

    def __init__(self, **spec_kw):
        super().__init__()
        self.options = (OptionSpec("pattern", "Pattern", "str", "", **spec_kw),)

    def run(self, ctx):
        return StepResult.nothing()


HISTORY = ["{author} - {title}", "{series}/{title}", "{title}", "{author} - {title}", "{a}/{b}/{c}"]


def _trail_spec(**over):
    kw = dict(
        suggestions=lambda: list(HISTORY),
        fallback=lambda: "{title}",
        fallback_label="the last Rename/Export pattern",
    )
    kw.update(over)
    return kw


def _editor(recipe=None, **spec_kw):
    from redactor_common.gui.redact_dialog import RecipeEditorDialog

    step = Renamer(**spec_kw)
    return RecipeEditorDialog([step], recipe or Recipe()), step


def _row(dlg):
    from PyQt6.QtWidgets import QFormLayout

    return dlg.options_form.itemAt(0, QFormLayout.ItemRole.FieldRole).widget()


def test_optionspec_backward_compat():
    o = OptionSpec("p", "P", "str", "x")
    assert o.suggestions is None and o.fallback is None
    assert o.fallback_label == "" and o.preview is None
    assert o.coerce("abc") == "abc"
    assert effective_option_source(o, "") == ("x", "default")


def test_effective_option_source_cases():
    spec = OptionSpec("p", "P", "str", "dflt", **_trail_spec())
    assert effective_option_source(spec, "{a}") == ("{a}", "set in this recipe")
    assert effective_option_source(spec, "") == ("{title}", "follows: the last Rename/Export pattern")
    assert effective_option_source(spec, None) == ("{title}", "follows: the last Rename/Export pattern")
    nolabel = OptionSpec("p", "P", "str", "", fallback=lambda: "{t}")
    assert effective_option_source(nolabel, "")[1].startswith("follows: ")
    boom = OptionSpec("p", "P", "str", "", fallback=lambda: 1 / 0, fallback_label="x")
    assert effective_option_source(boom, "") == ("", "follows: x")


def test_combo_suggestions_newest_first_deduped_paths_after(qapp):
    from PyQt6.QtWidgets import QComboBox

    dlg, _ = _editor(**_trail_spec())
    combo = dlg.options_host.findChild(QComboBox)
    assert combo.isEditable()
    items = [combo.itemText(i) for i in range(combo.count())]
    assert items == ["{author} - {title}", "{title}", "{series}/{title}", "{a}/{b}/{c}"]


def test_caption_stored_fallback_and_no_fallback(qapp):
    dlg, _ = _editor(Recipe(options={"rename": {"pattern": "{t}"}}), **_trail_spec())
    assert _row(dlg).caption.text() == "In effect: {t} — set in this recipe"

    dlg, _ = _editor(**_trail_spec())
    assert _row(dlg).caption.text() == (
        "In effect: {title} — follows: the last Rename/Export pattern"
    )

    dlg, _ = _editor(suggestions=lambda: ["{x}"])
    assert _row(dlg).caption.text() == "In effect:  — default"
    assert not _row(dlg).use_fallback.isEnabled()


def test_typing_updates_caption_and_use_fallback_clears(qapp):
    from PyQt6.QtWidgets import QComboBox

    dlg, _ = _editor(**_trail_spec())
    combo = dlg.options_host.findChild(QComboBox)
    row = _row(dlg)
    combo.setEditText("{year} {title}")
    assert row.caption.text() == "In effect: {year} {title} — set in this recipe"
    assert dlg.recipe().options["rename"]["pattern"] == "{year} {title}"
    row.use_fallback.click()
    assert combo.currentText() == ""
    assert "follows: the last Rename/Export pattern" in row.caption.text()
    assert dlg.recipe().options["rename"]["pattern"] == ""


def test_picking_a_suggestion_pins_it(qapp):
    from PyQt6.QtWidgets import QComboBox

    dlg, _ = _editor(**_trail_spec())
    combo = dlg.options_host.findChild(QComboBox)
    combo.setCurrentIndex(1)
    assert dlg.recipe().options["rename"]["pattern"] == "{title}"
    assert "set in this recipe" in _row(dlg).caption.text()


def test_old_recipe_roundtrip_shows_set_in_recipe(qapp):
    old = '{"order": ["rename"], "enabled": {"rename": true}, "options": {"rename": {"pattern": "{old}"}}}'
    recipe = Recipe.from_json(old)
    dlg, _ = _editor(recipe, **_trail_spec(fallback=lambda: "{new}"))
    assert _row(dlg).caption.text().endswith("set in this recipe")
    assert dlg.recipe().options["rename"]["pattern"] == "{old}"  # fallback change doesn't touch it
    assert Recipe.from_json(dlg.recipe().to_json()).options["rename"]["pattern"] == "{old}"


def test_preview_line_only_when_provided(qapp):
    from PyQt6.QtWidgets import QComboBox

    dlg, _ = _editor(**_trail_spec(preview=lambda p: p.replace("{title}", "Dune")))
    row = _row(dlg)
    assert not row.preview.isHidden()
    assert row.preview.text() == "Preview: Dune"
    dlg.options_host.findChild(QComboBox).setEditText("x {title}")
    assert row.preview.text() == "Preview: x Dune"

    dlg2, _ = _editor(**_trail_spec())
    assert _row(dlg2).preview.isHidden()


def test_plain_str_option_unchanged(qapp):
    from PyQt6.QtWidgets import QComboBox, QLineEdit

    dlg, _ = _editor(max_length=5)
    assert dlg.options_host.findChild(QComboBox) is None
    edit = dlg.options_host.findChild(QLineEdit)
    edit.setText("abc")
    assert dlg.recipe().options["rename"]["pattern"] == "abc"

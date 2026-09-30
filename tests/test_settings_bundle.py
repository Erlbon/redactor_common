"""Tests for core/settings_bundle.py and gui/settings_bundle_dialogs.py."""

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from redactor_common.core import settings_bundle as sb  # noqa: E402
from redactor_common.gui import settings_bundle_dialogs as dlg  # noqa: E402

_app = QApplication.instance() or QApplication([])


class FakeAdapter(sb.SettingsAdapter):
    app_slug = "fakeapp"
    app_version = "1.2.3"

    def __init__(self):
        self.data = {
            "columns": {"order": ["a", "b"], "hidden": []},
            "patterns": {"rename": "%title%", "history": ["x"]},
            "tools": {"ffmpeg_path": r"C:\tools\ffmpeg.exe"},
            "lookup": {"provider": "tmdb", "api_key": "SEKRET", "pinned": True},
        }
        self.writes = []
        self.redetected = 0

    def sections(self):
        return [
            sb.SectionSpec("columns", "Columns"),
            sb.SectionSpec("patterns", "Patterns"),
            sb.SectionSpec("tools", "Tool paths", portable=False),
            sb.SectionSpec("lookup", "Lookup"),
        ]

    def read_section(self, key):
        return dict(self.data[key])

    def write_section(self, key, values):
        self.writes.append((key, dict(values)))
        self.data[key].update(values)

    def redetect_tools(self):
        self.redetected += 1


def _export_text(adapter, include):
    return sb.dump_bundle(sb.build_bundle(adapter, include))


def test_round_trip_between_adapters():
    a = FakeAdapter()
    text = _export_text(a, {"columns", "patterns"})
    doc = json.loads(text)
    assert doc["format"] == "redactor-settings" and doc["version"] == 1
    assert doc["app"] == "fakeapp" and doc["app_version"] == "1.2.3" and doc["exported"]
    b = FakeAdapter()
    b.data["columns"] = {"order": [], "hidden": ["z"]}
    b.data["patterns"] = {"rename": "", "history": []}
    bundle = sb.parse_bundle(text, "fakeapp")
    res = sb.apply_bundle(b, bundle, {"columns", "patterns"})
    assert sorted(res.applied) == ["columns", "patterns"] and not res.failed
    assert b.data["columns"] == a.data["columns"]
    assert b.data["patterns"] == a.data["patterns"]


def test_wrong_app_and_garbage_rejected():
    text = _export_text(FakeAdapter(), {"columns"})
    with pytest.raises(sb.SettingsBundleError, match="fakeapp"):
        sb.parse_bundle(text, "otherapp")
    for junk in ["", "not json", "[1,2]", "null", '{"format": "x"}',
                 '{"format": "redactor-settings", "version": "1", "app": "fakeapp"}',
                 '{"format": "redactor-settings", "version": 1}']:
        with pytest.raises(sb.SettingsBundleError):
            sb.parse_bundle(junk, "fakeapp")


def test_newer_version_rejected():
    doc = json.loads(_export_text(FakeAdapter(), {"columns"}))
    doc["version"] = 2
    with pytest.raises(sb.SettingsBundleError, match="newer"):
        sb.parse_bundle(json.dumps(doc), "fakeapp")


def test_partial_junk_tolerated():
    doc = {"format": "redactor-settings", "version": 1, "app": "fakeapp", "sections": {
        "columns": {"label": 5, "items": {"order": ["q"]}},
        "patterns": "garbage", "tools": {"items": [1]}, "lookup": None}}
    bundle = sb.parse_bundle(json.dumps(doc), "fakeapp")
    assert list(bundle.sections) == ["columns"]
    assert sb.parse_bundle(json.dumps({**doc, "sections": 7}), "fakeapp").sections == {}


def test_unknown_sections_and_keys_ignored():
    a = FakeAdapter()
    doc = json.loads(_export_text(a, {"columns"}))
    doc["sections"]["columns"]["items"]["brand_new_key"] = 1
    doc["sections"]["columns"]["items"]["order"] = ["new"]
    doc["sections"]["from_future"] = {"label": "F", "items": {"k": 1}}
    bundle = sb.parse_bundle(json.dumps(doc), "fakeapp")
    changes = sb.diff_bundle(a, bundle)
    assert [(c.section, c.key) for c in changes] == [("columns", "order")]
    sb.apply_bundle(a, bundle, {"columns", "from_future"})
    assert a.writes == [("columns", {"order": ["new"]})]
    assert "brand_new_key" not in a.data["columns"]


@pytest.mark.parametrize("name", [
    "api_key", "ApiKey", "comicvine_api_key", "password", "db_passwd", "auth_token",
    "client_secret", "pin", "user_PIN", "bearer", "Bearer_value", "tmdbToken", "private_key"])
def test_looks_secret_true(name):
    assert sb.looks_secret(name)


@pytest.mark.parametrize("name", ["pinned", "mapping", "sort_key", "keyboard", "default_language", "spinner"])
def test_looks_secret_false(name):
    assert not sb.looks_secret(name)


def test_secrets_never_exported_or_applied():
    a = FakeAdapter()
    bundle = sb.build_bundle(a, {"lookup"})
    assert "api_key" not in bundle.sections["lookup"].items
    assert "SEKRET" not in sb.dump_bundle(bundle)
    assert bundle.sections["lookup"].items["pinned"] is True
    # A hostile/hand-edited file carrying secrets, even for keys the adapter offers.
    doc = json.loads(sb.dump_bundle(bundle))
    doc["sections"]["lookup"]["items"]["api_key"] = "EVIL"
    parsed = sb.parse_bundle(json.dumps(doc), "fakeapp")
    assert "api_key" not in parsed.sections["lookup"].items
    # Even a Bundle object built by hand is filtered at diff/apply time.
    parsed.sections["lookup"].items["api_key"] = "EVIL"
    parsed.sections["lookup"].items["provider"] = "tvdb"
    assert [c.key for c in sb.diff_bundle(a, parsed)] == ["provider"]
    sb.apply_bundle(a, parsed, {"lookup"})
    assert a.data["lookup"]["api_key"] == "SEKRET"
    assert a.data["lookup"]["provider"] == "tvdb"


def test_non_json_values_skipped():
    a = FakeAdapter()
    a.data["columns"]["weird"] = object()
    assert "weird" not in sb.build_bundle(a, {"columns"}).sections["columns"].items


def test_machine_specific_opt_in():
    a = FakeAdapter()
    assert sb.default_selection(a) == {"columns", "patterns", "lookup"}
    assert "tools" not in sb.build_bundle(a, sb.default_selection(a)).sections
    assert "tools" in sb.build_bundle(a, {"tools"}).sections


def test_diff_correctness():
    a = FakeAdapter()
    bundle = sb.parse_bundle(_export_text(a, {"columns", "patterns"}), "fakeapp")
    assert sb.diff_bundle(a, bundle) == []
    a.data["patterns"]["rename"] = "%series%"
    a.data["columns"]["hidden"] = ["x"]
    changes = sb.diff_bundle(a, bundle)
    assert sorted(changes, key=lambda c: c.section) == [
        sb.Change("columns", "hidden", ["x"], []),
        sb.Change("patterns", "rename", "%series%", "%title%"),
    ]


def test_apply_only_chosen_sections_and_failure_isolated():
    a = FakeAdapter()
    bundle = sb.parse_bundle(_export_text(a, {"columns", "patterns"}), "fakeapp")
    a.data["patterns"]["rename"] = "changed"
    a.data["columns"]["order"] = ["changed"]
    res = sb.apply_bundle(a, bundle, {"patterns"})
    assert res.applied == ["patterns"]
    assert a.data["columns"]["order"] == ["changed"]
    assert a.data["patterns"]["rename"] == "%title%"

    orig = a.write_section

    def boom(key, values):
        if key == "columns":
            raise OSError("disk")
        orig(key, values)
    a.write_section = boom
    a.data["patterns"]["rename"] = "again"
    res = sb.apply_bundle(a, bundle, {"columns", "patterns"})
    assert res.applied == ["patterns"] and "columns" in res.failed


def test_write_text_atomic(tmp_path):
    p = tmp_path / "s.json"
    sb.write_text_atomic(p, "one")
    sb.write_text_atomic(p, "two")
    assert p.read_text() == "two" and [x.name for x in tmp_path.iterdir()] == ["s.json"]


# ---- dialogs (offscreen, file dialogs and exec patched) ----

@pytest.fixture
def quiet(monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(("info", a[2])))
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(("warn", a[2])))
    return shown


def test_export_dialog_defaults_and_writes(monkeypatch, tmp_path, quiet):
    a = FakeAdapter()
    seen = {}

    def fake_exec(self):
        seen["checked"] = self.selected_sections()
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(dlg.ExportSectionsDialog, "exec", fake_exec)
    target = tmp_path / "out.json"

    def fake_save(parent, title, default, flt):
        seen["default"] = default
        return str(target), ""
    monkeypatch.setattr(dlg.QFileDialog, "getSaveFileName", staticmethod(fake_save))
    assert dlg.export_settings(None, a) == target
    assert seen["default"] == "fakeapp-settings.json"
    assert seen["checked"] == {"columns", "patterns", "lookup"}  # machine-specific unticked
    doc = json.loads(target.read_text(encoding="utf-8"))
    assert set(doc["sections"]) == {"columns", "patterns", "lookup"}
    assert quiet[-1][0] == "info"


def test_export_cancelled(monkeypatch, quiet):
    monkeypatch.setattr(dlg.ExportSectionsDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    assert dlg.export_settings(None, FakeAdapter()) is None


def _patch_open(monkeypatch, path):
    monkeypatch.setattr(dlg.QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(path), "")))


def test_import_wrong_app_rejected(monkeypatch, tmp_path, quiet):
    f = tmp_path / "x.json"
    f.write_text(_export_text(FakeAdapter(), {"columns"}).replace("fakeapp", "otherapp"), encoding="utf-8")
    _patch_open(monkeypatch, f)
    a = FakeAdapter()
    assert dlg.import_settings(None, a) is None
    assert quiet[-1][0] == "warn" and "otherapp" in quiet[-1][1] and not a.writes


def test_import_preview_then_apply(monkeypatch, tmp_path, quiet):
    src = FakeAdapter()
    src.data["patterns"]["rename"] = "%from_file%"
    src.data["tools"]["ffmpeg_path"] = "/other/ffmpeg"
    f = tmp_path / "x.json"
    f.write_text(_export_text(src, {"patterns", "tools"}), encoding="utf-8")
    _patch_open(monkeypatch, f)
    a = FakeAdapter()
    captured = {}

    def fake_exec(self):
        captured["sections"] = self.selected_sections()
        captured["tops"] = self.tree.topLevelItemCount()
        assert not a.writes  # nothing applied before confirm
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(dlg.ImportPreviewDialog, "exec", fake_exec)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    applied = []
    res = dlg.import_settings(None, a, on_applied=applied.append)
    assert captured["tops"] == 2
    assert captured["sections"] == {"patterns"}  # machine-specific section unticked
    assert res.applied == ["patterns"] and applied == [res]
    assert a.data["patterns"]["rename"] == "%from_file%"
    assert a.data["tools"]["ffmpeg_path"] == r"C:\tools\ffmpeg.exe"
    assert a.redetected == 1


def test_import_no_changes_and_cancel(monkeypatch, tmp_path, quiet):
    a = FakeAdapter()
    f = tmp_path / "x.json"
    f.write_text(_export_text(a, {"columns"}), encoding="utf-8")
    _patch_open(monkeypatch, f)
    assert dlg.import_settings(None, a) is None
    assert "already match" in quiet[-1][1]
    a.data["columns"]["order"] = ["z"]
    monkeypatch.setattr(dlg.ImportPreviewDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    assert dlg.import_settings(None, a) is None and not a.writes


def test_menu_actions():
    acts = dlg.settings_menu_actions(lambda: None, lambda: None)
    assert [x.key for x in acts] == ["export_settings", "import_settings"]

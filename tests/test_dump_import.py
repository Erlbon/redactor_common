"""Tests for core/dump_import.py and gui/dump_import_runner.py."""

import bz2
import gzip
import os
import sqlite3
import sys
import threading
import time
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from redactor_common.core.dump_import import (  # noqa: E402
    INFO_TABLE,
    DumpImportError,
    ImportCancelled,
    SqliteBuilder,
    iter_xml_records,
    open_dump,
    read_import_info,
    split_list,
)

# Module level: an unreferenced QApplication is garbage-collected at once.
_app = QApplication.instance() or QApplication(sys.argv)

XML = b"""<?xml version="1.0"?>
<Db xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Books>
""" + b"".join(
    f'<Book Id="{i}"><Series>S{i}</Series><Pages><Page Image="0"/></Pages></Book>\n'.encode() for i in range(1200)
) + b"""  </Books>
  <Lists><Item Name="x"/></Lists>
</Db>
"""


def _write_variants(tmp_path):
    plain = tmp_path / "db.xml"
    plain.write_bytes(XML)
    (tmp_path / "db.xml.gz").write_bytes(gzip.compress(XML))
    (tmp_path / "db.xml.bz2").write_bytes(bz2.compress(XML))
    with zipfile.ZipFile(tmp_path / "db.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("ComicDb.xml", XML)
    return [plain, tmp_path / "db.xml.gz", tmp_path / "db.xml.bz2", tmp_path / "db.zip"]


def test_every_container_streams_the_same_records(tmp_path):
    for path in _write_variants(tmp_path):
        seen = []
        with open_dump(str(path)) as dump:
            for book in iter_xml_records(dump, "Book"):
                seen.append((book.get("Id"), book.findtext("Series"), len(book.find("Pages"))))
        assert len(seen) == 1200, path
        assert seen[7] == ("7", "S7", 1)


def test_progress_rises_to_one(tmp_path):
    _write_variants(tmp_path)
    fractions = []
    with open_dump(str(tmp_path / "db.xml")) as dump:
        for _ in iter_xml_records(dump, "Book", progress=fractions.append, check_every=100):
            pass
    assert fractions == sorted(fractions) and fractions[-1] == 1.0 and len(fractions) > 5


def test_cancel_stops_the_reader(tmp_path):
    _write_variants(tmp_path)
    seen = 0
    with pytest.raises(ImportCancelled):
        with open_dump(str(tmp_path / "db.xml")) as dump:
            for _ in iter_xml_records(dump, "Book", cancelled=lambda: seen >= 300, check_every=100):
                seen += 1
    assert seen == 300


def test_bad_inputs_are_reported(tmp_path):
    with pytest.raises(DumpImportError, match="not found"):
        with open_dump(str(tmp_path / "missing.xml")):
            pass
    with zipfile.ZipFile(tmp_path / "two.zip", "w") as z:
        z.writestr("a.xml", "<a/>")
        z.writestr("b.xml", "<b/>")
    with pytest.raises(DumpImportError, match="found 2"):
        with open_dump(str(tmp_path / "two.zip")):
            pass
    with open_dump(str(tmp_path / "two.zip"), member_suffix="b.xml") as dump:
        assert dump.stream.read() == b"<b/>"
    (tmp_path / "cut.xml").write_bytes(XML[:5000])
    with pytest.raises(DumpImportError, match="damaged"):
        with open_dump(str(tmp_path / "cut.xml")) as dump:
            list(iter_xml_records(dump, "Book"))


def test_split_list():
    assert split_list(" a, b,, c ,a") == ["a", "b", "c"]
    assert split_list(None) == []
    assert split_list("x; y", ";") == ["x", "y"]


TABLES = {"series": ["id integer primary key", "name text"], "issue": ["id integer primary key", "series_id integer"]}


def test_builder_writes_indexes_info_and_renames(tmp_path):
    dest = tmp_path / "out" / "lib.db"
    with SqliteBuilder(str(dest), TABLES, ["create index issue_series on issue(series_id)"], batch=3) as out:
        for i in range(10):
            out.add("series", (i, f"S{i}"))
            out.add("issue", (i, i))
        assert not dest.exists() and (tmp_path / "out" / "lib.db.partial").exists()
        counts = out.finish({"source": "test"})
    assert counts == {"series": 10, "issue": 10}
    assert dest.exists() and not (tmp_path / "out" / "lib.db.partial").exists()
    con = sqlite3.connect(dest)
    assert con.execute("select name from series where id = 4").fetchone() == ("S4",)
    info = read_import_info(con)
    assert info["source"] == "test" and info["rows.issue"] == "10" and info["built"]
    assert con.execute("select count(*) from sqlite_stat1").fetchone()[0] > 0  # analyzed
    assert "issue_series" in {r[0] for r in con.execute("select name from sqlite_master where type='index'")}
    assert read_import_info(sqlite3.connect(":memory:")) == {}
    assert INFO_TABLE == "redactor_import_info"


def test_builder_failure_keeps_the_old_file(tmp_path):
    dest = tmp_path / "lib.db"
    dest.write_bytes(b"previous build")
    with pytest.raises(ImportCancelled):
        with SqliteBuilder(str(dest), TABLES) as out:
            out.add("series", (1, "S"))
            raise ImportCancelled()
    assert dest.read_bytes() == b"previous build"
    assert not (tmp_path / "lib.db.partial").exists()


def test_builder_rejects_rows_of_the_wrong_width(tmp_path):
    with pytest.raises(DumpImportError, match="expected 2 values"):
        with SqliteBuilder(str(tmp_path / "x.db"), TABLES) as out:
            out.add("series", (1, "S", "extra"))


# ---------------------------------------------------------------------------
# The GUI runner
# ---------------------------------------------------------------------------

def test_runner_returns_the_result():
    from redactor_common.gui.dump_import_runner import run_dump_import

    def work(progress, cancelled):
        progress(0.5)
        return {"rows": 3}

    assert run_dump_import(None, "T", "Working", work) == {"rows": 3}


def test_runner_cancel_waits_for_the_worker_and_returns_none():
    from redactor_common.gui import dump_import_runner as runner

    finished = threading.Event()

    def work(progress, cancelled):
        started.set()
        deadline = time.monotonic() + 10  # never hang the suite
        while not cancelled() and time.monotonic() < deadline:
            time.sleep(0.01)
        finished.set()
        raise ImportCancelled()

    started = threading.Event()
    # Press Cancel on the dialog once the worker is running.
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QProgressDialog

    def press_cancel():
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, QProgressDialog) and widget.isVisible():
                widget.canceled.emit()  # what the Cancel button does
                return
        QTimer.singleShot(20, press_cancel)

    QTimer.singleShot(50, press_cancel)
    start = time.monotonic()
    assert runner.run_dump_import(None, "T", "Working", work) is None
    assert finished.is_set() and time.monotonic() - start < 5


def test_runner_reraises_errors():
    from redactor_common.gui.dump_import_runner import run_dump_import

    def work(progress, cancelled):
        raise DumpImportError("bad file")

    with pytest.raises(DumpImportError, match="bad file"):
        run_dump_import(None, "T", "Working", work)


def test_settings_dialog_build_button_fills_the_path(tmp_path):
    from redactor_common.gui.local_db_settings_dialog import LocalDatabaseSettingsDialog

    dialog = LocalDatabaseSettingsDialog(
        title="T", instructions_html="x", path="", check=lambda p: "ok", save=lambda p: None,
        build=lambda parent: str(tmp_path / "built.db"), build_label="Build from X…",
    )
    assert dialog.build_button.text() == "Build from X…"
    dialog.build_button.click()
    assert dialog.path_edit.text() == str(tmp_path / "built.db")
    plain = LocalDatabaseSettingsDialog(title="T", instructions_html="x", path="", check=lambda p: "", save=lambda p: None)
    assert plain.build_button is None

"""Tests for core/dump_mysql.py: reading a mysqldump .sql file as a row stream."""

import io
import zipfile

import pytest

from redactor_common.core.dump_import import DumpImportError, ImportCancelled, open_dump
from redactor_common.core.dump_mysql import MysqlReadStats, iter_mysql_dump, iter_mysql_table, unescape_mysql

HEADER = (
    "-- MySQL dump 10.13  Distrib 5.5.62\n"
    "/*!40101 SET NAMES utf8 */;\n"
)

PUBS_CREATE = (
    "DROP TABLE IF EXISTS `pubs`;\n"
    "CREATE TABLE `pubs` (\n"
    "  `pub_id` int(11) NOT NULL AUTO_INCREMENT,\n"
    "  `pub_title` mediumtext,\n"
    "  `pub_year` date DEFAULT NULL,\n"
    "  `pub_isbn` varchar(100) DEFAULT NULL,\n"
    "  PRIMARY KEY (`pub_id`),\n"
    "  KEY `pub_title` (`pub_title`(50)),\n"
    "  FULLTEXT KEY `full_text` (`pub_title`)\n"
    ") ENGINE=MyISAM AUTO_INCREMENT=5 DEFAULT CHARSET=latin1;\n"
)


def dump(*parts: str) -> io.BytesIO:
    return io.BytesIO((HEADER + "".join(parts)).encode("utf-8"))


def test_reads_wanted_columns_by_name_in_any_order():
    data = dump(
        PUBS_CREATE,
        "INSERT INTO `pubs` VALUES (1,'Dune','1990-09-00','0441172717'),(2,'Emma',NULL,'');\n",
    )
    rows = list(iter_mysql_table(data, "pubs", ["pub_isbn", "pub_title", "pub_id"]))
    assert rows == [
        {"pub_isbn": "0441172717", "pub_title": "Dune", "pub_id": "1"},
        {"pub_isbn": "", "pub_title": "Emma", "pub_id": "2"},
    ]


def test_null_is_none_and_empty_string_stays_empty():
    data = dump(PUBS_CREATE, "INSERT INTO `pubs` VALUES (1,'','1999-00-00',NULL);\n")
    (row,) = iter_mysql_table(data, "pubs", ["pub_title", "pub_isbn"])
    assert row == {"pub_title": "", "pub_isbn": None}


def test_escapes_quotes_commas_parentheses_and_newlines():
    title = r"'It\'s a \"test\", (really)\nsecond line\\ end'"
    data = dump(PUBS_CREATE, f"INSERT INTO `pubs` VALUES (1,{title},NULL,NULL),(2,'a''b',NULL,NULL);\n")
    rows = list(iter_mysql_table(data, "pubs", ["pub_title"]))
    assert rows[0]["pub_title"] == 'It\'s a "test", (really)\nsecond line\\ end'
    assert rows[1]["pub_title"] == "a'b"


def test_non_ascii_text_is_utf8():
    data = dump(PUBS_CREATE, "INSERT INTO `pubs` VALUES (1,'La Divine Comédie','1841-00-00',NULL);\n")
    (row,) = iter_mysql_table(data, "pubs", ["pub_title"])
    assert row["pub_title"] == "La Divine Comédie"


def test_invalid_utf8_is_replaced_and_counted():
    raw = (HEADER + PUBS_CREATE).encode() + b"INSERT INTO `pubs` VALUES (1,'bad \xff byte',NULL,NULL);\n"
    stats = MysqlReadStats()
    (row,) = iter_mysql_table(io.BytesIO(raw), "pubs", ["pub_title"], stats=stats)
    assert "�" in row["pub_title"]
    assert stats.replaced == 1


def test_other_tables_and_lock_lines_are_skipped():
    data = dump(
        "CREATE TABLE `authors` (\n  `author_id` int(11),\n  `author_canonical` mediumtext\n) ENGINE=MyISAM;\n",
        "INSERT INTO `authors` VALUES (1,'Frank Herbert');\n",
        "LOCK TABLES `pubs` WRITE;\n",
        PUBS_CREATE,
        "INSERT INTO `pubs` VALUES (1,'Dune',NULL,NULL);\n",
        "UNLOCK TABLES;\n",
    )
    stats = MysqlReadStats()
    rows = list(iter_mysql_dump(data, {"pubs": ["pub_title"]}, stats=stats))
    assert rows == [("pubs", {"pub_title": "Dune"})]
    assert stats.rows == {"pubs": 1}


def test_several_wanted_tables_come_back_tagged():
    data = dump(
        "CREATE TABLE `authors` (\n  `author_id` int(11),\n  `author_canonical` mediumtext\n) ENGINE=MyISAM;\n",
        "INSERT INTO `authors` VALUES (1,'Frank Herbert'),(2,'Jane Austen');\n",
        PUBS_CREATE,
        "INSERT INTO `pubs` VALUES (1,'Dune',NULL,NULL);\n",
    )
    rows = list(iter_mysql_dump(data, {"authors": ["author_canonical"], "pubs": ["pub_id"]}))
    assert [name for name, _ in rows] == ["authors", "authors", "pubs"]


def test_complete_insert_uses_its_own_column_list():
    data = dump(PUBS_CREATE, "INSERT INTO `pubs` (`pub_id`,`pub_title`,`pub_year`,`pub_isbn`) VALUES (3,'Emma',NULL,NULL);\n")
    (row,) = iter_mysql_table(data, "pubs", ["pub_title"])
    assert row == {"pub_title": "Emma"}


def test_table_with_extra_columns_still_works():
    create = PUBS_CREATE.replace("  `pub_isbn` varchar(100) DEFAULT NULL,\n", "  `pub_isbn` varchar(100) DEFAULT NULL,\n  `pub_new` text,\n")
    data = dump(create, "INSERT INTO `pubs` VALUES (1,'Dune',NULL,'x','extra');\n")
    (row,) = iter_mysql_table(data, "pubs", ["pub_isbn"])
    assert row == {"pub_isbn": "x"}


def test_missing_wanted_column_fails_loudly():
    data = dump(PUBS_CREATE, "INSERT INTO `pubs` VALUES (1,'Dune',NULL,NULL);\n")
    with pytest.raises(DumpImportError, match="no column 'pub_pages'"):
        list(iter_mysql_table(data, "pubs", ["pub_pages"]))


def test_missing_wanted_table_fails_loudly():
    data = dump(PUBS_CREATE, "INSERT INTO `pubs` VALUES (1,'Dune',NULL,NULL);\n")
    with pytest.raises(DumpImportError, match="no table titles"):
        list(iter_mysql_dump(data, {"pubs": ["pub_id"], "titles": ["title_id"]}))


def test_rows_before_create_fail_loudly():
    data = dump("INSERT INTO `pubs` VALUES (1,'Dune',NULL,NULL);\n")
    with pytest.raises(DumpImportError, match="before its CREATE TABLE"):
        list(iter_mysql_table(data, "pubs", ["pub_id"]))


def test_wrong_row_width_is_a_bad_line_and_a_changed_format_fails():
    good = "INSERT INTO `pubs` VALUES (1,'Dune',NULL,NULL);\n"
    short = "INSERT INTO `pubs` VALUES (1,'Dune',NULL);\n"
    skipped = []
    stats = MysqlReadStats()
    data = dump(PUBS_CREATE, good, short, good)
    rows = list(iter_mysql_table(data, "pubs", ["pub_id"], stats=stats, on_bad_line=lambda n, why, text: skipped.append(why)))
    assert len(rows) == 2 and stats.bad == 1 and "3 values" in skipped[0]
    with pytest.raises(DumpImportError, match="right columns"):
        list(iter_mysql_table(dump(PUBS_CREATE, *([short] * 60)), "pubs", ["pub_id"]))


def test_garbage_between_rows_is_a_bad_line():
    broken = "INSERT INTO `pubs` VALUES (1,'a',NULL,NULL)xx(2,'b',NULL,NULL);\n"
    good = "INSERT INTO `pubs` VALUES (3,'c',NULL,NULL);\n"
    stats = MysqlReadStats()
    rows = list(iter_mysql_table(dump(PUBS_CREATE, *([good] * 30), broken, *([good] * 30)), "pubs", ["pub_id"], stats=stats))
    assert len(rows) == 60 and stats.bad == 1
    with pytest.raises(DumpImportError, match="right columns"):  # nothing readable at all: a changed format
        list(iter_mysql_table(dump(PUBS_CREATE, broken), "pubs", ["pub_id"]))


def test_numbers_and_signs_are_text():
    create = "CREATE TABLE `t` (\n  `a` int,\n  `b` float,\n  `c` int\n) ENGINE=MyISAM;\n"
    data = dump(create, "INSERT INTO `t` VALUES (1,-2.5e3,NULL);\n")
    (row,) = iter_mysql_table(data, "t", ["a", "b", "c"])
    assert row == {"a": "1", "b": "-2.5e3", "c": None}


def test_cancel_raises():
    rows = "".join(f"INSERT INTO `pubs` VALUES ({i},'t',NULL,NULL);\n" for i in range(2000))
    with pytest.raises(ImportCancelled):
        list(iter_mysql_table(dump(PUBS_CREATE, rows), "pubs", ["pub_id"], cancelled=lambda: True))


def test_reads_a_zip_through_open_dump(tmp_path):
    path = tmp_path / "backup.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("cygdrive/c/ISFDB/Backups/backup-MySQL-55", HEADER + PUBS_CREATE + "INSERT INTO `pubs` VALUES (1,'Dune',NULL,NULL);\n")
    with open_dump(str(path)) as opened:
        rows = list(iter_mysql_table(opened, "pubs", ["pub_title"]))
    assert rows == [{"pub_title": "Dune"}]


def test_unescape_mysql():
    assert unescape_mysql(rb"a\nb\\c\'d''e\0") == b"a\nb\\c'd'e\x00"
    assert unescape_mysql(rb"50\% off") == b"50\\% off"


def test_many_rows_in_one_long_line():
    values = ",".join(f"({i},'title {i}',NULL,'{i:010d}')" for i in range(20000))
    data = dump(PUBS_CREATE, f"INSERT INTO `pubs` VALUES {values};\n")
    stats = MysqlReadStats()
    rows = list(iter_mysql_table(data, "pubs", ["pub_id", "pub_isbn"], stats=stats))
    assert len(rows) == 20000 and rows[-1] == {"pub_id": "19999", "pub_isbn": "0000019999"}
    assert stats.rows["pubs"] == 20000

"""The overwrite review reads an enum field by its value (2026-11-27): str() of a str-mixin enum gives "Type.MEMBER",
so videoredactor's unset Content Type counted as an existing value and every scene lookup opened the review."""

import os
import types
from enum import Enum, IntEnum

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from redactor_common.gui import overwrite_review_dialog as ord_  # noqa: E402


class ContentType(str, Enum):
    UNSET = ""
    MOVIE = "movie"


class Level(IntEnum):
    TWO = 2


def test_enum_values_as_text():
    assert ord_._text(ContentType.UNSET) == ""
    assert ord_._text(ContentType.MOVIE) == "movie"
    assert ord_._text(Level.TWO) == "2"
    assert ord_._text([ContentType.MOVIE, "x"]) == "movie; x"


def test_an_unset_enum_field_is_blank_in_the_review():
    item = types.SimpleNamespace(path="a.mkv", metadata=types.SimpleNamespace(content_type=ContentType.UNSET))
    rows = ord_.build_overwrite_review_rows([item], {0: {"content_type": "movie"}}, str)
    (row,) = rows
    assert row.old_value == "" and row.default_checked

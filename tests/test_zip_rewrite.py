"""Tests for core/zip_rewrite.py (promoted from cbzredactor): the one archive-rewrite helper behind cbzredactor's Save,
Remove Pages, Clean Up and Resize."""

import os
import struct
import zipfile

import pytest

from redactor_common.core.zip_rewrite import (
    Action,
    ZipRewriteError,
    NewEntry,
    RewriteCancelled,
    RewriteError,
    RewritePlan,
    rewrite_archive,
    safe_extra,
)


def _make(path, entries=(("a.txt", b"alpha"), ("b.txt", b"bravo"), ("c.txt", b"charlie")), comment=b""):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.comment = comment
        for name, data in entries:
            zf.writestr(name, data)
    return str(path)


def _read_all(path):
    with zipfile.ZipFile(path) as zf:
        return [(i.filename, zf.read(i)) for i in zf.infolist()]


def _leftovers(tmp_path):
    return sorted(p.name for p in tmp_path.iterdir() if ".tmp" in p.name or ".redact-orig" in p.name)


def _rewrite(src, plan=None, dst=None, **kwargs):
    return rewrite_archive(src, dst or src, plan or RewritePlan(), **kwargs)


# --- what a plan can do --------------------------------------------------------


def test_default_plan_copies_everything_in_order(tmp_path):
    src = _make(tmp_path / "x.zip")
    result = _rewrite(src)
    assert [n for n, _ in _read_all(src)] == ["a.txt", "b.txt", "c.txt"]
    assert result.kept == 3 and not result.dropped and not result.renamed and not result.replaced
    assert result.entries_written == 3
    assert result.bytes_in == result.bytes_out == len(b"alphabravocharlie")
    assert _leftovers(tmp_path) == []


def test_drop_rename_replace_and_add(tmp_path):
    src = _make(tmp_path / "x.zip")

    def decide(entry, read):
        if entry.name == "a.txt":
            return Action(drop=True)
        if entry.name == "b.txt":
            return Action(rename="sub/b2.txt")
        if entry.name == "c.txt":
            return Action(data=b"CHANGED")
        return None

    plan = RewritePlan(decide=decide, before=[NewEntry("first.txt", b"1")], after=[NewEntry("last.txt", b"9")])
    result = _rewrite(src, plan)
    assert _read_all(src) == [
        ("first.txt", b"1"), ("sub/b2.txt", b"bravo"), ("c.txt", b"CHANGED"), ("last.txt", b"9"),
    ]
    assert result.dropped == ["a.txt"]
    assert result.renamed == {"b.txt": "sub/b2.txt"}
    assert result.replaced == ["c.txt"]
    assert result.added == ["first.txt", "last.txt"]
    assert result.kept == 0


def test_replacing_with_identical_bytes_counts_as_kept(tmp_path):
    src = _make(tmp_path / "x.zip")
    result = _rewrite(src, RewritePlan(decide=lambda e, read: Action(data=read())))
    assert result.replaced == [] and result.kept == 3


def test_order_callback_reorders_the_output(tmp_path):
    src = _make(tmp_path / "x.zip")
    _rewrite(src, RewritePlan(order=lambda entries: list(reversed(entries))))
    assert [n for n, _ in _read_all(src)] == ["c.txt", "b.txt", "a.txt"]


def test_begin_sees_every_entry_first(tmp_path):
    src = _make(tmp_path / "x.zip")
    seen = []
    _rewrite(src, RewritePlan(begin=lambda entries: seen.extend(e.name for e in entries)))
    assert seen == ["a.txt", "b.txt", "c.txt"]


def test_deferred_decisions_are_resolved_in_order_in_a_sliding_window(tmp_path):
    src = _make(tmp_path / "x.zip", [(f"{i}.txt", b"x") for i in range(5)])
    log = []

    def decide(entry, read):
        log.append(("decide", entry.name))
        return lambda: (log.append(("finish", entry.name)), Action(data=entry.name.encode()))[1]

    _rewrite(src, RewritePlan(decide=decide, window=2))
    assert [d for n, d in _read_all(src)] == [f"{i}.txt".encode() for i in range(5)]
    # a window of two is decided up front; each time one is finished the next is decided,
    # so the window never drains (the workers stay busy) and never exceeds two
    assert log[:6] == [
        ("decide", "0.txt"), ("decide", "1.txt"), ("finish", "0.txt"),
        ("decide", "2.txt"), ("finish", "1.txt"), ("decide", "3.txt"),
    ]


def test_progress_reports_every_source_entry(tmp_path):
    src = _make(tmp_path / "x.zip")
    calls = []
    _rewrite(src, RewritePlan(decide=lambda e, r: Action(drop=e.name == "b.txt")), progress=lambda d, t: calls.append((d, t)))
    assert calls == [(1, 3), (2, 3), (3, 3)]


def test_writes_to_another_path_and_leaves_the_source(tmp_path):
    src = _make(tmp_path / "x.zip")
    dst = str(tmp_path / "out.zip")
    _rewrite(src, RewritePlan(decide=lambda e, r: Action(drop=e.name == "a.txt")), dst=dst)
    assert [n for n, _ in _read_all(src)] == ["a.txt", "b.txt", "c.txt"]
    assert [n for n, _ in _read_all(dst)] == ["b.txt", "c.txt"]


def test_dispose_original_runs_after_the_new_file_is_in_place(tmp_path):
    # T7: the original keeps a name on disk (x.redact-orig.zip) until the
    # rewritten file has taken its place; only then does it go to the bin.
    src = _make(tmp_path / "x.zip")
    order = []

    def dispose(path):
        order.append((os.path.basename(path), os.path.exists(path), os.path.exists(src),
                      os.path.exists(src + ".tmp_write")))
        os.remove(path)

    _rewrite(src, dispose_original=dispose, plan=RewritePlan(decide=lambda e, r: Action(drop=e.name == "a.txt")))
    assert order == [("x.redact-orig.zip", True, True, False)]
    assert [n for n, _ in _read_all(src)] == ["b.txt", "c.txt"]
    assert _leftovers(tmp_path) == []


def test_temp_suffix_names_the_temp_file(tmp_path):
    src = _make(tmp_path / "x.zip")
    seen = []

    def decide(entry, read):
        seen.append(sorted(p.name for p in tmp_path.iterdir()))
        return None

    _rewrite(src, RewritePlan(decide=decide), temp_suffix=".tmp_custom")
    assert "x.zip.tmp_custom" in seen[0]


def test_two_entries_with_one_name_is_refused(tmp_path):
    src = _make(tmp_path / "x.zip")
    before = _read_all(src)
    with pytest.raises(ZipRewriteError, match="two entries"):
        _rewrite(src, RewritePlan(decide=lambda e, r: Action(rename="a.txt") if e.name == "b.txt" else None))
    with pytest.raises(ZipRewriteError, match="two entries"):
        _rewrite(src, RewritePlan(after=[NewEntry("a.txt", b"")]))
    assert _read_all(src) == before and _leftovers(tmp_path) == []


def test_duplicates_already_in_the_source_are_copied_faithfully(tmp_path):
    with pytest.warns(UserWarning):
        src = _make(tmp_path / "x.zip", [("a.txt", b"one"), ("a.txt", b"two")])
    _rewrite(src)
    assert _read_all(src) == [("a.txt", b"one"), ("a.txt", b"two")]


# --- comment and metadata --------------------------------------------------------


def test_archive_comment_is_kept_or_replaced(tmp_path):
    src = _make(tmp_path / "x.zip", comment=b"stamp")
    _rewrite(src)
    with zipfile.ZipFile(src) as zf:
        assert zf.comment == b"stamp"
    _rewrite(src, RewritePlan(comment=b"new"))
    with zipfile.ZipFile(src) as zf:
        assert zf.comment == b"new"
    _rewrite(src, RewritePlan(comment=b""))
    with zipfile.ZipFile(src) as zf:
        assert zf.comment == b""


def test_entry_metadata_is_preserved(tmp_path):
    src = str(tmp_path / "x.zip")
    with zipfile.ZipFile(src, "w") as zf:
        info = zipfile.ZipInfo("p.txt", date_time=(2001, 2, 3, 4, 5, 6))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        info.internal_attr = 1
        info.create_system = 3
        info.comment = b"entry note"
        info.extra = struct.pack("<HH4s", 0x5455, 4, b"\x01abc")  # extended timestamp: stays valid
        zf.writestr(info, b"payload")
        stored = zipfile.ZipInfo("s.bin", date_time=(1999, 12, 31, 23, 59, 58))
        stored.compress_type = zipfile.ZIP_STORED
        zf.writestr(stored, b"raw")
    _rewrite(src)
    with zipfile.ZipFile(src) as zf:
        p, s = zf.getinfo("p.txt"), zf.getinfo("s.bin")
        assert p.date_time == (2001, 2, 3, 4, 5, 6)
        assert p.compress_type == zipfile.ZIP_DEFLATED
        assert p.external_attr == 0o644 << 16
        assert p.internal_attr == 1
        assert p.create_system == 3
        assert p.comment == b"entry note"
        assert p.extra == struct.pack("<HH4s", 0x5455, 4, b"\x01abc")
        assert s.compress_type == zipfile.ZIP_STORED
        assert s.date_time == (1999, 12, 31, 23, 59, 58)


def test_safe_extra_drops_only_stale_fields():
    keep = struct.pack("<HH2s", 0x7875, 2, b"ab")
    zip64 = struct.pack("<HHQ", 0x0001, 8, 5)
    unicode_path = struct.pack("<HH5s", 0x7075, 5, b"\x01aaaa")
    assert safe_extra(keep + zip64 + unicode_path) == keep
    assert safe_extra(b"") == b""
    assert safe_extra(keep + b"\x05") == b""  # malformed: dropped whole, never half-copied
    assert safe_extra(struct.pack("<HH", 0x7875, 9) + b"ab") == b""


def test_replaced_entry_keeps_metadata_but_can_be_restamped(tmp_path):
    src = str(tmp_path / "x.zip")
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr(zipfile.ZipInfo("a.txt", date_time=(2001, 2, 3, 4, 5, 6)), b"old")
    _rewrite(src, RewritePlan(decide=lambda e, r: Action(data=b"new")))
    with zipfile.ZipFile(src) as zf:
        assert zf.getinfo("a.txt").date_time == (2001, 2, 3, 4, 5, 6)
    _rewrite(src, RewritePlan(decide=lambda e, r: Action(data=b"newer", date_time=(2020, 1, 1, 0, 0, 0))))
    with zipfile.ZipFile(src) as zf:
        assert zf.getinfo("a.txt").date_time == (2020, 1, 1, 0, 0, 0)


def test_new_entries_get_a_normal_file_mode_and_a_current_time(tmp_path):
    src = _make(tmp_path / "x.zip")
    _rewrite(src, RewritePlan(after=[NewEntry("n.txt", b"n")]))
    with zipfile.ZipFile(src) as zf:
        info = zf.getinfo("n.txt")
        assert info.external_attr == 0o600 << 16
        assert info.date_time[0] >= 2024


# --- verification -------------------------------------------------------------------


def test_verify_callback_and_crc_check(tmp_path):
    src = _make(tmp_path / "x.zip")
    before = _read_all(src)
    with pytest.raises(RewriteError, match="verification: nope"):
        _rewrite(src, RewritePlan(verify=lambda zf: "nope"))
    assert _read_all(src) == before and _leftovers(tmp_path) == []
    seen = []
    _rewrite(src, RewritePlan(verify=lambda zf: seen.append(zf.namelist()), verify_crc=True))
    assert seen == [["a.txt", "b.txt", "c.txt"]]


def test_a_failed_crc_check_keeps_the_source(tmp_path, monkeypatch):
    src = _make(tmp_path / "x.zip")
    before = _read_all(src)
    monkeypatch.setattr(zipfile.ZipFile, "testzip", lambda self: "b.txt")
    with pytest.raises(RewriteError, match="checksum"):
        _rewrite(src, RewritePlan(verify_crc=True))
    assert _read_all(src) == before and _leftovers(tmp_path) == []


# --- failures: always ZipRewriteError, always no temp --------------------------------------


def _patch_entry(path, name, flag_or=0, method=None):
    """Edits one entry's flag bits / compression method in both its local and
    central headers (how an encrypted or exotic-compression entry looks)."""
    raw = bytearray(open(path, "rb").read())
    encoded = name.encode()
    local = raw.index(b"PK\x03\x04")
    central = raw.index(b"PK\x01\x02")
    assert raw[local + 30:local + 30 + len(encoded)] == encoded
    for base, flag_at, method_at in ((local, 6, 8), (central, 8, 10)):
        flags = struct.unpack_from("<H", raw, base + flag_at)[0] | flag_or
        struct.pack_into("<H", raw, base + flag_at, flags)
        if method is not None:
            struct.pack_into("<H", raw, base + method_at, method)
    open(path, "wb").write(bytes(raw))


def test_source_that_is_not_a_zip(tmp_path):
    src = tmp_path / "x.zip"
    src.write_bytes(b"this is not a zip file at all")
    with pytest.raises(ZipRewriteError):
        _rewrite(str(src))
    assert _leftovers(tmp_path) == [] and src.read_bytes() == b"this is not a zip file at all"


def test_missing_source(tmp_path):
    with pytest.raises(ZipRewriteError):
        _rewrite(str(tmp_path / "gone.zip"))
    assert _leftovers(tmp_path) == []


def test_encrypted_entry(tmp_path):
    src = _make(tmp_path / "x.zip")
    _patch_entry(src, "a.txt", flag_or=0x1)
    before = open(src, "rb").read()
    with pytest.raises(ZipRewriteError, match="Could not copy"):
        _rewrite(src, error_prefix="Could not copy")
    assert _leftovers(tmp_path) == [] and open(src, "rb").read() == before


def test_unsupported_compression_method(tmp_path):
    src = _make(tmp_path / "x.zip")
    _patch_entry(src, "a.txt", method=99)
    with pytest.raises(ZipRewriteError):
        _rewrite(src)
    assert _leftovers(tmp_path) == []


def test_corrupt_compressed_stream(tmp_path):
    payload = os.urandom(0) + b"compressible " * 200
    src = _make(tmp_path / "x.zip", [("a.txt", payload)])
    raw = bytearray(open(src, "rb").read())
    start = raw.index(b"a.txt") + 5 + 6
    raw[start:start + 12] = b"\xff" * 12  # inside the deflate stream
    open(src, "wb").write(bytes(raw))
    with pytest.raises(ZipRewriteError):
        _rewrite(src)
    assert _leftovers(tmp_path) == []


@pytest.mark.parametrize("error", [ValueError("bad header"), KeyError("gone"), EOFError("cut off"), RuntimeError("x")])
def test_zip_level_errors_from_the_plan_become_cbz_errors(tmp_path, error):
    src = _make(tmp_path / "x.zip")

    def decide(entry, read):
        if entry.name == "b.txt":
            raise error
        return None

    with pytest.raises(RewriteError) as info:
        _rewrite(src, RewritePlan(decide=decide), error_prefix="Could not do it")
    assert str(info.value).startswith("Could not do it: ") and info.value.detail
    assert info.value.__cause__ is error
    assert _leftovers(tmp_path) == []


def test_lzma_error_becomes_a_cbz_error(tmp_path):
    import lzma

    src = _make(tmp_path / "x.zip")
    with pytest.raises(ZipRewriteError):
        _rewrite(src, RewritePlan(decide=lambda e, r: (_ for _ in ()).throw(lzma.LZMAError("corrupt"))))
    assert _leftovers(tmp_path) == []


def test_disk_full_while_writing(tmp_path, monkeypatch):
    src = _make(tmp_path / "x.zip")
    before = _read_all(src)

    def full(self, *args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(zipfile.ZipFile, "writestr", full)
    monkeypatch.setattr(zipfile._ZipWriteFile, "write", full)  # untouched entries are streamed
    with pytest.raises(ZipRewriteError):
        _rewrite(src)
    monkeypatch.undo()
    assert _read_all(src) == before and _leftovers(tmp_path) == []


def test_unexpected_exception_propagates_but_the_temp_is_removed(tmp_path):
    src = _make(tmp_path / "x.zip")

    def decide(entry, read):
        raise MemoryError("decoder blew up")

    with pytest.raises(MemoryError):
        _rewrite(src, RewritePlan(decide=decide))
    assert _leftovers(tmp_path) == []


def test_dispose_failure_replaces_nothing_and_removes_the_temp(tmp_path):
    src = _make(tmp_path / "x.zip")
    before = _read_all(src)

    def dispose(path):
        raise OSError("Recycle Bin unavailable")

    with pytest.raises(ZipRewriteError):
        _rewrite(src, dispose_original=dispose)
    assert _read_all(src) == before and _leftovers(tmp_path) == []


def test_a_failed_replace_puts_the_original_back_and_never_disposes_it(tmp_path, monkeypatch):
    # T7: the original used to go to the bin BEFORE the replace was tried.
    import redactor_common.core.zip_rewrite as zip_rewrite

    monkeypatch.setattr(zip_rewrite, "_LOCK_RETRY", {"delay": 0})
    src = _make(tmp_path / "x.zip")
    before = _read_all(src)
    disposed = []
    monkeypatch.setattr(os, "replace", lambda a, b: (_ for _ in ()).throw(PermissionError("locked")))
    with pytest.raises(ZipRewriteError):
        _rewrite(src, dispose_original=disposed.append, temp_suffix=".tmp_keep")
    monkeypatch.undo()
    assert disposed == [] and _read_all(src) == before and _leftovers(tmp_path) == []


def test_a_sharing_violation_on_replace_is_retried(tmp_path, monkeypatch):
    # T7: winerror 32 (another program has the file open) is an OSError, not a
    # PermissionError -- the old retry loop gave up on it at once.
    import sys

    import redactor_common.core.zip_rewrite as zip_rewrite

    monkeypatch.setattr(zip_rewrite, "_LOCK_RETRY", {"delay": 0})
    monkeypatch.setattr(sys, "platform", "win32")
    src = _make(tmp_path / "x.zip")
    real = os.replace
    calls = []

    def flaky(a, b):
        calls.append(b)
        if len(calls) < 3:
            err = OSError("in use")
            err.winerror = 32
            raise err
        real(a, b)

    monkeypatch.setattr(os, "replace", flaky)
    _rewrite(src, RewritePlan(decide=lambda e, r: Action(drop=e.name == "a.txt")))
    monkeypatch.undo()
    assert len(calls) == 3 and [n for n, _ in _read_all(src)] == ["b.txt", "c.txt"]


def test_cancel_before_any_work_and_midway(tmp_path):
    src = _make(tmp_path / "x.zip", [(f"{i}.txt", b"x") for i in range(6)])
    before = _read_all(src)
    with pytest.raises(RewriteCancelled):
        _rewrite(src, should_cancel=lambda: True)
    assert _read_all(src) == before and _leftovers(tmp_path) == []

    polls = []

    def cancel_second_window():
        polls.append(1)
        return len(polls) >= 2

    with pytest.raises(RewriteCancelled):
        _rewrite(src, RewritePlan(window=2), should_cancel=cancel_second_window)
    assert _read_all(src) == before and _leftovers(tmp_path) == []
    assert isinstance(RewriteCancelled("x"), ZipRewriteError)

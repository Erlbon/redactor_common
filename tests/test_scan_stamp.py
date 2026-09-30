"""Tests for the shared scan stamp + content fingerprint."""

import datetime

from redactor_common.core.scan_stamp import (
    ScanStamp,
    content_fingerprint,
    make_stamp,
    now_timestamp,
    parse_stamp,
    stamp_is_current,
)

FIXED = datetime.datetime(2026, 9, 30, 14, 5, 11, tzinfo=datetime.timezone.utc)


def test_round_trip_with_and_without_fingerprint():
    plain = make_stamp("OK", now=FIXED)
    assert plain.to_text() == "OK;2026-09-30T14:05:11Z"
    assert parse_stamp(plain.to_text()) == plain
    with_fp = make_stamp("WARNING", fingerprint="123-abc", now=FIXED)
    assert with_fp.to_text() == "WARNING;2026-09-30T14:05:11Z;123-abc"
    assert parse_stamp(with_fp.to_text()) == with_fp


def test_status_token_cannot_break_the_format():
    assert make_stamp("A;B", now=FIXED).to_text() == "A_B;2026-09-30T14:05:11Z"


def test_parse_never_raises_and_rejects_garbage():
    for bad in (None, 5, "", "OK", "OK;not-a-time", ";2026-09-30T14:05:11Z", "a;b;c;d", "OK;2026-13-40T99:99:99Z"):
        assert parse_stamp(bad) is None
    assert parse_stamp("  OK ; 2026-09-30T14:05:11Z  ") == ScanStamp("OK", "2026-09-30T14:05:11Z")


def test_now_timestamp_is_utc_seconds():
    assert now_timestamp(FIXED) == "2026-09-30T14:05:11Z"
    assert parse_stamp("OK;" + now_timestamp()) is not None


def test_display_and_tooltip():
    stamp = make_stamp("OK", now=FIXED)
    assert stamp.display(local=False) == "OK · 2026-09-30 14:05"
    assert stamp.display(local=False, with_time=False) == "OK · 2026-09-30"
    assert stamp.display().startswith("OK · 2026-")  # local zone may shift the day
    assert ScanStamp("OK", "garbled").display() == "OK"
    assert "OK" in stamp.tooltip("fine") and "fine" in stamp.tooltip("fine")


def _write(path, data):
    path.write_bytes(data)
    return str(path)


def test_fingerprint_is_stable_and_detects_payload_change(tmp_path):
    data = bytes(range(256)) * 2000  # ~500 KB, larger than the three samples
    a = _write(tmp_path / "a.bin", data)
    b = _write(tmp_path / "b.bin", data)
    assert content_fingerprint(a) == content_fingerprint(b) != ""
    changed_end = _write(tmp_path / "c.bin", data[:-1] + b"\x00")
    assert content_fingerprint(changed_end) != content_fingerprint(a)
    shorter = _write(tmp_path / "d.bin", data[:-10])
    assert content_fingerprint(shorter) != content_fingerprint(a)


def test_fingerprint_ignores_skipped_header_so_tag_edits_keep_the_stamp(tmp_path):
    payload = bytes(range(256)) * 1000
    tag_a = b"TAG-A" * 100
    tag_b = b"another, longer tag" * 300
    one = _write(tmp_path / "one.bin", tag_a + payload)
    two = _write(tmp_path / "two.bin", tag_b + payload)
    assert content_fingerprint(one, skip=[(0, len(tag_a))]) == content_fingerprint(two, skip=[(0, len(tag_b))])
    assert content_fingerprint(one) != content_fingerprint(two)


def test_fingerprint_skip_trailer_and_clamping(tmp_path):
    payload = bytes(range(256)) * 500
    plain = _write(tmp_path / "p.bin", payload)
    trailer = b"X" * 128
    with_trailer = _write(tmp_path / "t.bin", payload + trailer)
    skip = [(len(payload), len(payload) + len(trailer) + 50)]  # end past EOF is clamped
    assert content_fingerprint(with_trailer, skip=skip) == content_fingerprint(plain)


def test_fingerprint_small_and_missing_files(tmp_path):
    tiny = _write(tmp_path / "tiny.bin", b"abc")
    assert content_fingerprint(tiny).startswith("3-")
    empty = _write(tmp_path / "empty.bin", b"")
    assert content_fingerprint(empty).startswith("0-")
    assert content_fingerprint(str(tmp_path / "missing.bin")) == ""


def test_stamp_is_current(tmp_path):
    path = _write(tmp_path / "f.bin", bytes(range(256)) * 400)
    stamp = make_stamp("OK", fingerprint=content_fingerprint(path), now=FIXED)
    assert stamp_is_current(stamp, path)
    _write(tmp_path / "f.bin", bytes(range(255, -1, -1)) * 400)
    assert not stamp_is_current(stamp, path)
    assert not stamp_is_current(make_stamp("OK", now=FIXED), path)  # no fingerprint -> can't vouch
    assert not stamp_is_current(None, path)

"""gui/media_preview.py: the pure helpers. The window itself (QMediaPlayer, FFmpeg backend) is not built here:
its shutdown crashed the whole pytest process about one run in three, so it is checked by hand instead."""

from redactor_common.gui import media_preview as mp



def test_format_time():
    assert mp.format_time(0) == "0:00"
    assert mp.format_time(65_000) == "1:05"
    assert mp.format_time(3_600_000) == "1:00:00"
    assert mp.format_time(-5) == "0:00"


def test_track_label():
    assert mp.track_label(0, "English", "") == "1 - English"
    assert mp.track_label(1, "", "") == "2"
    assert mp.track_label(2, "Norwegian", "Commentary") == "3 - Norwegian - Commentary"

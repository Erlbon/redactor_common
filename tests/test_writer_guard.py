"""core/writer_guard.py: the scan each app's guard test runs over its own source."""
import os

from redactor_common.core.writer_guard import find_private_writers, scan_source


def whats(source):
    return [what for _line, what in scan_source(source)]


def test_zip_writes_are_found_and_reads_are_not():
    assert whats('import zipfile\nzipfile.ZipFile(p, "w")\nzipfile.ZipFile(p, mode="a")\nzipfile.ZipFile(p)\nZipFile(p, "r")\n') == [
        'ZipFile opened with mode "w"', 'ZipFile opened with mode "a"']
    assert whats("z.writestr(name, data)\n") == ["ZipFile.writestr()"]


def test_mutagen_saves_count_only_on_what_mutagen_made():
    assert whats("from mutagen.mp3 import MP3\naudio = MP3(p)\naudio.save()\naudio.tags.delete()\n") == [
        "a mutagen .save()", "a mutagen .delete()"]
    assert whats("import mutagen\neasy = mutagen.File(p, easy=True)\neasy.save(v2_version=3)\n") == ["a mutagen .save()"]
    assert whats("from mutagen.mp3 import MP3\naudio = retry(lambda: MP3(p))\nretry(lambda: audio.save())\n") == ["a mutagen .save()"]
    assert whats("from mutagen.id3 import ID3\nID3(p).save()\n") == ["a mutagen .save()"]
    assert whats("import mutagen\nimage.save(out, 'PNG')\nctx.save()\n") == []


def test_xml_serialization_counts_only_next_to_comicinfo():
    assert whats("import xml.etree.ElementTree as ET\nNAME = 'ComicInfo.xml'\nET.tostring(root)\n") == [
        "XML serialized in a module that handles ComicInfo"]
    assert whats("import xml.etree.ElementTree as ET\nET.tostring(root)\n") == []
    assert whats("# ComicInfo: never ET.tostring(root) here\nx = 1\n") == []


def test_the_folder_scan_skips_tests_and_the_given_paths(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "core" / "bad.py").write_text('import zipfile\nzipfile.ZipFile("x", "w")\n')
    (tmp_path / "core" / "allowed.py").write_text('import zipfile\nzipfile.ZipFile("x", "w")\n')
    (tmp_path / "tests" / "helper.py").write_text('import zipfile\nzipfile.ZipFile("x", "w")\n')
    (tmp_path / "test_top.py").write_text('import zipfile\nzipfile.ZipFile("x", "w")\n')
    assert find_private_writers(str(tmp_path), skip=["core/allowed.py"]) == ['core/bad.py:2: ZipFile opened with mode "w"']


def test_redactor_common_itself_writes_only_in_the_shared_writers():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    allowed = ["core/zip_rewrite.py", "core/audio_write.py", "core/comicinfo_write.py"]
    assert find_private_writers(here, skip=allowed) == []

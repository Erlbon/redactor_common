"""core/audio_write.py, the one mutagen writer. The checks run in a separate Python (tests/_audio_write_checks.py), as the reader
checks do (test_meta_readers.py says why)."""
import os
import subprocess
import sys

import pytest

from redactor_common.core import audio_write as aw


def test_audio_writer_checks_in_a_clean_interpreter(tmp_path):
    pytest.importorskip("mutagen")
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))                   # the parent of the checkout: import redactor_common resolves here
    env = dict(os.environ, PYTHONPATH=root + os.pathsep + os.environ.get("PYTHONPATH", ""))
    done = subprocess.run([sys.executable, os.path.join(here, "_audio_write_checks.py"), str(tmp_path)], capture_output=True,
                          text=True, env=env, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr


def test_id3_save_kwargs_keep_only_v23():
    class Tags:
        version = (2, 3, 0)

    assert aw.id3_save_kwargs(Tags()) == {"v2_version": 3}
    Tags.version = (2, 4, 0)
    assert aw.id3_save_kwargs(Tags()) == {} and aw.id3_save_kwargs(None) == {}


def test_save_tags_passes_the_kept_version_and_lets_the_caller_override():
    got = []

    class Audio:
        class tags:
            version = (2, 3, 0)

        def save(self, **kw):
            got.append(kw)

    aw.save_tags(Audio())
    aw.save_tags(Audio(), v2_version=4)
    aw.save_tags(Audio(), keep_id3_version=False)
    assert got == [{"v2_version": 3}, {"v2_version": 4}, {}]

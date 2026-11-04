"""Nothing under core/lookups/ (nor the clients they build on) may import Qt: the folder must be able to leave
redactor_common as its own package, for a Qt-free program, unchanged. Each module is imported in a fresh interpreter
in which importing PyQt6 fails."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LOOKUPS = sorted(p.stem for p in (REPO / "core" / "lookups").glob("*.py") if p.stem != "__init__")
MODULES = ([f"redactor_common.core.lookups.{name}" for name in LOOKUPS]
           + ["redactor_common.core.lookups", "redactor_common.core.wikipedia", "redactor_common.core.lookup_client"])

BLOCK_QT = """
import importlib.abc, sys

class NoQt(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "PyQt6" or name.startswith("PyQt6."):
            raise ImportError("Qt is blocked in this test: " + name)
        return None

sys.meta_path.insert(0, NoQt())
import importlib
importlib.import_module(sys.argv[1])
assert not any(m == "PyQt6" or m.startswith("PyQt6.") for m in sys.modules)
"""


def test_every_lookup_module_is_listed():
    assert {"musicbrainz", "discogs", "tmdb", "release_name", "cover_finder"} <= set(LOOKUPS)


@pytest.mark.parametrize("module", MODULES)
def test_imports_without_qt(module):
    env = dict(os.environ)
    # The parent of this checkout first, so `redactor_common` is THIS checkout, not an installed copy.
    env["PYTHONPATH"] = os.pathsep.join([str(REPO.parent)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    result = subprocess.run([sys.executable, "-c", BLOCK_QT, module], env=env, capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 0, result.stderr

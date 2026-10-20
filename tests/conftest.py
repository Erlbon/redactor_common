"""Test session housekeeping.

Many test modules create the one QApplication at import time and keep Qt objects alive to the end. With Python 3.15 on
Windows the interpreter then sometimes crashed in its own shutdown (an access violation, shell exit code 139) after every test
had passed and the report was printed; which runs crashed changed with the number of tests, not with what they did. The
pre-push hook reads the exit code, so a crash after a green run blocked pushes.

Once pytest is completely done (``pytest_unconfigure`` is its last hook, after the summary has been printed) the process
now ends at once with pytest's own exit code, skipping the interpreter's shutdown. Nothing is hidden: a failing run still
exits 1, and all output has been written.
"""
import os
import sys

_status = {"code": None}


def pytest_sessionfinish(session, exitstatus):
    _status["code"] = int(exitstatus)


def pytest_unconfigure(config):
    code = _status["code"]
    if code is None:
        return
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:   # noqa: BLE001 -- nothing may stop the exit code from being passed on
        pass
    os._exit(code)

"""Shared cleanup for the tests: a test run leaves no folders behind.

Every test file makes scratch folders with tempfile.mkdtemp() and never deleted them, so each
run left its folders - with their multi-GB model fixtures - in the temp folder for good (one
run filled 63 GB). Patching mkdtemp in this package, which every test run imports, means all
of them are removed when the run ends, under pytest and under plain unittest.
"""
import atexit
import shutil
import tempfile

_made: list[str] = []
_real_mkdtemp = tempfile.mkdtemp


def _mkdtemp(*args, **kwargs) -> str:
    path = _real_mkdtemp(*args, **kwargs)
    _made.append(path)
    return path


def clean_mkdtemps() -> int:
    """Delete every folder this run made, newest first (some live inside others)."""
    n = 0
    for path in reversed(_made):
        try:
            shutil.rmtree(path, ignore_errors=True)
            n += 1
        except OSError:
            pass                    # a server process may still hold a folder: leave it
    _made.clear()
    return n


tempfile.mkdtemp = _mkdtemp
atexit.register(clean_mkdtemps)

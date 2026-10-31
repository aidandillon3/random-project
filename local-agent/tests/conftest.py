"""Most tests are about one tool group or another, written when every group was on by default. Since the
pi-like default (only read_file, write_file, edit_file, run_command), they get every group turned on unless
they set a group themselves. Tests of the default itself are marked @pytest.mark.lean and get the real one."""

import os
import tempfile

import pytest

os.environ.setdefault("LOCALAGENT_DATA", tempfile.mkdtemp())    # before localagent is imported: never the real data

from localagent.config import Settings  # noqa: E402

EVERY_GROUP = dict(enable_more_files=True, enable_more_shell=True, enable_planning=True, enable_memory=True,
                   enable_web=True, enable_subagents=True, enable_github=True, enable_code=True, enable_unity=True)


def pytest_configure(config):
    config.addinivalue_line("markers", "lean: run with the app's real (pi-like) default tool set")


@pytest.fixture(autouse=True)
def _every_tool_group(request, monkeypatch):
    if request.node.get_closest_marker("lean"):
        yield
        return
    real = Settings.__init__

    def init(self, *args, **kwargs):
        for k, v in EVERY_GROUP.items():
            kwargs.setdefault(k, v)
        real(self, *args, **kwargs)

    monkeypatch.setattr(Settings, "__init__", init)
    yield

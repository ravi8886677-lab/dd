"""Establishing identity is safe when two processes do it at once.

The daemon and the dashboard are separate processes: the tray spawns
`python -m desktop_app.memory_viewer` alongside the running daemon. On a
fresh install both establish identity, the daemon during a startup that
also loads models and discovers MCP servers, the dashboard on its first
request. "Launch the app, then click Dashboard" puts them in the same
window.

A lock inside one process cannot see the other one, so these tests use
real processes. Threads pass against a version of the code that these
fail against, which is the whole point.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from jarvis.identity import IdentityStore

#: Long enough for every child to be through interpreter start-up and
#: sitting on the barrier before any of them touches the database.
_START_DELAY_SEC = 2.0

_CHILD = """
import os, sys, time
sys.path.insert(0, {src!r})
os.environ["JARVIS_DATA_DIR"] = {data!r}
from jarvis.identity import IdentityStore

# Everything expensive happens before the barrier, so the only thing
# left to overlap is the establishing itself.
store = IdentityStore({db!r})
start = {start!r}
while time.time() < start:
    pass
store.ensure_local_identity()
store.close()
"""


def _race(tmp_path, processes: int) -> IdentityStore:
    """Start N processes that all establish identity at the same instant."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = str(tmp_path / "jarvis.db")
    start = time.time() + _START_DELAY_SEC

    src = str((__import__("pathlib").Path(__file__).parents[1] / "src").resolve())
    children = [
        subprocess.Popen(
            [sys.executable, "-c", _CHILD.format(
                src=src, data=str(data_dir), db=db_path, start=start,
            )],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "JARVIS_DATA_DIR": str(data_dir)},
        )
        for _ in range(processes)
    ]
    for child in children:
        _, stderr = child.communicate(timeout=60)
        assert child.returncode == 0, stderr.decode()

    return IdentityStore(db_path)


@pytest.mark.unit
class TestTwoProcessesStartingTogether:
    def test_they_agree_on_one_user(self, tmp_path):
        store = _race(tmp_path, processes=2)
        try:
            assert len(store.get_users()) == 1
        finally:
            store.close()

    def test_they_agree_on_one_workspace(self, tmp_path):
        store = _race(tmp_path, processes=2)
        try:
            assert len(store.get_workspaces()) == 1
        finally:
            store.close()

    def test_they_agree_on_one_device(self, tmp_path):
        store = _race(tmp_path, processes=2)
        try:
            assert len(store.get_devices()) == 1
        finally:
            store.close()


@pytest.mark.unit
class TestManyProcessesStartingTogether:
    """Two is the case that happens; more is what makes the race visible."""

    def test_six_of_them_still_produce_one_identity(self, tmp_path):
        store = _race(tmp_path, processes=6)
        try:
            assert len(store.get_users()) == 1
            assert len(store.get_workspaces()) == 1
            assert len(store.get_devices()) == 1
        finally:
            store.close()

    def test_none_of_them_fails(self, tmp_path):
        """A losing process must return, not raise.

        The loser is whichever of the daemon and the dashboard got there
        second, and neither is allowed to die because the other won.
        """
        store = _race(tmp_path, processes=6)  # _race asserts every exit code
        store.close()

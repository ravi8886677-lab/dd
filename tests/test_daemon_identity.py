"""Starting Jarvis establishes who it is acting for.

The rows are useless if nothing creates them, and a device that is never
marked as seen cannot be shown to the user or attached to a permission.
So the daemon does it on the way up, before anything can act.
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest

from jarvis.identity import IdentityStore


def _run_daemon_startup(db_path: str) -> str:
    """Boot the daemon far enough to initialise, and return its output.

    The heavy parts are stood in for; the identity store is not, because
    it is what these tests are about.
    """
    from contextlib import redirect_stdout

    from jarvis import daemon
    from tests.conftest import MockConfig

    with patch("jarvis.daemon.load_settings") as mock_load, \
         patch("jarvis.daemon.Database") as mock_db, \
         patch("jarvis.daemon.discover_and_report_mcp_tools", return_value={}), \
         patch("jarvis.daemon.DialogueMemory") as mock_dm, \
         patch("jarvis.daemon.get_location_context", return_value="Location: Test"), \
         patch("jarvis.daemon.create_tts_engine") as mock_tts, \
         patch("jarvis.daemon.VoiceListener") as mock_vl, \
         patch("jarvis.memory.graph.GraphMemoryStore") as mock_graph:

        mock_load.return_value = MockConfig(db_path=db_path)
        mock_db.return_value = MagicMock()
        mock_dm.return_value = MagicMock()

        tts = MagicMock()
        tts.enabled = False
        mock_tts.return_value = tts
        mock_vl.return_value = MagicMock()

        graph = MagicMock()
        graph.migrate_legacy_shape.return_value = False
        mock_graph.return_value = graph

        captured = io.StringIO()
        with redirect_stdout(captured):
            daemon.main(smoke_test=True)
        return captured.getvalue()


@pytest.mark.unit
class TestStartupEstablishesIdentity:
    def test_a_first_launch_records_the_user_and_this_device(self, tmp_path):
        db_path = str(tmp_path / "jarvis.db")

        _run_daemon_startup(db_path)

        store = IdentityStore(db_path)
        try:
            assert len(store.get_users()) == 1
            assert len(store.get_devices()) == 1
            assert len(store.get_workspaces()) == 1
        finally:
            store.close()

    def test_it_says_which_machine_it_is_running_on(self, tmp_path):
        """Startup output is how the user knows what Jarvis thinks it is."""
        db_path = str(tmp_path / "jarvis.db")

        output = _run_daemon_startup(db_path)

        store = IdentityStore(db_path)
        try:
            device = store.get_devices()[0]
        finally:
            store.close()

        assert device.name in output

    def test_a_second_launch_adds_nothing(self, tmp_path):
        db_path = str(tmp_path / "jarvis.db")

        _run_daemon_startup(db_path)
        _run_daemon_startup(db_path)

        store = IdentityStore(db_path)
        try:
            assert len(store.get_users()) == 1
            assert len(store.get_devices()) == 1
        finally:
            store.close()

    def test_a_broken_identity_store_does_not_stop_the_daemon(self, tmp_path):
        """Failing open: knowing who you are is not a reason to refuse to run.

        Nothing depends on these rows yet, and when something does it
        will gate on their absence rather than on Jarvis having crashed
        at boot.
        """
        db_path = str(tmp_path / "jarvis.db")

        with patch("jarvis.daemon.IdentityStore", side_effect=OSError("disk gone")):
            output = _run_daemon_startup(db_path)

        assert "SMOKE_TEST_INIT_OK" in output

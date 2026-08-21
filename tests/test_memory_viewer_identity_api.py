"""The dashboard says which machine it is talking to.

Two processes share one database, and until now the dashboard could not
tell you whose data it was showing or where that data was being written
from. With more than one device on one user, that stops being cosmetic:
it is how you know the window in front of you is not showing another
machine's view.
"""

from __future__ import annotations

import pytest

try:
    import flask  # noqa: F401

    _HAS_FLASK = True
except ImportError:
    _HAS_FLASK = False

from tests.dashboard_assets import read_js, read_template

pytestmark = pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    from src.desktop_app import memory_viewer

    db_path = tmp_path / "jarvis.db"
    monkeypatch.setattr(memory_viewer, "_get_db_path", lambda: str(db_path))
    memory_viewer._db_conn = None
    memory_viewer._graph_store = None
    memory_viewer._identity_store = None
    memory_viewer.app.config["TESTING"] = True

    with memory_viewer.app.test_client() as client:
        client.environ_base["HTTP_X_DASHBOARD_TOKEN"] = memory_viewer._SESSION_TOKEN
        yield client, db_path

    if memory_viewer._db_conn is not None:
        memory_viewer._db_conn.close()
        memory_viewer._db_conn = None
    if memory_viewer._identity_store is not None:
        memory_viewer._identity_store.close()
        memory_viewer._identity_store = None


@pytest.mark.unit
class TestTheIdentityEndpoint:
    def test_it_needs_the_token_like_everything_else(self, tmp_path, monkeypatch):
        from src.desktop_app import memory_viewer

        monkeypatch.setattr(
            memory_viewer, "_get_db_path", lambda: str(tmp_path / "jarvis.db"),
        )
        memory_viewer._identity_store = None
        memory_viewer.app.config["TESTING"] = True
        with memory_viewer.app.test_client() as anonymous:
            assert anonymous.get("/api/identity").status_code == 401

    def test_it_reports_the_device_and_workspace(self, dashboard):
        client, _ = dashboard

        body = client.get("/api/identity").get_json()

        assert body["device"]["name"]
        assert body["device"]["platform"]
        assert body["workspace"]["name"]

    def test_it_establishes_the_identity_on_a_fresh_install(self, dashboard):
        """The dashboard may open before the daemon has ever run."""
        client, db_path = dashboard
        from jarvis.identity import IdentityStore

        assert client.get("/api/identity").status_code == 200

        store = IdentityStore(str(db_path))
        try:
            assert len(store.get_users()) == 1
        finally:
            store.close()

    def test_it_shows_the_same_device_the_daemon_recorded(self, dashboard):
        client, db_path = dashboard
        from jarvis.identity import IdentityStore

        store = IdentityStore(str(db_path))
        try:
            recorded = store.ensure_local_identity()
        finally:
            store.close()

        body = client.get("/api/identity").get_json()

        assert body["device"]["id"] == recorded.device.id

    def test_it_lists_other_devices_without_pretending_they_are_this_one(
        self, dashboard,
    ):
        from unittest.mock import patch

        client, db_path = dashboard
        from jarvis.identity import IdentityStore

        store = IdentityStore(str(db_path))
        try:
            here = store.ensure_local_identity()
            with patch("jarvis.identity.local_device_id", lambda: "another-machine"):
                store.ensure_local_identity()
        finally:
            store.close()

        body = client.get("/api/identity").get_json()

        assert body["device"]["id"] == here.device.id
        assert len(body["devices"]) == 2

    def test_it_does_not_list_another_user_s_devices_or_accounts(self, dashboard):
        """The endpoint says "your devices", so it has to mean it."""
        client, db_path = dashboard
        from jarvis.identity import IdentityStore
        from tests.test_identity import _add_a_second_user_with_their_own_things

        store = IdentityStore(str(db_path))
        try:
            mine = store.ensure_local_identity()
            _add_a_second_user_with_their_own_things(store)
        finally:
            store.close()

        body = client.get("/api/identity").get_json()

        assert [d["id"] for d in body["devices"]] == [mine.device.id]
        assert body["accounts"] == []

    def test_no_account_is_connected_on_a_fresh_install(self, dashboard):
        client, _ = dashboard

        assert client.get("/api/identity").get_json()["accounts"] == []


@pytest.mark.unit
class TestThePageShowsIt:
    def test_the_markup_carries_a_place_for_it(self):
        assert "identity-device" in read_template()

    def test_the_script_fills_it_from_the_endpoint(self):
        script = read_js()

        assert "/api/identity" in script
        assert "identity-device" in script

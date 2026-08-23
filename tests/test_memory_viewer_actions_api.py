"""The audit timeline: a log nobody can read is not an audit log.

The spec lists it as an MVP item for a reason. "Every external action
attributable" is a property about what the user can find out, not about
what a table contains, so the slice is not finished until the record has
a surface.
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
    memory_viewer._action_log = None
    memory_viewer.app.config["TESTING"] = True

    with memory_viewer.app.test_client() as client:
        client.environ_base["HTTP_X_DASHBOARD_TOKEN"] = memory_viewer._SESSION_TOKEN
        yield client, db_path

    for attr in ("_action_log", "_identity_store"):
        store = getattr(memory_viewer, attr, None)
        if store is not None:
            store.close()
            setattr(memory_viewer, attr, None)
    if memory_viewer._db_conn is not None:
        memory_viewer._db_conn.close()
        memory_viewer._db_conn = None


@pytest.mark.unit
class TestTheActionsEndpoint:
    def test_it_needs_the_token_like_everything_else(self, tmp_path, monkeypatch):
        from src.desktop_app import memory_viewer

        monkeypatch.setattr(
            memory_viewer, "_get_db_path", lambda: str(tmp_path / "jarvis.db"),
        )
        memory_viewer._action_log = None
        memory_viewer.app.config["TESTING"] = True
        with memory_viewer.app.test_client() as anonymous:
            assert anonymous.get("/api/actions").status_code == 401

    def test_a_fresh_install_has_nothing_to_show(self, dashboard):
        client, _ = dashboard

        response = client.get("/api/actions")

        assert response.status_code == 200
        assert response.get_json()["actions"] == []

    def test_it_shows_an_action_with_its_outcome(self, dashboard):
        client, db_path = dashboard
        from jarvis.audit import ActionLog, Outcome, Verification

        log = ActionLog(str(db_path))
        try:
            action_id = log.record_decision(
                tool_name="localFiles", tool_source="builtin",
            )
            log.record_outcome(
                action_id, outcome=Outcome.OK, verification=Verification.CONFIRMED,
            )
        finally:
            log.close()

        actions = client.get("/api/actions").get_json()["actions"]

        assert actions[0]["tool_name"] == "localFiles"
        assert actions[0]["outcome"] == "ok"
        assert actions[0]["verification"] == "confirmed"

    def test_an_unfinished_action_is_visible_as_unfinished(self, dashboard):
        client, db_path = dashboard
        from jarvis.audit import ActionLog

        log = ActionLog(str(db_path))
        try:
            log.record_decision(tool_name="openApp", tool_source="builtin")
        finally:
            log.close()

        actions = client.get("/api/actions").get_json()["actions"]

        assert actions[0]["outcome"] is None

    def test_a_refusal_carries_the_rule_that_refused(self, dashboard):
        client, db_path = dashboard
        from jarvis.audit import ActionLog, Decision

        log = ActionLog(str(db_path))
        try:
            log.record_decision(
                tool_name="computerUse", tool_source="builtin",
                decision=Decision.DENIED, decision_reason="YOLO mode is off",
                policy_rule_id="computer_use.yolo",
            )
        finally:
            log.close()

        action = client.get("/api/actions").get_json()["actions"][0]

        assert action["decision"] == "denied"
        assert action["policy_rule_id"] == "computer_use.yolo"

    def test_the_grant_appears_alongside_what_it_allowed(self, dashboard):
        client, db_path = dashboard
        from jarvis.audit import ActionLog

        log = ActionLog(str(db_path))
        try:
            log.record_human_event("yolo.granted", detail="30 minutes")
        finally:
            log.close()

        action = client.get("/api/actions").get_json()["actions"][0]

        assert action["tool_source"] == "human"
        assert action["tool_name"] == "yolo.granted"


@pytest.mark.unit
class TestThePageShowsTheTimeline:
    def test_there_is_a_tab_for_it(self):
        assert 'data-tab="activity"' in read_template()

    def test_a_human_decision_is_not_rendered_as_an_unfinished_action(self):
        """A grant is a thing that happened, not a call awaiting a result.

        It has no outcome entry because there is nothing to come back
        from, and showing that as "never finished" reads as a fault.
        """
        script = read_js()

        marker = "function activityStatus"
        body = script[script.index(marker):script.index(marker) + 900]
        assert "human" in body, "the renderer must special-case a human decision"
        assert body.index("human") < body.index("never finished")

    def test_the_script_loads_it_from_the_endpoint(self):
        script = read_js()

        assert "/api/actions" in script
        assert "activity" in script

"""The dashboard must work on an install that has never run Jarvis.

Opening the dashboard before speaking to the assistant is an ordinary
thing to do: the tray menu offers it, and a new user clicks it. At that
moment nothing has created the database, because the tables are applied
when the daemon starts.

So the dashboard has to be able to open a data directory that is not
there yet and answer with empty results rather than an error. The
knowledge-graph endpoints have always done this, and these tests hold
the diary, stats and meal endpoints to the same contract.

The last test is the one that keeps the two halves connected: whichever
side creates the file first, the other must recognise what it finds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    import flask  # noqa: F401

    _HAS_FLASK = True
except ImportError:
    _HAS_FLASK = False

pytestmark = pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")


@pytest.fixture(params=["no data directory", "empty data directory"])
def fresh_install(request, tmp_path, monkeypatch):
    """A dashboard pointed at a database the daemon has never created.

    Both shapes reach a user: the directory is absent before the first
    launch, and present but empty once anything else has written to the
    data directory first.
    """
    from src.desktop_app import memory_viewer

    db_path = tmp_path / "never-launched" / "jarvis.db"
    if request.param == "empty data directory":
        db_path.parent.mkdir(parents=True)
    assert not db_path.exists(), "the point of the fixture"

    monkeypatch.setattr(memory_viewer, "_get_db_path", lambda: str(db_path))
    memory_viewer._db_conn = None
    memory_viewer._graph_store = None
    memory_viewer.app.config["TESTING"] = True

    with memory_viewer.app.test_client() as client:
        client.environ_base["HTTP_X_DASHBOARD_TOKEN"] = memory_viewer._SESSION_TOKEN
        yield client, db_path

    if memory_viewer._db_conn is not None:
        memory_viewer._db_conn.close()
        memory_viewer._db_conn = None
    if memory_viewer._graph_store is not None:
        memory_viewer._graph_store.close()
        memory_viewer._graph_store = None


@pytest.mark.unit
class TestAFreshInstallGetsEmptyResultsNotErrors:
    def test_the_stats_endpoint_reports_nothing_recorded_yet(self, fresh_install):
        client, _ = fresh_install

        response = client.get("/api/stats")

        assert response.status_code == 200
        body = response.get_json()
        assert body["total_memories"] == 0
        assert body["total_meals"] == 0
        assert body["earliest_date"] is None

    def test_the_diary_is_empty_rather_than_broken(self, fresh_install):
        client, _ = fresh_install

        response = client.get("/api/memories")

        assert response.status_code == 200
        assert response.get_json()["memories"] == []

    def test_the_meal_log_is_empty_rather_than_broken(self, fresh_install):
        client, _ = fresh_install

        response = client.get("/api/meals")

        assert response.status_code == 200
        assert response.get_json()["meals"] == []

    def test_the_topic_list_is_empty_rather_than_broken(self, fresh_install):
        client, _ = fresh_install

        response = client.get("/api/topics")

        assert response.status_code == 200

    def test_the_knowledge_graph_still_answers(self, fresh_install):
        """Already true, and it is the shape the others now copy."""
        client, _ = fresh_install

        assert client.get("/api/graph/stats").status_code == 200

    def test_opening_the_dashboard_creates_the_data_directory(self, fresh_install):
        client, db_path = fresh_install

        client.get("/api/stats")

        assert Path(db_path).parent.is_dir()


@pytest.mark.unit
class TestTheDashboardAndTheDaemonShareOneDatabase:
    """Whichever opens the file first, the other must recognise it.

    Two independent pieces of code apply the schema. If they ever drift,
    the failure is silent until a user hits the one endpoint that reads
    the column that only one side knows about.
    """

    def test_the_daemon_can_use_a_file_the_dashboard_created(self, fresh_install):
        client, db_path = fresh_install
        from src.jarvis.memory.db import Database

        client.get("/api/stats")  # dashboard creates the file

        database = Database(str(db_path), sqlite_vss_path=None)
        try:
            database.upsert_conversation_summary(
                date_utc="2026-08-20",
                summary="Talked about the build order.",
                topics="planning",
            )
            assert database.get_conversation_summary("2026-08-20") is not None
        finally:
            database.close()

    def test_the_dashboard_reads_what_the_daemon_wrote(self, fresh_install):
        client, db_path = fresh_install
        from src.jarvis.memory.db import Database

        client.get("/api/stats")  # dashboard creates the file

        database = Database(str(db_path), sqlite_vss_path=None)
        try:
            database.upsert_conversation_summary(
                date_utc="2026-08-20",
                summary="Talked about the build order.",
                topics="planning",
            )
        finally:
            database.close()

        body = client.get("/api/memories").get_json()

        assert [m["summary"] for m in body["memories"]] == [
            "Talked about the build order.",
        ]
        assert client.get("/api/stats").get_json()["total_memories"] == 1

    def test_full_text_search_works_on_a_dashboard_created_file(self, fresh_install):
        """The FTS triggers are the part most easily left out."""
        client, db_path = fresh_install
        from src.jarvis.memory.db import Database

        client.get("/api/stats")  # dashboard creates the file

        database = Database(str(db_path), sqlite_vss_path=None)
        try:
            database.upsert_conversation_summary(
                date_utc="2026-08-20",
                summary="Talked about the build order.",
                topics="planning",
            )
        finally:
            database.close()

        body = client.get("/api/memories?search=build").get_json()

        assert body["count"] == 1

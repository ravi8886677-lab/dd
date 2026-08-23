"""Connecting to a hosted server should not require hand-written JSON.

The remote transport and the OAuth flow are both finished and tested.
Nothing in the dashboard could reach them: the registry's Add endpoint
refused anything without a pinned package, and most hosted servers have
no package at all - they have a URL. Of the servers in the official
registry, the large majority are remote, and every one of them was
unreachable from the UI that lists them.

The refusal is right for a *local* server, where an unpinned package is
a supply-chain hazard. A hosted server is a different thing: the risk is
who you are handing your data to, which is a question about the host.
"""

from __future__ import annotations

import pytest

try:
    import flask  # noqa: F401

    _HAS_FLASK = True
except ImportError:
    _HAS_FLASK = False

pytestmark = pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    from src.desktop_app import memory_viewer

    config = tmp_path / "config.json"
    monkeypatch.setattr(memory_viewer, "_config_path", lambda: config)
    memory_viewer.app.config["TESTING"] = True
    memory_viewer._reset_rate_limits()

    with memory_viewer.app.test_client() as client:
        client.environ_base["HTTP_X_DASHBOARD_TOKEN"] = memory_viewer._SESSION_TOKEN
        yield client, config


def _cached(monkeypatch, entry):
    from src.desktop_app import memory_viewer  # noqa: F401
    import jarvis.tools.external.mcp_registry as registry

    monkeypatch.setattr(registry, "find_cached", lambda name: entry)


def _written(config):
    import json

    return json.loads(config.read_text())["mcps"]


@pytest.mark.unit
class TestAHostedServerCanBeAdded:
    def test_a_server_with_only_a_url_is_written_as_a_remote_entry(
        self, dashboard, monkeypatch,
    ):
        client, config = dashboard
        _cached(monkeypatch, {
            "name": "com.example/notes",
            "install": None,
            "remote_url": "https://mcp.example.com/mcp",
            "remote_transport": "streamable-http",
        })

        response = client.post("/api/mcp/registry/add", json={"name": "com.example/notes"})

        assert response.status_code == 200
        written = _written(config)["notes"]
        assert written["url"] == "https://mcp.example.com/mcp"
        assert written["transport"] == "http"
        assert written["auth"] == "oauth"
        assert "command" not in written

    def test_it_still_refuses_a_server_with_neither_package_nor_url(
        self, dashboard, monkeypatch,
    ):
        client, _ = dashboard
        _cached(monkeypatch, {"name": "com.example/nothing", "install": None, "remote_url": None})

        response = client.post("/api/mcp/registry/add", json={"name": "com.example/nothing"})

        assert response.status_code == 400

    def test_a_pinned_package_still_wins_over_a_url(self, dashboard, monkeypatch):
        """A server offering both is installed locally, as before."""
        client, config = dashboard
        _cached(monkeypatch, {
            "name": "com.example/both",
            "install": {"transport": "stdio", "command": "npx", "args": ["-y", "pkg@1.2.3"]},
            "remote_url": "https://mcp.example.com/mcp",
        })

        response = client.post("/api/mcp/registry/add", json={"name": "com.example/both"})

        assert response.status_code == 200
        assert _written(config)["both"]["command"] == "npx"


@pytest.mark.unit
class TestTheUrlIsCheckedBeforeItIsStored:
    def test_plain_http_is_refused(self, dashboard, monkeypatch):
        """The client blocks plain HTTP at spawn time; refuse it earlier."""
        client, _ = dashboard
        _cached(monkeypatch, {
            "name": "com.example/insecure", "install": None,
            "remote_url": "http://mcp.example.com/mcp",
        })

        response = client.post("/api/mcp/registry/add", json={"name": "com.example/insecure"})

        assert response.status_code == 400

    def test_a_transport_jarvis_cannot_speak_is_refused_with_a_reason(
        self, dashboard, monkeypatch,
    ):
        """SSE is common in the registry and Jarvis does not speak it.

        Writing the entry anyway would produce a server that fails at
        connect time with an error about transports, long after the user
        clicked Add and believed it worked.
        """
        client, _ = dashboard
        _cached(monkeypatch, {
            "name": "com.example/streamer", "install": None,
            "remote_url": "https://mcp.example.com/sse",
            "remote_transport": "sse",
        })

        response = client.post("/api/mcp/registry/add", json={"name": "com.example/streamer"})

        assert response.status_code == 400
        assert "sse" in response.get_json()["error"].lower()


@pytest.mark.unit
class TestTheRegistryKeepsTheTransportItDeclared:
    def test_the_declared_transport_is_cached(self):
        """`_normalise` kept the URL and dropped the type beside it."""
        from jarvis.tools.external.mcp_registry import _normalise

        entry = _normalise({
            "server": {
                "name": "com.example/notes", "description": "d", "version": "1",
                "remotes": [{"type": "streamable-http", "url": "https://x.example/mcp"}],
            },
            "_meta": {"io.modelcontextprotocol.registry/official": {
                "status": "active", "isLatest": True,
            }},
        })

        assert entry["remote_url"] == "https://x.example/mcp"
        assert entry["remote_transport"] == "streamable-http"

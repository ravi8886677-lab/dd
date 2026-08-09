"""The dashboard must not be open to whatever can reach the port.

It serves the user's diary, personal facts and meal log, it can chat as
them, and `/api/mcp` writes a command that Jarvis later spawns. Binding
to loopback is not access control: any local process can connect, and a
page in the user's browser can be aimed at it — DNS rebinding defeats
the assumption that a loopback address means a local caller.
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
def viewer():
    from src.desktop_app import memory_viewer
    return memory_viewer


@pytest.fixture
def client(viewer):
    """Deliberately unauthenticated — most tests here check refusals."""
    viewer.app.config["TESTING"] = True
    with viewer.app.test_client() as c:
        yield c


@pytest.fixture
def auth_client(viewer):
    """Past the gate, for tests about what happens after authentication."""
    viewer.app.config["TESTING"] = True
    with viewer.app.test_client() as c:
        c.environ_base["HTTP_X_DASHBOARD_TOKEN"] = viewer._SESSION_TOKEN
        yield c


@pytest.mark.unit
class TestUnauthenticatedAccessIsRefused:
    def test_reading_the_diary_needs_the_token(self, client):
        assert client.get("/api/memories").status_code == 401

    def test_the_knowledge_graph_needs_the_token(self, client):
        assert client.get("/api/graph/tree").status_code == 401

    def test_chatting_as_the_user_needs_the_token(self, client):
        response = client.post("/api/chat", json={"message": "who am I?"})
        assert response.status_code == 401

    def test_registering_an_mcp_command_needs_the_token(self, client):
        """The sharpest one: this writes a command Jarvis will spawn."""
        response = client.post(
            "/api/mcp", json={"name": "evil", "command": "/bin/sh", "args": ["-c", "id"]},
        )
        assert response.status_code == 401

    def test_deleting_a_connection_needs_the_token(self, client):
        assert client.delete("/api/mcp/anything").status_code == 401

    def test_the_landing_page_explains_itself_rather_than_erroring_blankly(self, client):
        response = client.get("/")
        assert response.status_code == 401
        assert b"token" in response.data.lower()


@pytest.mark.unit
class TestTokenGrantsAccess:
    def test_the_launch_url_opens_the_dashboard(self, client, viewer):
        response = client.get(f"/?token={viewer._SESSION_TOKEN}")
        assert response.status_code == 200
        assert b"Jarvis Memory" in response.data

    def test_opening_it_sets_a_cookie_so_api_calls_carry_the_token(self, client, viewer):
        client.get(f"/?token={viewer._SESSION_TOKEN}")
        assert client.get("/api/stats").status_code == 200

    def test_the_cookie_is_not_readable_by_scripts(self, client, viewer):
        response = client.get(f"/?token={viewer._SESSION_TOKEN}")
        cookie = response.headers.get("Set-Cookie", "")
        assert "HttpOnly" in cookie
        assert "SameSite=Strict" in cookie

    def test_a_header_works_too(self, client, viewer):
        response = client.get(
            "/api/stats", headers={"X-Dashboard-Token": viewer._SESSION_TOKEN},
        )
        assert response.status_code == 200

    def test_a_wrong_token_is_refused(self, client):
        assert client.get("/api/stats?token=not-the-token").status_code == 401


@pytest.mark.unit
class TestHostHeaderIsChecked:
    """Guards DNS rebinding: an attacker's name resolving to 127.0.0.1."""

    def test_a_foreign_host_header_is_refused(self, client, viewer):
        response = client.get(
            "/api/stats",
            headers={"Host": "evil.example.com", "X-Dashboard-Token": viewer._SESSION_TOKEN},
        )
        assert response.status_code == 403

    def test_a_rebinding_host_is_refused_even_with_a_valid_token(self, client, viewer):
        """Host check runs before auth — a stolen token must not be enough."""
        response = client.post(
            "/api/mcp",
            json={"name": "x", "command": "/bin/sh"},
            headers={"Host": "attacker.test", "X-Dashboard-Token": viewer._SESSION_TOKEN},
        )
        assert response.status_code == 403

    @pytest.mark.parametrize("host", ["localhost:5050", "127.0.0.1:5050", "localhost"])
    def test_normal_local_hosts_are_accepted(self, client, viewer, host):
        response = client.get(
            "/api/stats", headers={"Host": host, "X-Dashboard-Token": viewer._SESSION_TOKEN},
        )
        assert response.status_code == 200


@pytest.mark.unit
class TestSettingsTestDoesNotLeakTheKey:
    """`Test connection` sends a credential somewhere. Where matters.

    Before the guard, POSTing a foreign base URL made the endpoint read
    the user's live key out of config.json and hand it to that server —
    credential exfiltration through a button labelled "Test connection".
    """

    def test_a_foreign_host_will_not_borrow_the_saved_key(self, auth_client):
        response = auth_client.post(
            "/api/settings/test",
            json={"llm_base_url": "http://attacker.example/v1"},
        )

        assert response.status_code == 400
        assert b"saved key" in response.data

    def test_a_non_http_scheme_is_refused(self, auth_client):
        response = auth_client.post(
            "/api/settings/test", json={"llm_base_url": "file:///etc/passwd"},
        )

        assert response.status_code == 400

    def test_a_key_is_not_sent_to_a_private_address(self, auth_client):
        """Local model servers are legitimate, but they need no credential."""
        response = auth_client.post(
            "/api/settings/test",
            json={"llm_base_url": "http://127.0.0.1:11434/v1", "llm_api_key": "sk-live-secret"},
        )

        assert response.status_code == 400
        assert b"private address" in response.data

    def test_a_local_endpoint_without_a_key_is_allowed(self, auth_client):
        """Ollama and LM Studio are the normal offline setup."""
        response = auth_client.post(
            "/api/settings/test", json={"llm_base_url": "http://127.0.0.1:11434/v1"},
        )

        # Reaches the request stage rather than being refused outright;
        # nothing is listening in the test environment, hence 502.
        assert response.status_code in (200, 400, 502)
        assert b"private address" not in response.data


@pytest.mark.unit
class TestTheNoAuthDevFlag:
    """A convenience switch must not become a remote-code-execution path.

    `/api/mcp` writes a command Jarvis later spawns, so anything that can
    reach the dashboard unauthenticated can run code as the user. The
    flag exists to make a local demo painless; it does not get to switch
    off the defence against a *webpage* driving the dashboard.
    """

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "maybe"])
    def test_a_falsey_value_does_not_switch_auth_off(self, viewer, monkeypatch, value):
        """`NO_AUTH=0` reading as "on" is how people disable auth by accident."""
        monkeypatch.setenv("JARVIS_DASHBOARD_NO_AUTH", value)
        assert viewer._no_auth_enabled() is False
        assert viewer._token_matches("wrong-token") is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_an_explicit_opt_in_is_honoured(self, viewer, monkeypatch, value):
        monkeypatch.setenv("JARVIS_DASHBOARD_NO_AUTH", value)
        assert viewer._no_auth_enabled() is True
        assert viewer._token_matches("anything") is True

    def test_the_flag_is_off_when_unset(self, viewer, monkeypatch):
        monkeypatch.delenv("JARVIS_DASHBOARD_NO_AUTH", raising=False)
        assert viewer._no_auth_enabled() is False

    def test_the_host_check_survives_the_flag(self, viewer, monkeypatch):
        """The Host check is the rebinding defence, not authentication.

        Without it any website the user visits can point a name at
        127.0.0.1 and drive the dashboard from their browser. No demo
        convenience is worth handing that out.
        """
        monkeypatch.setenv("JARVIS_DASHBOARD_NO_AUTH", "1")
        assert viewer._host_is_allowed("evil.example.com") is False
        assert viewer._host_is_allowed("") is False
        assert viewer._host_is_allowed("localhost:5071") is True

    def test_a_rebinding_request_is_still_refused_with_the_flag_on(
        self, viewer, monkeypatch
    ):
        monkeypatch.setenv("JARVIS_DASHBOARD_NO_AUTH", "1")
        viewer.app.config["TESTING"] = True
        with viewer.app.test_client() as c:
            response = c.get("/api/memories", headers={"Host": "evil.example.com"})
        assert response.status_code == 403

    def test_the_flag_does_let_a_local_browser_in_without_a_token(
        self, viewer, monkeypatch
    ):
        """The point of the flag: a local demo with no launch URL."""
        monkeypatch.setenv("JARVIS_DASHBOARD_NO_AUTH", "1")
        viewer.app.config["TESTING"] = True
        with viewer.app.test_client() as c:
            response = c.get("/api/stats", headers={"Host": "localhost:5071"})
        assert response.status_code != 401

    def test_mcp_registration_is_still_shut_to_a_foreign_host(
        self, viewer, monkeypatch
    ):
        """The endpoint that ends in code execution gets checked explicitly."""
        monkeypatch.setenv("JARVIS_DASHBOARD_NO_AUTH", "1")
        viewer.app.config["TESTING"] = True
        with viewer.app.test_client() as c:
            response = c.post(
                "/api/mcp",
                json={"name": "evil", "command": "sh", "args": ["-c", "id"]},
                headers={"Host": "evil.example.com"},
            )
        assert response.status_code == 403

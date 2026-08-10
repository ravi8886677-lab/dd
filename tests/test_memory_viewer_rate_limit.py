"""Tests for the dashboard's rate limits.

`/api/mcp` writes a command Jarvis later spawns, so it is a code-execution
path. The session token gates it; these limits bound how fast that token can
be guessed and how fast the endpoint can be driven.
"""

from __future__ import annotations

import pytest

try:
    import flask  # noqa: F401

    _HAS_FLASK = True
except ImportError:
    _HAS_FLASK = False

pytestmark = [pytest.mark.unit,
              pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")]


@pytest.fixture
def viewer(tmp_path, monkeypatch):
    from src.desktop_app import memory_viewer

    config_path = tmp_path / "config.json"
    config_path.write_text("{}")
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(config_path))
    memory_viewer.app.config["TESTING"] = True
    memory_viewer._reset_rate_limits()
    # GET /api/mcp runs a live discovery pass, which spawns every configured
    # server. These tests write dozens, and none of them is about discovery.
    monkeypatch.setattr(
        "jarvis.tools.registry.discover_mcp_tools", lambda servers: ({}, {}),
    )
    yield memory_viewer
    memory_viewer._reset_rate_limits()


@pytest.fixture
def client(viewer):
    c = viewer.app.test_client()
    c.environ_base["HTTP_X_DASHBOARD_TOKEN"] = viewer._SESSION_TOKEN
    return c


class TestGuessingTheTokenIsSlowedDown:
    """The token is the only thing between a local process and the diary."""

    def test_repeated_bad_tokens_are_eventually_locked_out(self, viewer):
        c = viewer.app.test_client()
        c.environ_base["HTTP_X_DASHBOARD_TOKEN"] = "wrong"

        statuses = [c.get("/api/stats").status_code
                    for _ in range(viewer.MAX_AUTH_FAILURES + 5)]

        assert 401 in statuses, "expected the first attempts to be plain refusals"
        assert statuses[-1] == 429, "guessing was never rate limited"

    def test_lockout_survives_a_changed_guess(self, viewer):
        """Rotating the guess is the whole point of a brute force."""
        c = viewer.app.test_client()

        for i in range(viewer.MAX_AUTH_FAILURES + 5):
            c.environ_base["HTTP_X_DASHBOARD_TOKEN"] = f"guess-{i}"
            last = c.get("/api/stats").status_code

        assert last == 429

    def test_the_real_token_still_works_before_any_failures(self, client):
        assert client.get("/api/stats").status_code != 429


class TestCodeExecutionPathsAreLimited:
    """Writing an MCP server registers a command Jarvis will spawn."""

    def test_rapid_mcp_writes_are_throttled(self, client, viewer):
        statuses = []
        for i in range(viewer.MAX_WRITES_PER_WINDOW + 5):
            resp = client.post("/api/mcp", json={
                "name": f"srv{i}", "command": "npx", "args": ["-y", "pkg@1.0.0"],
            })
            statuses.append(resp.status_code)

        assert 200 in statuses, "expected the first writes to succeed"
        assert statuses[-1] == 429, "unbounded MCP writes"

    def test_catalogue_adds_share_the_write_budget(self, client, viewer):
        """Otherwise the limit is bypassed by using the other endpoint."""
        for i in range(viewer.MAX_WRITES_PER_WINDOW):
            client.post("/api/mcp", json={
                "name": f"srv{i}", "command": "npx", "args": ["-y", "pkg@1.0.0"],
            })

        resp = client.post("/api/mcp/catalogue/github")

        assert resp.status_code == 429

    def test_reads_are_not_throttled_by_the_write_budget(self, client, viewer):
        """Polling the dashboard must not lock the user out of their own diary."""
        for i in range(viewer.MAX_WRITES_PER_WINDOW + 2):
            client.post("/api/mcp", json={
                "name": f"srv{i}", "command": "npx", "args": ["-y", "pkg@1.0.0"],
            })

        assert client.get("/api/mcp").status_code == 200


class TestTheLimitIsTimeBased:

    def test_a_lockout_never_reaches_the_real_user(self, viewer):
        """The bucket is dashboard-wide, so if a valid token were charged
        against it, anyone able to send bad tokens could lock the owner out
        of their own diary. A correct token skips the bucket entirely."""
        guesser = viewer.app.test_client()
        guesser.environ_base["HTTP_X_DASHBOARD_TOKEN"] = "wrong"
        for _ in range(viewer.MAX_AUTH_FAILURES + 20):
            guesser.get("/api/stats")

        assert guesser.get("/api/stats").status_code == 429

        owner = viewer.app.test_client()
        owner.environ_base["HTTP_X_DASHBOARD_TOKEN"] = viewer._SESSION_TOKEN
        assert owner.get("/api/mcp").status_code == 200

    def test_the_refusal_ages_out_for_the_guesser_too(self, viewer, monkeypatch):
        """Otherwise a burst of typos would bar that client for good."""
        now = [1000.0]
        monkeypatch.setattr(viewer, "_now", lambda: now[0])

        c = viewer.app.test_client()
        c.environ_base["HTTP_X_DASHBOARD_TOKEN"] = "wrong"
        for _ in range(viewer.MAX_AUTH_FAILURES + 5):
            c.get("/api/stats")
        assert c.get("/api/stats").status_code == 429

        now[0] += viewer.RATE_LIMIT_WINDOW_SEC + 1

        assert c.get("/api/stats").status_code == 401

    def test_the_window_reopens_once_it_has_passed(self, client, viewer, monkeypatch):
        now = [1000.0]
        # The module's own clock seam, not time.monotonic itself: patching
        # that through the module attribute swaps it process-wide.
        monkeypatch.setattr(viewer, "_now", lambda: now[0])

        for i in range(viewer.MAX_WRITES_PER_WINDOW + 2):
            client.post("/api/mcp", json={
                "name": f"srv{i}", "command": "npx", "args": ["-y", "pkg@1.0.0"],
            })
        assert client.post("/api/mcp", json={
            "name": "blocked", "command": "npx", "args": ["-y", "pkg@1.0.0"],
        }).status_code == 429

        now[0] += viewer.RATE_LIMIT_WINDOW_SEC + 1

        assert client.post("/api/mcp", json={
            "name": "later", "command": "npx", "args": ["-y", "pkg@1.0.0"],
        }).status_code == 200


class TestTheLandingPageIsNotAFreeOracle:
    """`/` is exempt from the token gate so the launch URL can hand the token
    over, but it compares the token itself and answers 200 or 401. Left
    uncounted, it answers an unlimited number of guesses."""

    def test_guessing_against_the_landing_page_is_rate_limited(self, viewer):
        c = viewer.app.test_client()

        statuses = [c.get(f"/?token=guess-{i}").status_code
                    for i in range(viewer.MAX_AUTH_FAILURES + 5)]

        assert 401 in statuses, "expected the first guesses to be plain refusals"
        assert statuses[-1] == 429, "the landing page answered guesses forever"

    def test_the_launch_url_still_works(self, viewer):
        c = viewer.app.test_client()

        assert c.get(f"/?token={viewer._SESSION_TOKEN}").status_code == 200


class TestAStaleTabDoesNotLockTheOwnerOut:
    """The dashboard polls itself twice every five seconds. After a restart
    those polls carry a dead cookie, so a forgotten tab generates failures
    faster than a person ever would."""

    def test_the_budget_exceeds_the_dashboards_own_poll_rate(self, viewer):
        # Two setInterval(…, 5000) loops on the page: 24 requests a minute.
        polls_per_window = 2 * (60 / 5) * (viewer.RATE_LIMIT_WINDOW_SEC / 60)
        assert viewer.MAX_AUTH_FAILURES > polls_per_window, (
            "one stale tab can saturate the auth budget on its own"
        )

    def test_a_valid_token_is_served_while_a_stale_tab_hammers(self, viewer):
        stale = viewer.app.test_client()
        stale.environ_base["HTTP_X_DASHBOARD_TOKEN"] = "expired-cookie"
        for _ in range(viewer.MAX_AUTH_FAILURES + 10):
            stale.get("/api/stats")

        owner = viewer.app.test_client()
        owner.environ_base["HTTP_X_DASHBOARD_TOKEN"] = viewer._SESSION_TOKEN

        assert owner.get("/api/mcp").status_code == 200


class TestTheBucketStaysBounded:
    """A refused request must not re-arm the window it was refused by, or a
    flood keeps itself locked out forever and the list grows without limit
    under the lock every other request contends for."""

    def test_sustained_guessing_does_not_grow_the_bucket(self, viewer):
        c = viewer.app.test_client()
        c.environ_base["HTTP_X_DASHBOARD_TOKEN"] = "wrong"
        for _ in range(viewer.MAX_AUTH_FAILURES * 5):
            c.get("/api/stats")

        recorded = len(viewer._rate_events.get("auth", []))
        assert recorded <= viewer.MAX_AUTH_FAILURES, (
            f"bucket holds {recorded} events for a {viewer.MAX_AUTH_FAILURES} limit"
        )

    def test_the_lockout_lifts_even_while_traffic_continues(self, viewer, monkeypatch):
        """The flood does not get to extend its own punishment indefinitely."""
        now = [1000.0]
        monkeypatch.setattr(viewer, "_now", lambda: now[0])

        c = viewer.app.test_client()
        c.environ_base["HTTP_X_DASHBOARD_TOKEN"] = "wrong"
        for _ in range(viewer.MAX_AUTH_FAILURES + 5):
            c.get("/api/stats")
        assert c.get("/api/stats").status_code == 429

        # Traffic keeps arriving right up to the moment the window passes.
        for _ in range(10):
            now[0] += viewer.RATE_LIMIT_WINDOW_SEC / 10
            c.get("/api/stats")
        now[0] += viewer.RATE_LIMIT_WINDOW_SEC + 1

        assert c.get("/api/stats").status_code == 401

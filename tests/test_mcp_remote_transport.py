"""Behavioural tests for reaching MCP servers over the network.

Only stdio existed before, which locked out every hosted MCP server.
Adding a network transport also adds the failure modes a local
subprocess never had: a URL is attacker-influenceable in a way a
command path is not, and a bearer token in flight can be read by
anything on the path.
"""

import pytest

from jarvis.tools.external.mcp_client import (
    MCPClient,
    RemoteEndpointError,
    _validate_remote_url,
    is_remote_transport,
)


def _cfg(**kw):
    base = {"transport": "http", "url": "https://mcp.example.com/mcp"}
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Which transports count as remote
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "transport", ["http", "https", "streamable_http", "streamable-http", "HTTP"]
)
def test_the_spellings_people_actually_write_are_accepted(transport):
    assert is_remote_transport(transport) is True


@pytest.mark.parametrize("transport", ["stdio", "", None, "sse"])
def test_stdio_and_unknown_transports_are_not_remote(transport):
    assert is_remote_transport(transport) is False


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

def test_an_https_url_is_accepted():
    _validate_remote_url("srv", "https://mcp.example.com/mcp")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:3000/mcp",
        "http://127.0.0.1:3000/mcp",
        "http://[::1]:3000/mcp",
    ],
)
def test_plain_http_is_allowed_only_to_the_local_machine(url):
    """A loopback hop cannot be sniffed, so TLS buys nothing there."""
    _validate_remote_url("srv", url)


def test_plain_http_to_a_remote_host_is_refused():
    """The bearer token would cross the network in clear text."""
    with pytest.raises(RemoteEndpointError) as excinfo:
        _validate_remote_url("srv", "http://mcp.example.com/mcp")
    assert "https" in str(excinfo.value).lower()


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "ftp://example.com/x", "gopher://x", "javascript:alert(1)"],
)
def test_non_http_schemes_are_refused(url):
    with pytest.raises(RemoteEndpointError):
        _validate_remote_url("srv", url)


def test_a_url_without_a_host_is_refused():
    with pytest.raises(RemoteEndpointError):
        _validate_remote_url("srv", "https:///mcp")


def test_a_missing_url_is_refused_by_name():
    with pytest.raises(RemoteEndpointError) as excinfo:
        _validate_remote_url("my-server", "")
    assert "my-server" in str(excinfo.value)


def test_credentials_embedded_in_the_url_are_refused():
    """They end up in logs and in the Referer of anything the server fetches."""
    with pytest.raises(RemoteEndpointError):
        _validate_remote_url("srv", "https://user:pw@mcp.example.com/mcp")


# ---------------------------------------------------------------------------
# The supply-chain guard has nothing to say about a URL
# ---------------------------------------------------------------------------

def test_a_remote_server_is_not_asked_to_pin_a_package():
    """There is no registry fetch in an HTTP connection, so nothing to pin."""
    from jarvis.tools.external.mcp_supply_chain import validate_server_launch

    validate_server_launch("srv", _cfg())


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_a_remote_server_no_longer_raises_not_implemented(monkeypatch):
    """The whole point: hosted servers were unreachable by construction."""
    captured = {}

    def fake_client(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(
        "jarvis.tools.external.mcp_client.streamablehttp_client", fake_client
    )

    client = MCPClient({"srv": _cfg()})
    client._connect(client.server_configs["srv"], "srv")

    assert captured["url"] == "https://mcp.example.com/mcp"


def test_configured_headers_reach_the_server(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "jarvis.tools.external.mcp_client.streamablehttp_client",
        lambda url, **kw: captured.update(kw) or object(),
    )

    client = MCPClient({"srv": _cfg(headers={"X-Tenant": "acme"})})
    client._connect(client.server_configs["srv"], "srv")

    assert captured["headers"]["X-Tenant"] == "acme"


def test_a_bearer_token_is_read_from_the_credential_store(monkeypatch):
    """A hosted server's token belongs in the keychain, not config.json."""
    from jarvis.utils import secret_store

    monkeypatch.setattr(
        secret_store, "get_secret", lambda name, use_cache=True: (
            "tok-123" if name == "mcp_token:srv" else None
        )
    )
    captured = {}
    monkeypatch.setattr(
        "jarvis.tools.external.mcp_client.streamablehttp_client",
        lambda url, **kw: captured.update(kw) or object(),
    )

    client = MCPClient({"srv": _cfg()})
    client._connect(client.server_configs["srv"], "srv")

    assert captured["headers"]["Authorization"] == "Bearer tok-123"


def test_no_authorization_header_when_no_token_is_stored(monkeypatch):
    from jarvis.utils import secret_store

    monkeypatch.setattr(secret_store, "get_secret", lambda name, use_cache=True: None)
    captured = {}
    monkeypatch.setattr(
        "jarvis.tools.external.mcp_client.streamablehttp_client",
        lambda url, **kw: captured.update(kw) or object(),
    )

    client = MCPClient({"srv": _cfg()})
    client._connect(client.server_configs["srv"], "srv")

    assert "Authorization" not in (captured.get("headers") or {})


def test_a_config_header_does_not_override_the_stored_token(monkeypatch):
    """Otherwise a stale key in config.json silently beats the keychain."""
    from jarvis.utils import secret_store

    monkeypatch.setattr(
        secret_store, "get_secret", lambda name, use_cache=True: "tok-fresh"
    )
    captured = {}
    monkeypatch.setattr(
        "jarvis.tools.external.mcp_client.streamablehttp_client",
        lambda url, **kw: captured.update(kw) or object(),
    )

    client = MCPClient({"srv": _cfg(headers={"Authorization": "Bearer tok-stale"})})
    client._connect(client.server_configs["srv"], "srv")

    assert captured["headers"]["Authorization"] == "Bearer tok-fresh"


def test_a_bad_url_is_refused_before_any_connection_is_made(monkeypatch):
    calls = {"n": 0}

    def fake_client(url, **kw):
        calls["n"] += 1
        return object()

    monkeypatch.setattr(
        "jarvis.tools.external.mcp_client.streamablehttp_client", fake_client
    )

    client = MCPClient({"srv": _cfg(url="http://mcp.example.com/mcp")})
    with pytest.raises(RemoteEndpointError):
        client._connect(client.server_configs["srv"], "srv")

    assert calls["n"] == 0


def test_stdio_still_works_through_the_same_entry_point(monkeypatch):
    spawned = {"n": 0}
    monkeypatch.setattr(
        "jarvis.tools.external.mcp_client._resolve_command", lambda c: c
    )
    monkeypatch.setattr(
        "jarvis.tools.external.mcp_client.stdio_client",
        lambda params, **kw: spawned.update(n=spawned["n"] + 1) or None,
    )

    client = MCPClient(
        {"srv": {"transport": "stdio", "command": "node", "args": ["s.js"]}}
    )
    client._connect(client.server_configs["srv"], "srv")

    assert spawned["n"] == 1


def test_an_unknown_transport_still_says_so_clearly():
    client = MCPClient({"srv": {"transport": "carrier-pigeon"}})
    with pytest.raises(NotImplementedError) as excinfo:
        client._connect(client.server_configs["srv"], "srv")
    assert "carrier-pigeon" in str(excinfo.value)

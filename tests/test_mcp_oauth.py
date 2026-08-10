"""Behavioural tests for the OAuth flow behind a Connect button.

The point is that a user authorises their own account in their own
browser and never sees a token. So the token must not touch config.json,
the callback listener must not be reachable from off the machine, and a
code that arrives for a different flow must not be accepted.
"""

import json

import pytest

from jarvis.tools.external import mcp_oauth
from jarvis.utils import secret_store


@pytest.fixture
def fake_keychain(monkeypatch):
    store = {}
    monkeypatch.setattr(
        secret_store, "get_secret", lambda name, use_cache=True: store.get(name)
    )
    monkeypatch.setattr(
        secret_store,
        "set_secret",
        lambda name, value: store.__setitem__(name, value) or True,
    )
    monkeypatch.setattr(
        secret_store, "delete_secret", lambda name: store.pop(name, None) or True
    )
    return store


# ---------------------------------------------------------------------------
# Token storage
# ---------------------------------------------------------------------------

def test_tokens_round_trip_through_the_credential_store(fake_keychain):
    import asyncio

    from mcp.shared.auth import OAuthToken

    storage = mcp_oauth.KeychainTokenStorage("acme-crm")
    token = OAuthToken(access_token="at-123", token_type="Bearer", refresh_token="rt-9")

    asyncio.run(storage.set_tokens(token))
    restored = asyncio.run(storage.get_tokens())

    assert restored is not None
    assert restored.access_token == "at-123"
    assert restored.refresh_token == "rt-9"


def test_a_token_lands_in_the_keychain_not_the_config(fake_keychain):
    import asyncio

    from mcp.shared.auth import OAuthToken

    storage = mcp_oauth.KeychainTokenStorage("acme-crm")
    asyncio.run(storage.set_tokens(OAuthToken(access_token="at-123", token_type="Bearer")))

    assert any("acme-crm" in key for key in fake_keychain), fake_keychain
    assert "at-123" in json.dumps(fake_keychain)


def test_two_servers_do_not_share_a_token(fake_keychain):
    import asyncio

    from mcp.shared.auth import OAuthToken

    asyncio.run(
        mcp_oauth.KeychainTokenStorage("alpha").set_tokens(
            OAuthToken(access_token="alpha-token", token_type="Bearer")
        )
    )
    beta = asyncio.run(mcp_oauth.KeychainTokenStorage("beta").get_tokens())
    assert beta is None


def test_no_stored_token_reads_as_none(fake_keychain):
    import asyncio

    assert asyncio.run(mcp_oauth.KeychainTokenStorage("nothing").get_tokens()) is None


def test_a_corrupt_stored_token_does_not_raise(fake_keychain):
    import asyncio

    fake_keychain[mcp_oauth._token_key("broken")] = "{not json"
    assert asyncio.run(mcp_oauth.KeychainTokenStorage("broken").get_tokens()) is None


def test_client_registration_round_trips(fake_keychain):
    import asyncio

    from mcp.shared.auth import OAuthClientInformationFull

    storage = mcp_oauth.KeychainTokenStorage("acme-crm")
    info = OAuthClientInformationFull(
        client_id="cid-1",
        client_secret="csec",
        redirect_uris=["http://127.0.0.1:1234/callback"],
    )
    asyncio.run(storage.set_client_info(info))
    restored = asyncio.run(storage.get_client_info())

    assert restored is not None
    assert restored.client_id == "cid-1"


# ---------------------------------------------------------------------------
# The loopback listener
# ---------------------------------------------------------------------------

def test_the_callback_listener_binds_to_loopback_only():
    """Anything else would let the network race the browser for the code."""
    listener = mcp_oauth.LoopbackCallback()
    try:
        assert listener.host == "127.0.0.1"
        assert listener.redirect_uri.startswith("http://127.0.0.1:")
        assert listener.port > 0
    finally:
        listener.close()


def test_the_listener_hands_back_the_code_and_state():
    import threading
    import urllib.request

    listener = mcp_oauth.LoopbackCallback()
    try:
        result = {}

        def wait():
            result["value"] = listener.wait_for_code(timeout=10)

        waiter = threading.Thread(target=wait)
        waiter.start()

        urllib.request.urlopen(
            f"{listener.redirect_uri}?code=the-code&state=the-state", timeout=10
        ).read()
        waiter.join(timeout=10)

        assert result["value"] == ("the-code", "the-state")
    finally:
        listener.close()


def test_an_authorisation_error_is_reported_rather_than_hanging():
    import threading
    import urllib.error
    import urllib.request

    listener = mcp_oauth.LoopbackCallback()
    try:
        captured = {}

        def wait():
            try:
                listener.wait_for_code(timeout=10)
            except Exception as e:  # noqa: BLE001
                captured["error"] = str(e)

        waiter = threading.Thread(target=wait)
        waiter.start()
        try:
            urllib.request.urlopen(
                f"{listener.redirect_uri}?error=access_denied", timeout=10
            ).read()
        except urllib.error.HTTPError:
            pass
        waiter.join(timeout=10)

        assert "access_denied" in captured.get("error", "")
    finally:
        listener.close()


def test_the_browser_page_does_not_echo_the_code_back():
    """The success page is rendered by whatever the user has open."""
    import threading
    import urllib.request

    listener = mcp_oauth.LoopbackCallback()
    try:
        threading.Thread(
            target=lambda: listener.wait_for_code(timeout=10), daemon=True
        ).start()
        body = urllib.request.urlopen(
            f"{listener.redirect_uri}?code=secret-code&state=s", timeout=10
        ).read().decode()
        assert "secret-code" not in body
    finally:
        listener.close()


# ---------------------------------------------------------------------------
# Wiring into the transport
# ---------------------------------------------------------------------------

def test_a_server_without_oauth_gets_no_auth_provider(monkeypatch):
    from jarvis.tools.external.mcp_client import MCPClient

    captured = {}
    monkeypatch.setattr(
        "jarvis.tools.external.mcp_client.streamablehttp_client",
        lambda url, **kw: captured.update(kw) or object(),
    )
    monkeypatch.setattr(secret_store, "get_secret", lambda n, use_cache=True: None)

    cfg = {"transport": "http", "url": "https://mcp.example.com/mcp"}
    MCPClient({"srv": cfg})._connect(cfg, "srv")

    assert captured.get("auth") is None


def test_an_oauth_server_gets_a_provider(monkeypatch):
    from jarvis.tools.external.mcp_client import MCPClient

    captured = {}
    monkeypatch.setattr(
        "jarvis.tools.external.mcp_client.streamablehttp_client",
        lambda url, **kw: captured.update(kw) or object(),
    )
    monkeypatch.setattr(secret_store, "get_secret", lambda n, use_cache=True: None)

    cfg = {"transport": "http", "url": "https://mcp.example.com/mcp", "auth": "oauth"}
    MCPClient({"srv": cfg})._connect(cfg, "srv")

    assert captured.get("auth") is not None


def test_oauth_does_not_also_send_a_bearer_header(monkeypatch):
    """The provider owns the Authorization header; two would conflict."""
    from jarvis.tools.external.mcp_client import MCPClient

    captured = {}
    monkeypatch.setattr(
        "jarvis.tools.external.mcp_client.streamablehttp_client",
        lambda url, **kw: captured.update(kw) or object(),
    )
    monkeypatch.setattr(
        secret_store, "get_secret", lambda n, use_cache=True: "static-token"
    )

    cfg = {"transport": "http", "url": "https://mcp.example.com/mcp", "auth": "oauth"}
    MCPClient({"srv": cfg})._connect(cfg, "srv")

    assert "Authorization" not in (captured.get("headers") or {})


# ── RFC 9207: authorisation server issuer identification ─────────────────


class TestTheIssuerIsCaptured:
    """The SDK's callback contract returns only (code, state), so the `iss`
    parameter has to be read here or it is lost."""

    def _redirect(self, listener, query):
        import threading
        import urllib.request

        result = {}

        def wait():
            try:
                result["value"] = listener.wait_for_code(timeout=10)
            except Exception as e:  # noqa: BLE001
                result["error"] = e

        waiter = threading.Thread(target=wait)
        waiter.start()
        urllib.request.urlopen(f"{listener.redirect_uri}?{query}", timeout=10).read()
        waiter.join(timeout=10)
        return result

    def test_the_issuer_is_read_off_the_redirect(self):
        listener = mcp_oauth.LoopbackCallback()
        try:
            self._redirect(listener, "code=c&state=s&iss=https://as.example.com")
            assert listener.issuer == "https://as.example.com"
        finally:
            listener.close()

    def test_a_redirect_without_an_issuer_reads_as_none(self):
        listener = mcp_oauth.LoopbackCallback()
        try:
            self._redirect(listener, "code=c&state=s")
            assert listener.issuer is None
        finally:
            listener.close()


class TestMixUpAttacksAreRefused:
    """RFC 9207. Without this check a malicious authorisation server can have
    the client send its code to the wrong token endpoint, which hands the
    attacker the code."""

    def test_a_matching_issuer_is_accepted(self):
        mcp_oauth.verify_issuer(
            "srv", received="https://as.example.com",
            expected="https://as.example.com", advertised=True,
        )

    def test_a_different_issuer_is_refused(self):
        with pytest.raises(mcp_oauth.IssuerMismatchError):
            mcp_oauth.verify_issuer(
                "srv", received="https://evil.example.com",
                expected="https://as.example.com", advertised=True,
            )

    def test_comparison_is_exact_not_prefix(self):
        """A sibling host under the same registrable domain is a different
        issuer, and so is a path suffix."""
        for received in ("https://as.example.com.evil.net",
                         "https://as.example.com/../other",
                         "HTTPS://AS.EXAMPLE.COM"):
            with pytest.raises(mcp_oauth.IssuerMismatchError):
                mcp_oauth.verify_issuer(
                    "srv", received=received,
                    expected="https://as.example.com", advertised=True,
                )

    def test_a_missing_issuer_is_refused_when_the_server_promised_one(self):
        """The metadata says every response carries iss, so one without it
        did not come from that server."""
        with pytest.raises(mcp_oauth.IssuerMismatchError):
            mcp_oauth.verify_issuer(
                "srv", received=None,
                expected="https://as.example.com", advertised=True,
            )

    def test_a_missing_issuer_is_allowed_when_the_server_never_promised_one(self):
        """RFC 9207 is opt-in. Refusing here would break every provider that
        has not adopted it."""
        mcp_oauth.verify_issuer(
            "srv", received=None,
            expected="https://as.example.com", advertised=False,
        )

    def test_an_issuer_is_still_checked_even_if_it_was_not_advertised(self):
        """A server that sends iss without advertising it still must not send
        somebody else's."""
        with pytest.raises(mcp_oauth.IssuerMismatchError):
            mcp_oauth.verify_issuer(
                "srv", received="https://evil.example.com",
                expected="https://as.example.com", advertised=False,
            )

    def test_nothing_to_compare_against_does_not_raise(self):
        """Discovery may not have produced metadata. There is no security
        benefit either way, and refusing would break the connection."""
        mcp_oauth.verify_issuer(
            "srv", received="https://as.example.com", expected=None, advertised=False,
        )

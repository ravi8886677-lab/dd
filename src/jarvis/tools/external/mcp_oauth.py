"""OAuth 2.1 for hosted MCP servers.

A hosted server needs the user's authorisation, and the alternative to
this is what the dashboard does today: ask them to find an API key in
some other product's settings and paste it into a text box. That is the
step people abandon, and it leaves a long-lived credential sitting in a
config file.

Here the user clicks Connect, their own browser opens at their own
provider, they approve, and a token arrives. The token goes into the OS
credential store; nothing is shown to the user and nothing is written to
`config.json`.

The redirect comes back to a listener on 127.0.0.1 bound to an ephemeral
port. That is the desktop OAuth pattern: there is no server to host a
redirect on, and loopback means the code cannot be intercepted by
anything off the machine. PKCE is handled by the SDK's provider, which
also verifies the `state` this module hands back.
"""

from __future__ import annotations

import json
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import OAuthClientProvider, TokenStorage  # type: ignore
from mcp.shared.auth import (  # type: ignore
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

from ...debug import debug_log
from ...utils import secret_store

# How long to wait for someone to finish authorising in the browser.
_CALLBACK_TIMEOUT_SEC = 300.0

_CALLBACK_PATH = "/oauth/callback"


def _token_key(server_name: str) -> str:
    return f"mcp_oauth_token:{server_name}"


def _client_key(server_name: str) -> str:
    return f"mcp_oauth_client:{server_name}"


class KeychainTokenStorage(TokenStorage):
    """Stores OAuth tokens and client registrations in the OS credential store.

    The SDK ships no persistent storage, so without this every restart
    would send the user back through the browser. Keeping tokens here
    rather than in `config.json` matters more than for an API key: a
    refresh token is a standing grant on the user's account, and MCP
    server subprocesses can read that file.
    """

    def __init__(self, server_name: str) -> None:
        self._server_name = server_name

    async def get_tokens(self) -> Optional[OAuthToken]:
        raw = secret_store.get_secret(_token_key(self._server_name))
        if not raw:
            return None
        try:
            return OAuthToken.model_validate(json.loads(raw))
        except Exception as e:  # noqa: BLE001
            # A damaged entry means the next call re-authorises rather
            # than the daemon failing to start.
            debug_log(
                f"stored OAuth token for '{self._server_name}' is unreadable "
                f"({e}); re-authorisation will be needed",
                "mcp",
            )
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        secret_store.set_secret(
            _token_key(self._server_name), tokens.model_dump_json(exclude_none=True)
        )

    async def get_client_info(self) -> Optional[OAuthClientInformationFull]:
        raw = secret_store.get_secret(_client_key(self._server_name))
        if not raw:
            return None
        try:
            return OAuthClientInformationFull.model_validate(json.loads(raw))
        except Exception as e:  # noqa: BLE001
            debug_log(
                f"stored OAuth client registration for '{self._server_name}' is "
                f"unreadable ({e}); it will be registered again",
                "mcp",
            )
            return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        secret_store.set_secret(
            _client_key(self._server_name),
            client_info.model_dump_json(exclude_none=True),
        )


class IssuerMismatchError(RuntimeError):
    """The authorisation response did not come from the expected issuer."""


def verify_issuer(
    server_name: str,
    *,
    received: Optional[str],
    expected: Optional[str],
    advertised: bool,
) -> None:
    """Check the RFC 9207 ``iss`` parameter on an authorisation response.

    Without this, a client that talks to more than one authorisation server
    can be induced to send its code to the wrong token endpoint, handing the
    attacker a usable code. The SDK checks ``state`` and PKCE but not this,
    and its callback contract returns only ``(code, state)``, so the check
    has to happen on our side of the redirect.

    Args:
        received: ``iss`` as it arrived, or ``None`` if absent.
        expected: the issuer from discovered metadata, or ``None`` if we
            never got any.
        advertised: whether that metadata set
            ``authorization_response_iss_parameter_supported``.

    Raises:
        IssuerMismatchError: the response is not from the expected issuer.
    """
    if expected is None:
        # Nothing to compare against. Refusing would break the connection
        # without buying anything, since we cannot tell right from wrong.
        debug_log(f"no issuer metadata for '{server_name}'; iss unverified", "mcp")
        return

    if received is None:
        if advertised:
            # The server's own metadata promises iss on every response, so a
            # response without one did not come from it.
            raise IssuerMismatchError(
                f"'{server_name}': the authorisation server advertises RFC 9207 "
                "but the response carried no 'iss'."
            )
        debug_log(f"'{server_name}' does not send iss (RFC 9207 not adopted)", "mcp")
        return

    # RFC 9207 §2.4: compare as a simple case-sensitive string. Anything
    # looser lets a lookalike host or a path suffix through.
    if received != expected:
        raise IssuerMismatchError(
            f"'{server_name}': authorisation response came from '{received}' "
            f"but '{expected}' was expected."
        )


class _CallbackHandler(BaseHTTPRequestHandler):
    """Answers exactly one redirect and records what it carried."""

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        error = (params.get("error") or [None])[0]
        code = (params.get("code") or [None])[0]
        state = (params.get("state") or [None])[0]
        # RFC 9207. Recorded separately because the SDK's callback contract
        # has no room for it.
        self.server.oauth_issuer = (params.get("iss") or [None])[0]  # type: ignore[attr-defined]

        self.server.oauth_result = (code, state, error)  # type: ignore[attr-defined]
        self.server.oauth_done.set()  # type: ignore[attr-defined]

        if error:
            body = _page("Authorisation refused", f"The provider reported: {error}")
            self.send_response(400)
        elif code:
            body = _page("Connected", "You can close this tab and go back to Jarvis.")
            self.send_response(200)
        else:
            body = _page("Nothing to do", "That link carried no authorisation code.")
            self.send_response(400)

        encoded = body.encode("utf-8")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args) -> None:
        """Silence the default stderr access log.

        It would print the full request line, and that line contains the
        authorisation code.
        """


def _page(title: str, message: str) -> str:
    """The page the user is left looking at.

    Deliberately free of the code or any other parameter: this is
    rendered by whatever browser they had open, and its URL is already
    in their history.
    """
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title></head>"
        "<body style=\"font-family:system-ui,sans-serif;background:#040a14;"
        "color:#eefaff;display:flex;align-items:center;justify-content:center;"
        "height:100vh;margin:0\">"
        f"<div style='text-align:center'><h1>{title}</h1><p>{message}</p></div>"
        "</body></html>"
    )


class LoopbackCallback:
    """A one-shot HTTP listener on 127.0.0.1 for the OAuth redirect."""

    def __init__(self) -> None:
        self.host = "127.0.0.1"
        self._server = HTTPServer((self.host, 0), _CallbackHandler)
        self.port = self._server.server_address[1]
        self._server.oauth_result = (None, None, None)  # type: ignore[attr-defined]
        self._server.oauth_issuer = None  # type: ignore[attr-defined]
        self._server.oauth_done = threading.Event()  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True,
            name="JarvisOAuthCallback",
        )
        self._thread.start()

    @property
    def redirect_uri(self) -> str:
        return f"http://{self.host}:{self.port}{_CALLBACK_PATH}"

    @property
    def issuer(self) -> Optional[str]:
        """The RFC 9207 ``iss`` from the redirect, or ``None`` if absent."""
        return self._server.oauth_issuer  # type: ignore[attr-defined]

    def wait_for_code(
        self, timeout: float = _CALLBACK_TIMEOUT_SEC
    ) -> Tuple[str, Optional[str]]:
        """Block until the browser comes back, or give up.

        Returns ``(code, state)``. The provider checks ``state`` against
        what it sent, which is what stops a code from another flow being
        accepted here.
        """
        if not self._server.oauth_done.wait(timeout):  # type: ignore[attr-defined]
            raise TimeoutError(
                "Timed out waiting for the browser to finish authorising."
            )
        code, state, error = self._server.oauth_result  # type: ignore[attr-defined]
        if error:
            raise RuntimeError(f"Authorisation failed: {error}")
        if not code:
            raise RuntimeError("The redirect carried no authorisation code.")
        return code, state

    def close(self) -> None:
        try:
            self._server.shutdown()
        except Exception as e:  # noqa: BLE001
            debug_log(f"OAuth callback shutdown error: {e}", "mcp")
        try:
            self._server.server_close()
        except Exception:  # noqa: BLE001
            pass


def build_provider(server_name: str, server_url: str) -> OAuthClientProvider:
    """Build the SDK auth provider for one server.

    The listener is opened here rather than at redirect time because the
    redirect URI has to be registered with the provider before the
    authorisation URL is built, and it must be the exact same URI the
    browser is later sent back to.
    """
    listener = LoopbackCallback()

    metadata = OAuthClientMetadata(
        client_name="Jarvis",
        redirect_uris=[listener.redirect_uri],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )

    async def redirect_handler(authorization_url: str) -> None:
        debug_log(f"opening browser to authorise '{server_name}'", "mcp")
        print(
            f"\n  🔗 Opening your browser to connect '{server_name}'.\n"
            "     Approve it there and come back.\n",
            flush=True,
        )
        if not webbrowser.open(authorization_url):
            # A headless or locked-down desktop has no browser to open.
            print(f"  🌐 Open this to authorise:\n     {authorization_url}\n", flush=True)

    # The provider is built below, but the callback closure needs to read the
    # metadata it discovers at runtime. A one-slot box is the smallest way to
    # close that loop without reaching into the SDK's internals from outside.
    provider_box: list = []

    async def callback_handler() -> Tuple[str, Optional[str]]:
        import asyncio

        try:
            code, state = await asyncio.to_thread(listener.wait_for_code)
            expected, advertised = _discovered_issuer(provider_box)
            verify_issuer(
                server_name,
                received=listener.issuer,
                expected=expected,
                advertised=advertised,
            )
            return code, state
        finally:
            listener.close()

    provider = OAuthClientProvider(
        server_url=server_url,
        client_metadata=metadata,
        storage=KeychainTokenStorage(server_name),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
    provider_box.append(provider)
    return provider


def _discovered_issuer(provider_box: list) -> Tuple[Optional[str], bool]:
    """The issuer the SDK discovered, and whether it advertises RFC 9207.

    Read defensively: this reaches into the SDK's context, and a shape change
    there must degrade to "cannot verify" rather than break authorisation.
    """
    if not provider_box:
        return None, False
    try:
        oauth_metadata = getattr(provider_box[0].context, "oauth_metadata", None)
        if oauth_metadata is None:
            return None, False
        issuer = getattr(oauth_metadata, "issuer", None)
        advertised = bool(
            getattr(oauth_metadata, "authorization_response_iss_parameter_supported", False)
        )
        return (str(issuer) if issuer is not None else None), advertised
    except Exception as e:  # noqa: BLE001
        debug_log(f"could not read issuer metadata: {e}", "mcp")
        return None, False


def uses_oauth(server_cfg: dict) -> bool:
    """Whether this server is configured to authenticate with OAuth."""
    return str(server_cfg.get("auth") or "").strip().lower() in {"oauth", "oauth2"}


def forget(server_name: str) -> None:
    """Drop a server's stored token and registration — a Disconnect button."""
    secret_store.delete_secret(_token_key(server_name))
    secret_store.delete_secret(_client_key(server_name))
    debug_log(f"cleared stored OAuth credentials for '{server_name}'", "mcp")


def is_port_free(port: int) -> bool:
    """Whether a loopback port can still be bound. Used by diagnostics."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0

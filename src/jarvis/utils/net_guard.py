"""Shared SSRF guard for tools that fetch model- or web-supplied URLs.

Jarvis follows URLs it did not get from the user: search results, links on a
page it already read, arguments an MCP tool description talked the model into
using. Any of those can point at the loopback interface, the cloud metadata
endpoint (169.254.169.254), or a machine on the user's LAN that is only
reachable because Jarvis runs inside the trust boundary.

Every outbound fetch of an untrusted URL goes through :func:`guarded_get`,
which validates the target *and* every redirect hop before issuing a request.

Known limitation: the address is resolved for the check and resolved again by
the HTTP client when it connects, so a resolver that returns a different
answer between the two can still slip through (DNS rebinding). Closing that
needs the connection pinned to the validated address, which the ``requests``
API does not expose. The guard raises the cost of an attack substantially; it
is not a complete mitigation on its own.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any, Dict, Optional
from urllib.parse import urljoin, urlparse

import requests

from ..debug import debug_log

# Redirect hops allowed before a fetch is abandoned. Caps both latency and the
# number of times a hostile server can bounce us around hoping one hop slips
# past the guard.
DEFAULT_MAX_REDIRECTS = 3
DEFAULT_TIMEOUT_SEC = 15.0

# Headers that authenticate the caller and must not cross an origin boundary
# on a redirect. Compared lowercase because header names are case-insensitive.
_CREDENTIAL_HEADERS = frozenset({
    "authorization", "cookie", "proxy-authorization",
})


def _origin(url: str) -> tuple:
    """Scheme, host and port — what "same origin" means for credentials."""
    parsed = urlparse(url)
    return (parsed.scheme, (parsed.hostname or "").lower(), parsed.port)


class UnsafeURLError(ValueError):
    """Base for every refusal to fetch. Callers that do not care why a URL was
    rejected can catch this alone."""


class NonPublicAddressError(UnsafeURLError):
    """The target is not on the public internet: a non-http(s) scheme, or an
    address in loopback, private, link-local, reserved or multicast space."""


class UnresolvableHostError(UnsafeURLError):
    """The hostname did not resolve, so it could not be cleared as public.

    Distinct from :class:`NonPublicAddressError` because the causes are
    different in kind: this one is a typo or a DNS outage and may well succeed
    on a retry, whereas a non-public address is refused by policy every time.
    Telling the two apart keeps the tool from advising the model to give up on
    an address that was merely temporarily unresolvable.
    """


class TooManyRedirectsError(UnsafeURLError):
    """The redirect chain outran its cap. Every hop stayed public, so this is
    a loop or a deliberately long chain rather than an attempt to reach inward."""


def _ip_is_public(ip: ipaddress._BaseAddress) -> bool:
    """True only for addresses that are routable on the public internet."""
    # Judge IPv4-mapped IPv6 (``::ffff:127.0.0.1``) by its embedded IPv4
    # address. Current interpreters classify the mapped form correctly on
    # their own; patch releases before the stdlib fix report it as public,
    # which would make the mapping a bypass of every check below.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def check_url(url: str) -> None:
    """Raise if ``url`` is not http(s) pointing at exclusively public addresses.

    Raises:
        NonPublicAddressError: bad scheme, or an address in reserved space.
        UnresolvableHostError: the hostname did not resolve.
    """
    try:
        parsed = urlparse(url)
    except Exception as e:
        raise NonPublicAddressError(f"Unparseable URL: {url}") from e
    if parsed.scheme not in ("http", "https"):
        raise NonPublicAddressError(f"Only http(s) may be fetched, not {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise NonPublicAddressError(f"URL has no host: {url}")

    # A literal IP is judged directly — resolving it would be pointless and
    # would let a resolver override what the URL plainly says.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not _ip_is_public(ip):
            raise NonPublicAddressError(f"{ip} is not a public address")
        return

    # A hostname is only as safe as its worst record: a hostile resolver can
    # answer [1.1.1.1, 127.0.0.1] and clients differ on which they try.
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception as e:
        debug_log(f"net_guard: DNS lookup failed for {host}: {e}", "web")
        raise UnresolvableHostError(f"Could not resolve {host}") from e
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except Exception as e:
            raise NonPublicAddressError(f"Unreadable address for {host}") from e
        if not _ip_is_public(ip):
            debug_log(f"net_guard: refusing {url} — resolves to {ip}", "web")
            raise NonPublicAddressError(f"{host} resolves to non-public {ip}")


def is_public_url(url: str) -> bool:
    """Whether ``url`` is http(s) and every address it resolves to is public."""
    try:
        check_url(url)
    except UnsafeURLError:
        return False
    return True


def guarded_get(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    stream: bool = False,
    **kwargs: Any,
) -> requests.Response:
    """GET ``url``, validating it and every redirect hop against the guard.

    Redirects are walked by hand rather than left to ``requests`` because the
    library resolves the chain internally: by the time it returns, a hop
    through 127.0.0.1 has already been fetched. Returns the final response
    without inspecting its status, so callers keep their own error handling.

    Raises :class:`UnsafeURLError` if the target, or any hop, is not public,
    or if the chain exceeds ``max_redirects``.
    """
    check_url(url)

    # Copied because hops may strip credentials from it, and the caller's
    # dict is theirs.
    sent_headers = dict(headers or {})
    current_url = url
    for _ in range(max_redirects + 1):
        response = requests.get(
            current_url, headers=sent_headers, timeout=timeout,
            allow_redirects=False, stream=stream, **kwargs,
        )
        if not (response.is_redirect or response.is_permanent_redirect):
            return response
        location = response.headers.get("Location", "")
        if not location:
            return response
        next_url = urljoin(current_url, location)
        response.close()
        try:
            check_url(next_url)
        except UnsafeURLError as e:
            debug_log(f"net_guard: refusing redirect to {next_url}: {e}", "web")
            raise
        if _origin(next_url) != _origin(current_url):
            # requests.Session does this in rebuild_auth, and the hand-rolled
            # walk skips that machinery. Following a redirect to another host
            # with the caller's credentials still attached hands them to
            # whoever the origin points at — a public-to-public hop the guard
            # deliberately allows, so nothing else would stop it.
            dropped = [h for h in sent_headers if h.lower() in _CREDENTIAL_HEADERS]
            for header in dropped:
                del sent_headers[header]
            if dropped:
                debug_log(
                    f"net_guard: dropped {', '.join(dropped)} crossing to "
                    f"{_origin(next_url)}", "web",
                )
        debug_log(f"net_guard: following redirect to {next_url}", "web")
        current_url = next_url

    raise TooManyRedirectsError(f"Too many redirects (>{max_redirects}) from {url}")

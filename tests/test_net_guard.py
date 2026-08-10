"""Tests for the shared SSRF guard.

Jarvis fetches URLs that came from a search result, a web page it already
read, or an MCP tool description — none of which the user wrote. The guard is
what stops such a URL pointing at the loopback interface, the cloud metadata
endpoint, or the LAN.
"""

import socket
from unittest.mock import Mock, patch

import pytest

from src.jarvis.utils.net_guard import UnsafeURLError, guarded_get, is_public_url


class TestSchemesAreRestricted:
    """Only http(s) may be fetched."""

    @pytest.mark.parametrize("url", [
        "file:///etc/passwd",
        "ftp://example.com/",
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "gopher://example.com/",
    ])
    def test_non_http_schemes_rejected(self, url):
        assert is_public_url(url) is False

    def test_https_allowed(self):
        assert is_public_url("https://1.1.1.1/") is True


class TestPrivateAddressSpaceIsRejected:
    """Literal IPs in every non-public range must be refused."""

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1/",            # loopback
        "http://127.1.2.3/",            # the whole 127/8 block
        "http://[::1]/",                # IPv6 loopback
        "http://10.0.0.1/",             # private
        "http://192.168.1.1/",          # private
        "http://172.16.0.1/",           # private
        "http://169.254.169.254/",      # link-local, cloud metadata
        "http://[fe80::1]/",            # IPv6 link-local
        "http://0.0.0.0/",              # unspecified
        "http://224.0.0.1/",            # multicast
        "http://240.0.0.1/",            # reserved
    ])
    def test_non_public_literals_rejected(self, url):
        assert is_public_url(url) is False

    def test_public_literal_allowed(self):
        assert is_public_url("http://8.8.8.8/") is True

    @pytest.mark.parametrize("url", [
        "http://[::ffff:127.0.0.1]/",   # loopback wearing an IPv6 mapping
        "http://[::ffff:10.0.0.1]/",    # private, same trick
        "http://[::ffff:169.254.169.254]/",
    ])
    def test_ipv4_mapped_ipv6_cannot_smuggle_a_private_address(self, url):
        """The mapping must be unwrapped, not judged as an opaque IPv6 address."""
        assert is_public_url(url) is False


class TestHostnamesAreResolvedBeforeTheVerdict:
    """A hostname is only as safe as what it resolves to."""

    def _addrinfo(self, *addrs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0)) for a in addrs]

    def test_hostname_resolving_to_loopback_rejected(self):
        with patch("socket.getaddrinfo", return_value=self._addrinfo("127.0.0.1")):
            assert is_public_url("http://evil.example/") is False

    def test_rejected_when_any_record_is_private(self):
        """A hostile resolver can mix a public and a private record."""
        with patch("socket.getaddrinfo",
                   return_value=self._addrinfo("1.1.1.1", "127.0.0.1")):
            assert is_public_url("http://evil.example/") is False

    def test_hostname_resolving_to_public_allowed(self):
        with patch("socket.getaddrinfo", return_value=self._addrinfo("1.1.1.1")):
            assert is_public_url("http://good.example/") is True

    def test_unresolvable_hostname_rejected(self):
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("nope")):
            assert is_public_url("http://nx.example/") is False

    def test_url_without_host_rejected(self):
        assert is_public_url("http://") is False


class TestGuardedGetRefusesBeforeTouchingTheNetwork:
    """The point of the guard is that the blocked request is never issued."""

    def test_blocked_url_raises_and_never_calls_requests(self):
        with patch("requests.get") as mock_get:
            with pytest.raises(UnsafeURLError):
                guarded_get("http://169.254.169.254/latest/meta-data/")
            mock_get.assert_not_called()


class TestRedirectsAreRevalidated:
    """A public URL that redirects inward is the interesting attack."""

    def _redirect(self, location):
        resp = Mock(is_redirect=True, is_permanent_redirect=False,
                    headers={"Location": location})
        resp.close = Mock()
        return resp

    def _ok(self):
        return Mock(is_redirect=False, is_permanent_redirect=False,
                    headers={}, status_code=200, raise_for_status=Mock())

    def test_redirect_into_private_space_is_refused(self):
        with patch("requests.get", return_value=self._redirect("http://127.0.0.1/admin")):
            with pytest.raises(UnsafeURLError):
                guarded_get("https://8.8.8.8/start")

    def test_redirect_to_public_is_followed(self):
        responses = [self._redirect("https://1.1.1.1/next"), self._ok()]
        with patch("requests.get", side_effect=responses) as mock_get:
            result = guarded_get("https://8.8.8.8/start")
            assert result is responses[-1]
            assert mock_get.call_count == 2

    def test_redirect_chain_is_capped(self):
        """An endless public redirect loop must terminate, not hang."""
        with patch("requests.get", return_value=self._redirect("https://1.1.1.1/loop")):
            with pytest.raises(UnsafeURLError):
                guarded_get("https://8.8.8.8/start", max_redirects=2)

    def test_relative_redirect_resolved_against_current_url(self):
        responses = [self._redirect("/next"), self._ok()]
        with patch("requests.get", side_effect=responses) as mock_get:
            guarded_get("https://8.8.8.8/start")
            assert mock_get.call_args_list[1][0][0] == "https://8.8.8.8/next"


class TestCredentialsDoNotFollowARedirectAcrossOrigins:
    """`requests` strips Authorization in `rebuild_auth` when a redirect
    changes host. The hand-rolled walk has to do the same, or the first
    caller that fetches an authenticated untrusted URL hands its token to
    whatever public host the origin points at."""

    def _redirect(self, location):
        return Mock(is_redirect=True, is_permanent_redirect=False,
                    headers={"Location": location}, close=Mock())

    def _ok(self):
        return Mock(is_redirect=False, is_permanent_redirect=False,
                    headers={}, status_code=200, raise_for_status=Mock())

    def test_authorization_is_dropped_when_the_host_changes(self):
        responses = [self._redirect("https://1.1.1.1/next"), self._ok()]
        headers = {"Authorization": "Bearer sekrit", "Accept": "text/html"}
        with patch("requests.get", side_effect=responses) as mock_get:
            guarded_get("https://8.8.8.8/start", headers=headers)

        sent = mock_get.call_args_list[1].kwargs["headers"]
        assert "Authorization" not in sent
        assert sent["Accept"] == "text/html", "unrelated headers must survive"

    @pytest.mark.parametrize("header", ["Cookie", "Proxy-Authorization"])
    def test_other_credential_headers_are_dropped_too(self, header):
        responses = [self._redirect("https://1.1.1.1/next"), self._ok()]
        with patch("requests.get", side_effect=responses) as mock_get:
            guarded_get("https://8.8.8.8/s", headers={header: "secret"})

        assert header not in mock_get.call_args_list[1].kwargs["headers"]

    def test_credentials_survive_a_same_origin_redirect(self):
        responses = [self._redirect("https://8.8.8.8/next"), self._ok()]
        with patch("requests.get", side_effect=responses) as mock_get:
            guarded_get("https://8.8.8.8/start",
                        headers={"Authorization": "Bearer sekrit"})

        sent = mock_get.call_args_list[1].kwargs["headers"]
        assert sent["Authorization"] == "Bearer sekrit"

    def test_the_callers_dict_is_not_mutated(self):
        responses = [self._redirect("https://1.1.1.1/next"), self._ok()]
        headers = {"Authorization": "Bearer sekrit"}
        with patch("requests.get", side_effect=responses):
            guarded_get("https://8.8.8.8/start", headers=headers)

        assert headers == {"Authorization": "Bearer sekrit"}

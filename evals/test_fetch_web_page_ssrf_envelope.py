"""
Regression eval: what the model is told when a fetch is refused.

`fetchWebPage` takes a URL the model chose, and the model reads pages other
people wrote. A refusal is therefore model-facing text, and its wording
steers what the model does next.

Two failures are possible and they need opposite advice:

- A **policy refusal** (loopback, private, link-local). Retrying can never
  work, and a model that reformulates the address — an IP for a name, a
  different port, a redirect through a public host — is working around a
  security control on behalf of whoever planted the URL.
- A **resolution failure** (typo, DNS outage). Retrying is legitimate, and
  telling the model to give up loses a page the user actually wanted.

Before the SSRF guard both cases came back as `Failed to fetch page`, and
the private-address case was not a refusal at all: the page was fetched.

This file is behavioural, not judge-driven: it exercises the real
`FetchWebPageTool.run` against a mocked network and asserts the observable
envelope.

Run: .venv/bin/python -m pytest evals/test_fetch_web_page_ssrf_envelope.py -v
"""

import socket
from unittest.mock import Mock, patch

import pytest

from jarvis.tools.base import ToolContext
from jarvis.tools.builtin.fetch_web_page import FetchWebPageTool


def _make_ctx():
    ctx = Mock(spec=ToolContext)
    ctx.user_print = Mock()
    ctx.cfg = Mock(voice_debug=False)
    ctx.language = "en"
    return ctx


@pytest.mark.eval
class TestAPolicyRefusalTellsTheModelToStop:
    """The model must not be left thinking a retry might work."""

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:5071/api/yolo",
        "http://169.254.169.254/latest/meta-data/",
        "http://192.168.0.1/",
    ])
    @patch("requests.get")
    def test_the_envelope_forbids_reformulating_the_address(self, mock_get, url):
        result = FetchWebPageTool().run({"url": url}, _make_ctx())

        assert result.success is False
        text = result.reply_text.lower()
        assert "not a public internet address" in text
        assert "do not retry" in text
        # The refusal has to precede the request, or the damage is done.
        mock_get.assert_not_called()

    @patch("requests.get")
    def test_a_refusal_is_not_dressed_up_as_a_network_error(self, mock_get):
        """`Failed to fetch page` invites a retry, which is the one thing
        that must not happen here."""
        result = FetchWebPageTool().run({"url": "http://10.0.0.1/"}, _make_ctx())

        assert "failed to fetch page" not in result.reply_text.lower()


@pytest.mark.eval
class TestAResolutionFailureLeavesTheDoorOpen:
    """A typo or a DNS blip is not a security event."""

    @patch("socket.getaddrinfo", side_effect=socket.gaierror("nope"))
    @patch("requests.get")
    def test_an_unresolvable_host_reads_as_an_ordinary_failure(self, mock_get, _dns):
        result = FetchWebPageTool().run(
            {"url": "https://exmaple.com/typo"}, _make_ctx(),
        )

        assert result.success is False
        text = result.reply_text.lower()
        assert "failed to fetch page" in text
        # Crucially, none of the policy language: the model may try again.
        assert "do not retry" not in text
        assert "not a public internet address" not in text


@pytest.mark.eval
class TestAPublicPageStillComesBack:
    """The refusals above are worthless if everything is refused."""

    @patch("socket.getaddrinfo")
    @patch("requests.get")
    def test_a_normal_page_is_returned_intact(self, mock_get, mock_dns):
        mock_dns.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
        ]
        body = (b"<html><head><title>Real Page</title></head>"
                b"<body><p>The content the user asked for.</p></body></html>")
        resp = Mock(status_code=200, content=body, text=body.decode(),
                    headers={}, is_redirect=False, is_permanent_redirect=False,
                    raise_for_status=Mock())
        resp.__enter__ = Mock(return_value=resp)
        resp.__exit__ = Mock(return_value=False)
        mock_get.return_value = resp

        result = FetchWebPageTool().run(
            {"url": "https://example.com/article"}, _make_ctx(),
        )

        assert result.success is True
        assert "content the user asked for" in result.reply_text

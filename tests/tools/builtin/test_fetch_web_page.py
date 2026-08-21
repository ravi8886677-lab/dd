"""Tests for fetch web page tool."""

import pytest
from unittest.mock import Mock, patch
import requests

from src.jarvis.tools.builtin.fetch_web_page import FetchWebPageTool
from src.jarvis.tools.base import ToolContext
from src.jarvis.tools.types import ToolExecutionResult

pytestmark = pytest.mark.unit


def _make_response_mock(**attrs) -> Mock:
    """Build a Mock that doubles as both the requests response and a context
    manager (the production code uses ``with requests.get(...) as resp`` so
    the connection is released deterministically).
    """
    # A bare Mock returns a truthy Mock for every attribute, so without these
    # defaults the SSRF guard reads an ordinary page response as a redirect.
    attrs.setdefault("is_redirect", False)
    attrs.setdefault("is_permanent_redirect", False)
    resp = Mock(**attrs)
    resp.__enter__ = Mock(return_value=resp)
    resp.__exit__ = Mock(return_value=False)
    return resp


class TestFetchWebPageRefusesNonPublicTargets:
    """fetchWebPage takes a URL the model chose, and the model reads attacker-
    controlled pages. It must not become a way to reach the loopback
    interface, the cloud metadata endpoint, or the user's LAN.
    """

    def setup_method(self):
        self.tool = FetchWebPageTool()
        self.context = Mock(spec=ToolContext)
        self.context.user_print = Mock()

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:5071/api/yolo",
        "http://localhost:5071/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/admin",
        "http://192.168.1.1/",
        "http://172.16.9.9/",
    ])
    def test_non_public_url_is_refused_without_a_request(self, url):
        with patch('requests.get') as mock_get:
            result = self.tool.run({"url": url}, self.context)
            assert result.success is False
            mock_get.assert_not_called()

    def test_refusal_is_explained_rather_than_blamed_on_the_network(self):
        """A connection error also says 'refused'. The model needs to learn the
        address was rejected by policy, so it stops trying to reach it."""
        result = self.tool.run({"url": "http://127.0.0.1/"}, self.context)
        assert result.success is False
        assert "public" in result.reply_text.lower()

    def test_redirect_into_private_space_is_refused(self):
        """The redirect target is a perfectly parseable page. Without the guard
        the tool follows the hop and succeeds, which is the bug."""
        redirect = _make_response_mock(
            is_redirect=True, is_permanent_redirect=False,
            headers={"Location": "http://127.0.0.1/admin"},
            status_code=200,
            text='<html><head><title>Admin</title></head><body><p>secrets</p></body></html>',
            content=b'<html><head><title>Admin</title></head><body><p>secrets</p></body></html>',
            raise_for_status=Mock(),
            close=Mock(),
        )
        with patch('requests.get', return_value=redirect):
            result = self.tool.run({"url": "https://8.8.8.8/start"}, self.context)
            assert result.success is False
            assert "secrets" not in result.reply_text


class TestFetchWebPageTool:
    """Test fetch web page tool functionality."""

    def setup_method(self):
        """Set up test fixtures."""
        self.tool = FetchWebPageTool()
        self.context = Mock(spec=ToolContext)
        self.context.user_print = Mock()

    def test_tool_properties(self):
        """Test tool metadata properties."""
        assert self.tool.name == "fetchWebPage"
        assert "fetch" in self.tool.description.lower()
        assert self.tool.inputSchema["type"] == "object"
        assert "url" in self.tool.inputSchema["required"]

    def test_run_no_args(self):
        """Test fetch web page with no arguments."""
        result = self.tool.run(None, self.context)

        assert isinstance(result, ToolExecutionResult)
        assert result.success is False
        assert "url" in result.reply_text.lower()

    def test_run_empty_url(self):
        """Test fetch web page with empty URL."""
        args = {"url": ""}
        result = self.tool.run(args, self.context)

        assert isinstance(result, ToolExecutionResult)
        assert result.success is False
        assert "url" in result.reply_text.lower()

    @patch('requests.get')
    def test_run_success(self, mock_get):
        """Test successful web page fetch."""
        mock_response = _make_response_mock(
            status_code=200,
            text='<html><head><title>Test</title></head><body><p>Content</p></body></html>',
            content=b'<html><head><title>Test</title></head><body><p>Content</p></body></html>',
            headers={'content-type': 'text/html'},
            raise_for_status=Mock(),
        )
        mock_get.return_value = mock_response

        args = {"url": "https://example.com"}
        result = self.tool.run(args, self.context)

        assert isinstance(result, ToolExecutionResult)
        assert result.success is True
        assert "example.com" in result.reply_text
        self.context.user_print.assert_called()

    @patch('requests.get')
    def test_run_success_without_beautifulsoup(self, mock_get):
        """Test successful web page fetch without BeautifulSoup."""
        mock_response = _make_response_mock(
            status_code=200,
            text='<html><body>Raw content</body></html>',
            content=b'<html><body>Raw content</body></html>',
            headers={'content-type': 'text/html'},
            raise_for_status=Mock(),
        )
        mock_get.return_value = mock_response

        with patch('builtins.__import__', side_effect=ImportError):
            args = {"url": "https://example.com"}
            result = self.tool.run(args, self.context)

        assert isinstance(result, ToolExecutionResult)
        assert result.success is True
        assert "Raw Content" in result.reply_text

    @patch('requests.get')
    def test_run_http_error(self, mock_get):
        """Test fetch web page with HTTP error."""
        mock_response = _make_response_mock(status_code=404)
        mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError("404 Not Found")
        mock_get.return_value = mock_response

        args = {"url": "https://example.com/notfound"}
        result = self.tool.run(args, self.context)

        assert isinstance(result, ToolExecutionResult)
        assert result.success is False
        assert "Failed to fetch page" in result.reply_text

    @patch('requests.get')
    def test_run_request_error(self, mock_get):
        """Test fetch web page with network error."""
        mock_get.side_effect = requests.exceptions.RequestException("Network error")

        args = {"url": "https://example.com"}
        result = self.tool.run(args, self.context)

        assert isinstance(result, ToolExecutionResult)
        assert result.success is False
        assert "Failed to fetch page" in result.reply_text

    def test_run_invalid_url(self):
        """Test fetch web page with invalid URL."""
        args = {"url": "not-a-url"}
        result = self.tool.run(args, self.context)
        assert isinstance(result, ToolExecutionResult)
        assert result.success is False
        assert "failed" in result.reply_text.lower() or "error" in result.reply_text.lower()

    @patch('requests.get')
    def test_run_with_links_extraction(self, mock_get):
        """Test fetch web page including link extraction when include_links=True."""
        html = (
            '<html><head><title>Links Page</title></head>'
            '<body><p>Intro</p>'
            '<a href="/relative">Relative Link</a>'
            '<a href="https://absolute.test/page">Absolute Link</a>'
            '<a href="mailto:test@example.com">Mail</a>'
            '</body></html>'
        )
        mock_response = _make_response_mock(
            status_code=200,
            text=html,
            content=html.encode(),
            raise_for_status=Mock(),
        )
        mock_get.return_value = mock_response

        args = {"url": "https://example.com", "include_links": True}
        result = self.tool.run(args, self.context)
        assert result.success is True
        assert isinstance(result, ToolExecutionResult)
        assert "Links found on page" in result.reply_text
        # relative link should be resolved to absolute
        assert "https://example.com/relative" in result.reply_text
        assert "absolute.test" in result.reply_text

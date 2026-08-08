"""Behaviour of the openApp tool.

This tool starts processes and opens pages on the user's machine, driven
by a model that reads attacker-controlled web pages. Most of what
matters here is what it *refuses*.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from src.jarvis.tools.base import ToolContext
from src.jarvis.tools.builtin.open_app import OpenAppTool


def _ctx():
    ctx = Mock(spec=ToolContext)
    ctx.user_print = Mock()
    ctx.cfg = Mock()
    return ctx


@pytest.mark.unit
class TestOpeningThings:
    @patch("src.jarvis.tools.builtin.open_app.webbrowser.open")
    def test_playing_a_song_opens_a_youtube_search(self, mock_open):
        """The motivating request: 'play a Hindi song on YouTube'."""
        result = OpenAppTool().run(
            {"site": "youtube", "query": "hindi songs"}, _ctx(),
        )

        assert result.success is True
        url = mock_open.call_args[0][0]
        assert url.startswith("https://www.youtube.com/results")
        assert "hindi+songs" in url

    @patch("src.jarvis.tools.builtin.open_app.webbrowser.open")
    def test_a_query_is_url_encoded(self, mock_open):
        """An unencoded query would break the URL, or smuggle parameters."""
        OpenAppTool().run({"site": "google", "query": "a & b?c=d"}, _ctx())

        url = mock_open.call_args[0][0]
        assert "&" not in url.split("?", 1)[1].replace("q=", "", 1).split("&")[0] or "%26" in url

    @patch("src.jarvis.tools.builtin.open_app.webbrowser.open")
    def test_an_https_url_opens(self, mock_open):
        result = OpenAppTool().run({"url": "https://example.com/x"}, _ctx())

        assert result.success is True
        mock_open.assert_called_once_with("https://example.com/x")

    @patch("src.jarvis.tools.builtin.open_app.shutil.which", return_value="/usr/bin/firefox")
    @patch("src.jarvis.tools.builtin.open_app.subprocess.Popen")
    def test_launching_an_app_starts_a_process(self, mock_popen, _which):
        result = OpenAppTool().run({"app": "browser"}, _ctx())

        assert result.success is True
        assert mock_popen.called

    @patch("src.jarvis.tools.builtin.open_app.shutil.which", return_value=None)
    def test_a_missing_app_reports_failure_rather_than_pretending(self, _which):
        """Claiming success when nothing launched would be a lie the user acts on."""
        result = OpenAppTool().run({"app": "browser"}, _ctx())

        assert result.success is False
        assert "could not find" in (result.error_message or "").lower()


@pytest.mark.unit
class TestRefusals:
    """The model reads web pages, and pages are attacker-controlled."""

    @patch("src.jarvis.tools.builtin.open_app.webbrowser.open")
    def test_file_urls_are_refused(self, mock_open):
        """file:// would let a page talk the assistant into exposing local files."""
        result = OpenAppTool().run({"url": "file:///etc/passwd"}, _ctx())

        assert result.success is False
        mock_open.assert_not_called()

    @patch("src.jarvis.tools.builtin.open_app.webbrowser.open")
    def test_javascript_urls_are_refused(self, mock_open):
        result = OpenAppTool().run({"url": "javascript:alert(1)"}, _ctx())

        assert result.success is False
        mock_open.assert_not_called()

    @patch("src.jarvis.tools.builtin.open_app.webbrowser.open")
    def test_data_urls_are_refused(self, mock_open):
        result = OpenAppTool().run({"url": "data:text/html,<script>x</script>"}, _ctx())

        assert result.success is False
        mock_open.assert_not_called()

    @patch("src.jarvis.tools.builtin.open_app.subprocess.Popen")
    def test_an_unknown_app_name_launches_nothing(self, mock_popen):
        result = OpenAppTool().run({"app": "rm -rf /"}, _ctx())

        assert result.success is False
        mock_popen.assert_not_called()

    def test_the_schema_offers_no_command_field(self):
        """The load-bearing constraint: the model cannot express a command.

        If a `command`-style field ever appears here, an injected
        instruction on any fetched page becomes code execution on the
        user's machine.
        """
        props = set(OpenAppTool().inputSchema["properties"])
        assert props == {"app", "site", "query", "url"}
        for forbidden in ("command", "cmd", "args", "shell", "exec", "path"):
            assert forbidden not in props

    def test_app_and_site_are_closed_sets(self):
        """Free-form values would defeat the allow-list."""
        schema = OpenAppTool().inputSchema["properties"]
        assert schema["app"]["enum"]
        assert schema["site"]["enum"]

    @patch("src.jarvis.tools.builtin.open_app.subprocess.Popen")
    @patch("src.jarvis.tools.builtin.open_app.webbrowser.open")
    def test_an_empty_request_does_nothing(self, mock_open, mock_popen):
        result = OpenAppTool().run({}, _ctx())

        assert result.success is False
        mock_open.assert_not_called()
        mock_popen.assert_not_called()

    @patch("src.jarvis.tools.builtin.open_app.webbrowser.open")
    def test_a_site_search_without_a_query_is_refused(self, mock_open):
        """Otherwise the model opens a bare search page and calls it done."""
        result = OpenAppTool().run({"site": "youtube"}, _ctx())

        assert result.success is False
        mock_open.assert_not_called()

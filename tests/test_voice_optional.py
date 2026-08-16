"""
Tests for installs that do not want voice.

Voice costs a Whisper model download and a live microphone. An install that
only ever types should pay neither, and should not be offered tools whose
dependencies it never installed.
"""

import sys
from types import SimpleNamespace

import pytest

from jarvis.config import load_settings


class TestVoiceSwitch:
    """`voice_enabled` decides whether the audio stack starts at all."""

    def test_voice_is_on_by_default(self):
        """Voice is the product; turning it off is the opt-out."""
        assert load_settings().voice_enabled is True

    def test_disabled_config_reaches_settings(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text('{"voice_enabled": false}')
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_file))
        assert load_settings().voice_enabled is False


class TestToolAvailability:
    """A tool whose dependency is missing is not advertised to the model."""

    def test_computer_use_is_unavailable_without_pyautogui(self, monkeypatch):
        """Offering a tool that raises ImportError on call wastes a whole turn."""
        from jarvis.tools.builtin.computer_use import ComputerUseTool

        monkeypatch.setitem(sys.modules, "pyautogui", None)
        assert ComputerUseTool().is_available() is False

    def test_a_tool_with_no_extra_dependency_is_always_available(self):
        from jarvis.tools.builtin.web_search import WebSearchTool

        assert WebSearchTool().is_available() is True

    def test_the_advertised_catalogue_excludes_unavailable_tools(self, monkeypatch):
        from jarvis.tools import registry

        monkeypatch.setitem(sys.modules, "pyautogui", None)
        advertised = registry.available_builtin_tools()
        assert "computerUse" not in advertised
        assert "webSearch" in advertised

    def test_the_full_registry_still_resolves_unavailable_tools_by_name(self, monkeypatch):
        """An explicit call must reach the tool and get its real error.

        Dropping the name entirely would turn a missing dependency into
        "no such tool", which sends the model looking for a different one
        instead of reporting what is actually wrong.
        """
        from jarvis.tools import registry

        monkeypatch.setitem(sys.modules, "pyautogui", None)
        assert "computerUse" in registry.BUILTIN_TOOLS

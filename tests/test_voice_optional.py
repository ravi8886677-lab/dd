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


class TestTheDaemonBuildsNoListener:
    """Voice off has to mean the listener is never *constructed*.

    Constructing a `VoiceListener` is what loads the Whisper model and opens
    the microphone; starting the thread is a later step. A daemon that builds
    one and declines to start it still downloads the model and still takes the
    device, so the whole feature is defeated while every config-level test
    stays green. Assert on construction, not on `.start()`.
    """

    def _run_daemon(self, voice_enabled: bool):
        """Drive `daemon.main` through its smoke path with voice on or off."""
        import io
        from unittest.mock import MagicMock, patch

        # `patch` resolves a target by walking attributes from the top-level
        # package, so the submodule has to be imported before `jarvis.daemon`
        # is an attribute of `jarvis` at all. Without this the patch raises
        # AttributeError and the failure reads as a broken daemon rather than
        # an unimported module.
        import jarvis.daemon  # noqa: F401

        from tests.conftest import MockConfig

        with patch("jarvis.daemon.load_settings") as mock_load, \
             patch("jarvis.daemon.Database") as mock_db, \
             patch("jarvis.daemon.discover_and_report_mcp_tools", return_value={}), \
             patch("jarvis.daemon.DialogueMemory") as mock_dm, \
             patch("jarvis.daemon.get_location_context", return_value="Location: Test"), \
             patch("jarvis.daemon.create_tts_engine") as mock_tts, \
             patch("jarvis.daemon.VoiceListener") as mock_vl, \
             patch("jarvis.memory.graph.GraphMemoryStore") as mock_graph:

            mock_load.return_value = MockConfig(
                db_path=":memory:", voice_enabled=voice_enabled,
            )
            mock_db.return_value = MagicMock()
            mock_dm.return_value = MagicMock()

            tts_instance = MagicMock()
            tts_instance.enabled = False
            mock_tts.return_value = tts_instance

            mock_vl.return_value = MagicMock()

            graph_instance = MagicMock()
            graph_instance.migrate_legacy_shape.return_value = False
            mock_graph.return_value = graph_instance

            captured = io.StringIO()
            with patch("sys.stdout", captured):
                from jarvis.daemon import main
                main(smoke_test=True)

            return mock_vl, captured.getvalue()

    def test_voice_off_never_constructs_a_listener(self):
        """No listener object means no Whisper download and no microphone."""
        mock_vl, output = self._run_daemon(voice_enabled=False)

        assert mock_vl.call_count == 0, (
            "VoiceListener was constructed on a text-only install, which "
            "loads Whisper and opens the microphone regardless of whether "
            "the thread is ever started"
        )
        assert "SMOKE_TEST_INIT_OK" in output, "the daemon must still come up"

    def test_voice_on_constructs_and_starts_the_listener(self):
        """The guard must not have cost the default install its listener."""
        mock_vl, output = self._run_daemon(voice_enabled=True)

        assert mock_vl.call_count == 1
        mock_vl.return_value.start.assert_called_once()
        assert "SMOKE_TEST_INIT_OK" in output

    def test_dictation_cannot_outlive_the_listener(self):
        """Dictation shares the listener's Whisper model, so voice off kills it."""
        _mock_vl, output = self._run_daemon(voice_enabled=False)

        assert "Dictation disabled" in output, (
            f"dictation was left enabled without a Whisper model:\n{output}"
        )


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

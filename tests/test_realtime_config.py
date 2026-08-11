"""Realtime voice configuration.

The spec puts two guarantees in config rather than in the session: the
feature is off unless the user turns it on, and audio cannot start leaving
the machine because a flag was flipped without credentials behind it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.config import load_settings
from jarvis.utils.secret_store import CREDENTIAL_KEYS


def _settings_from(tmp_path, monkeypatch, config: dict):
    """Load settings from a config file holding exactly ``config``."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
    # The credential store is not part of what these tests are about, and
    # asking a real keyring for a secret makes them depend on the host.
    monkeypatch.setattr("jarvis.config.resolve_secret", lambda name, value: value)
    return load_settings()


@pytest.mark.unit
def test_realtime_voice_is_off_by_default(tmp_path, monkeypatch):
    """The local pipeline is the default path, not the hosted one."""
    cfg = _settings_from(tmp_path, monkeypatch, {})

    assert cfg.realtime_voice_enabled is False


@pytest.mark.unit
def test_enabling_it_without_a_key_leaves_it_off(tmp_path, monkeypatch):
    """A flag with no credentials behind it must not half-open the feature.

    Otherwise the failure surfaces at the first spoken word, after the
    user believes voice is live.
    """
    cfg = _settings_from(
        tmp_path,
        monkeypatch,
        {"realtime_voice_enabled": True, "realtime_api_key": ""},
    )

    assert cfg.realtime_voice_enabled is False


@pytest.mark.unit
def test_enabling_it_with_a_key_turns_it_on(tmp_path, monkeypatch):
    cfg = _settings_from(
        tmp_path,
        monkeypatch,
        {"realtime_voice_enabled": True, "realtime_api_key": "sk-test"},
    )

    assert cfg.realtime_voice_enabled is True


@pytest.mark.unit
def test_loading_config_does_not_drag_in_the_tool_registry():
    """Reading a config file must not require the whole tool stack.

    `load_settings` reaches the realtime factory to validate the provider.
    If that path imports the tool registry it also imports `mcp`, so
    loading a config file fails outright wherever that package is absent,
    and config and tools become an import cycle.

    Run in a subprocess because other tests import the registry, which
    would mask the regression in-process.
    """
    probe = (
        "import sys; sys.path.insert(0, 'src');"
        "from jarvis.config import load_settings;"
        "load_settings();"
        "print('registry' if 'jarvis.tools.registry' in sys.modules else 'clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("clean"), (
        "loading config imported the tool registry"
    )


@pytest.mark.unit
def test_the_realtime_key_is_treated_as_a_credential():
    """It must migrate to the OS store like every other key, not sit in JSON."""
    assert "realtime_api_key" in CREDENTIAL_KEYS

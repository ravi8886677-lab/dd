"""
Tests that the speech-provider rename does not break installs already out there.

Renaming the provider was a compliance fix: `CLAUDE.md` line 3 forbids
depending on a proprietary cloud vendor, so `openai_stt.py` stopped carrying
Groq's URL as a baked-in default. Correct, and on its own it silently breaks
every install that was relying on that default.

A shipped config said `{"stt_provider": "groq", "stt_api_key": "..."}` and
nothing more, because the endpoint was implied. After the rename the alias
resolves the *name* and the endpoint is empty, so the factory returns `None`
and transcription drops to local Whisper without a word. On a voice-optional
install that never downloaded a Whisper model, "drops to local" means no
recognition at all.

The alias is not enough. Only a migration repairs a config already on disk.
"""

import json
import os
from pathlib import Path

import pytest

# CI runs `pytest -m unit`. Without this every test in this file is
# deselected there and the guards below only fire on a local pre-push hook.
pytestmark = pytest.mark.unit

# The endpoint the removed `DEFAULT_BASE_URL` used to supply. It lives here
# and in the migration only. Reintroducing it as a runtime default is the
# thing the rename exists to prevent.
_HISTORICAL_DEFAULT = "https://api.groq.com/openai/v1"


def _load_with(tmp_path, monkeypatch, config: dict):
    """Write a config, load it through the real path, return settings + disk."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(config))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))

    from jarvis.config import load_settings

    settings = load_settings()
    return settings, json.loads(cfg_path.read_text())


class TestShippedConfigsKeepTranscribing:
    """The upgrade must not cost a working install its hosted recogniser."""

    def test_a_groq_config_that_relied_on_the_default_still_reaches_an_endpoint(
        self, tmp_path, monkeypatch
    ):
        """The exact shape an earlier release wrote: a name and a key, no URL."""
        settings, _disk = _load_with(tmp_path, monkeypatch, {
            "_config_version": 4,
            "stt_provider": "groq",
            "stt_api_key": "a-key",
        })

        from jarvis.speech.factory import get_stt_backend

        assert settings.stt_base_url == _HISTORICAL_DEFAULT, (
            "the endpoint the old default supplied was not backfilled, so this "
            "install silently stops using its hosted recogniser"
        )
        assert get_stt_backend(settings) is not None, (
            "an upgraded install fell back to local Whisper with no message"
        )

    def test_the_provider_name_is_rewritten_on_disk(self, tmp_path, monkeypatch):
        """A runtime alias leaves the stale name on disk for every later reader."""
        settings, disk = _load_with(tmp_path, monkeypatch, {
            "_config_version": 4,
            "stt_provider": "groq",
            "stt_api_key": "a-key",
        })

        from jarvis.config import CONFIG_VERSION

        assert settings.stt_provider == "openai_compatible"
        assert disk["stt_provider"] == "openai_compatible"
        assert disk["_config_version"] == CONFIG_VERSION

    def test_a_custom_endpoint_is_never_overwritten(self, tmp_path, monkeypatch):
        """Someone already pointing at their own box must keep pointing there."""
        settings, disk = _load_with(tmp_path, monkeypatch, {
            "_config_version": 4,
            "stt_provider": "groq",
            "stt_api_key": "a-key",
            "stt_base_url": "http://localhost:8080/v1",
        })

        assert settings.stt_base_url == "http://localhost:8080/v1"
        assert disk["stt_base_url"] == "http://localhost:8080/v1"


class TestTheDefaultIsNotResurrected:
    """Backfilling a stale config must not become a live default again."""

    def test_a_local_config_gains_no_endpoint(self, tmp_path, monkeypatch):
        """Nothing is written for an install that never chose a hosted provider."""
        settings, disk = _load_with(tmp_path, monkeypatch, {
            "_config_version": 4,
            "stt_provider": "local",
        })

        assert settings.stt_base_url == ""
        assert "stt_base_url" not in disk or not disk["stt_base_url"]

    def test_a_fresh_config_choosing_the_provider_still_needs_an_endpoint(
        self, tmp_path, monkeypatch
    ):
        """The compliance point: unset means unconfigured, not 'reach a vendor'.

        A config written *after* the rename names the protocol, not a vendor,
        so there is no historical default to inherit. Backfilling it here
        would put audio on a third party the user never named — exactly what
        removing `DEFAULT_BASE_URL` prevented.
        """
        settings, _disk = _load_with(tmp_path, monkeypatch, {
            "_config_version": 5,
            "stt_provider": "openai_compatible",
            "stt_api_key": "a-key",
        })

        from jarvis.speech.factory import get_stt_backend

        assert settings.stt_base_url == ""
        assert get_stt_backend(settings) is None


class TestMigrationRunsOnce:
    """`load_settings` re-enters itself, so a migration must not run twice.

    `_load_keyring` calls `debug_log`, which asks `_is_debug_enabled`, which
    calls `load_settings` again — from inside the first `load_settings`, before
    it has saved. The inner call sees the pre-migration version and migrates a
    second time: the work is repeated, the credential store is queried again,
    and any migration that speaks to the user says it twice.

    `factory.py` documents this hazard for its own warning. The migration
    itself needs the same protection, and a guard there covers every future
    migration rather than each one remembering.
    """

    def test_a_migration_is_applied_exactly_once_per_load(self, tmp_path):
        """Driven in a subprocess, because the recursion depends on real startup.

        In-process the keyring path is already warm from earlier tests and the
        recursion does not fire, so an in-process version of this test passes
        while the behaviour is broken — it would certify the bug rather than
        catch it.
        """
        import json
        import subprocess
        import sys
        import textwrap

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({
            "_config_version": 4,
            "stt_provider": "groq",
            "stt_api_key": "a-key",
        }))

        # Counts the notice the user actually sees, not an internal call,
        # so the assertion survives the migration being refactored.
        program = textwrap.dedent("""
            import jarvis.config as c
            c.load_settings()
        """)

        result = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True, text=True, timeout=120,
            env={
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                "JARVIS_CONFIG_PATH": str(cfg_path),
            },
        )
        count = result.stdout.count("Speech provider renamed")
        assert count, f"the upgrade notice never printed:\n{result.stdout}\n{result.stderr}"
        assert count == 1, (
            f"the upgrade notice printed {count} times. `load_settings` "
            "re-enters itself through _load_keyring -> debug_log -> "
            "_is_debug_enabled, so every notice is printed twice and the "
            "credential store is queried twice."
        )


class TestALocalEndpointNeedsNoKey:
    """The offline path the rename exists for must actually be reachable.

    A `whisper.cpp` server on this machine authenticates nobody. Requiring a
    key before the adapter is even built blocks the one configuration that
    makes the provider rename worth doing, and contradicts both the settings
    tooltip ("usually not [required] by a local one") and the module
    docstring's claim that pointing at a local server keeps the offline path
    real.

    The original rule — refuse without a key rather than build something that
    fails on every call — was written for a hosted endpoint, where a missing
    key means certain failure. An endpoint the user named is configured; if it
    rejects the request, the adapter returns `None` and the caller drops to
    local Whisper, which is the contract everywhere else in this package.
    """

    def test_a_keyless_local_endpoint_produces_a_backend(self):
        from types import SimpleNamespace

        from jarvis.speech.factory import get_stt_backend

        settings = SimpleNamespace(
            stt_provider="openai_compatible",
            stt_api_key="",
            stt_model="",
            stt_base_url="http://localhost:8080/v1",
        )
        assert get_stt_backend(settings) is not None, (
            "a local whisper.cpp server, which needs no key, could not be used"
        )

    def test_an_endpoint_with_a_key_still_produces_a_backend(self):
        from types import SimpleNamespace

        from jarvis.speech.factory import get_stt_backend

        settings = SimpleNamespace(
            stt_provider="openai_compatible",
            stt_api_key="a-key",
            stt_model="",
            stt_base_url="https://example.invalid/v1",
        )
        assert get_stt_backend(settings) is not None

    def test_no_endpoint_is_still_unconfigured(self):
        """The endpoint, not the key, is what decides configured-ness."""
        from types import SimpleNamespace

        from jarvis.speech.factory import get_stt_backend

        settings = SimpleNamespace(
            stt_provider="openai_compatible",
            stt_api_key="a-key",
            stt_model="",
            stt_base_url="",
        )
        assert get_stt_backend(settings) is None

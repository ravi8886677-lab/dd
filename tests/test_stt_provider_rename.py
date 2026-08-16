"""
Tests for the vendor-neutral hosted speech provider.

`CLAUDE.md` line 3 forbids depending on a proprietary cloud vendor. The
adapter was always generic — `stt_base_url` points at anything serving the
OpenAI transcriptions shape, including a local `whisper.cpp` server — but it
was named for one vendor and carried that vendor's URL as a baked-in default.
These tests pin the compliant shape: a neutral provider name, the old name
still accepted so shipped configs keep working, and no endpoint assumed.
"""

from types import SimpleNamespace

import pytest

from jarvis.speech.factory import OPENAI_COMPATIBLE, resolve_stt_provider, get_stt_backend


class TestProviderNaming:
    """The provider is named for the protocol, not for a company."""

    def test_openai_compatible_is_the_name(self):
        assert resolve_stt_provider("openai_compatible") == OPENAI_COMPATIBLE

    def test_groq_still_resolves_so_existing_configs_keep_working(self):
        """A config written by a shipped release must not break on upgrade."""
        assert resolve_stt_provider("groq") == OPENAI_COMPATIBLE

    def test_unknown_names_still_fall_back_to_local(self):
        assert resolve_stt_provider("nonsense-provider") == "local"

    def test_empty_means_local(self):
        assert resolve_stt_provider("") == "local"
        assert resolve_stt_provider(None) == "local"


class TestNoAssumedEndpoint:
    """Nothing points at a vendor unless the user typed the URL."""

    def test_provider_without_an_endpoint_is_unconfigured(self):
        """No baked-in URL means an unset endpoint cannot silently reach a vendor."""
        settings = SimpleNamespace(
            stt_provider="openai_compatible",
            stt_api_key="a-key",
            stt_model="",
            stt_base_url="",
        )
        assert get_stt_backend(settings) is None

    def test_provider_without_a_key_is_unconfigured(self):
        settings = SimpleNamespace(
            stt_provider="openai_compatible",
            stt_api_key="",
            stt_model="",
            stt_base_url="https://example.invalid/v1",
        )
        assert get_stt_backend(settings) is None

    def test_endpoint_and_key_together_produce_a_backend(self):
        settings = SimpleNamespace(
            stt_provider="openai_compatible",
            stt_api_key="a-key",
            stt_model="",
            stt_base_url="http://localhost:8080/v1",
        )
        backend = get_stt_backend(settings)
        assert backend is not None

    def test_a_local_endpoint_is_as_valid_as_a_hosted_one(self):
        """The offline path is the point of naming the provider for the protocol."""
        settings = SimpleNamespace(
            stt_provider="openai_compatible",
            stt_api_key="unused-by-whisper-cpp",
            stt_base_url="http://127.0.0.1:8080/v1",
            stt_model="whisper-1",
        )
        assert get_stt_backend(settings) is not None

    def test_local_provider_never_produces_a_backend(self):
        settings = SimpleNamespace(
            stt_provider="local",
            stt_api_key="a-key",
            stt_model="",
            stt_base_url="http://localhost:8080/v1",
        )
        assert get_stt_backend(settings) is None

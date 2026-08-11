"""Choosing a realtime provider from settings.

The session runner is provider-agnostic, so exactly one place decides which
adapter serves a session. It also assembles the connection details, and
what it puts in them is what leaves the machine.
"""

from __future__ import annotations

import pytest

from jarvis.realtime.factory import create_realtime_backend, realtime_config_from
from jarvis.realtime.gemini_realtime import GeminiRealtimeBackend
from jarvis.realtime.openai_realtime import OpenAIRealtimeBackend


class _Cfg:
    def __init__(self, **kw):
        self.realtime_provider = kw.get("provider", "gemini")
        self.realtime_model = kw.get("model", "gemini-live-2.5-flash-preview")
        self.realtime_api_key = kw.get("api_key", "k")
        self.realtime_base_url = kw.get("base_url", "")
        self.realtime_voice = kw.get("voice", "")


@pytest.mark.unit
def test_gemini_is_served_by_the_gemini_adapter():
    assert isinstance(create_realtime_backend(_Cfg(provider="gemini")), GeminiRealtimeBackend)


@pytest.mark.unit
def test_openai_is_served_by_the_openai_adapter():
    assert isinstance(create_realtime_backend(_Cfg(provider="openai")), OpenAIRealtimeBackend)


@pytest.mark.unit
def test_an_unknown_provider_falls_back_to_the_default_rather_than_failing():
    """A typo in config should not take voice out entirely."""
    assert isinstance(create_realtime_backend(_Cfg(provider="nonsense")), GeminiRealtimeBackend)


@pytest.mark.unit
def test_the_connection_carries_the_configured_model_key_and_voice():
    cfg = _Cfg(model="gemini-live-2.5-flash-preview", api_key="sk-test", voice="Puck")

    config = realtime_config_from(cfg, instructions="be brief", tools=[{"name": "stop"}])

    assert config.model == "gemini-live-2.5-flash-preview"
    assert config.api_key == "sk-test"
    assert config.voice == "Puck"
    assert config.instructions == "be brief"
    assert config.tools == [{"name": "stop"}]


@pytest.mark.unit
def test_an_absent_endpoint_override_stays_empty_so_the_adapter_default_applies():
    config = realtime_config_from(_Cfg(base_url=""), instructions="", tools=None)

    assert config.base_url == ""

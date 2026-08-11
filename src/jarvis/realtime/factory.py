"""Pick the realtime provider and assemble its connection details.

The one place that names an adapter. Everything else works against
``RealtimeVoiceBackend``, so adding a provider means adding a branch here
and an adapter beside it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..debug import debug_log
from .backend import RealtimeConfig, RealtimeVoiceBackend
from .gemini_realtime import GeminiRealtimeBackend
from .openai_realtime import OpenAIRealtimeBackend

GEMINI = "gemini"
OPENAI = "openai"
DEFAULT_PROVIDER = GEMINI

# Each provider names its own realtime model, so an empty setting resolves
# against the provider rather than against one hardcoded string.
DEFAULT_MODELS = {
    GEMINI: "gemini-live-2.5-flash-preview",
    OPENAI: "gpt-realtime",
}


def resolve_provider(raw: Any) -> str:
    """Normalise a configured provider name.

    An unrecognised value falls back to the default rather than raising: a
    typo in config should cost the user their choice of provider, not the
    whole feature.
    """
    name = str(raw or "").strip().lower()
    if name in DEFAULT_MODELS:
        return name
    if name:
        debug_log(f"⚠️ realtime: unknown provider {name!r}, using {DEFAULT_PROVIDER}")
    return DEFAULT_PROVIDER


def default_model_for(provider: str) -> str:
    """The model to use when the setting is empty."""
    return DEFAULT_MODELS[resolve_provider(provider)]


def create_realtime_backend(settings: Any) -> RealtimeVoiceBackend:
    """Build the adapter for the configured provider."""
    provider = resolve_provider(getattr(settings, "realtime_provider", None))
    if provider == OPENAI:
        return OpenAIRealtimeBackend()
    return GeminiRealtimeBackend()


def realtime_config_from(
    settings: Any,
    *,
    instructions: str = "",
    tools: Optional[List[Dict[str, Any]]] = None,
) -> RealtimeConfig:
    """Gather what a session needs to connect.

    ``base_url`` stays empty when unset so each adapter applies its own
    endpoint, rather than one provider's URL being handed to another.
    """
    provider = resolve_provider(getattr(settings, "realtime_provider", None))
    model = str(getattr(settings, "realtime_model", "") or "").strip()

    return RealtimeConfig(
        model=model or DEFAULT_MODELS[provider],
        api_key=str(getattr(settings, "realtime_api_key", "") or ""),
        base_url=str(getattr(settings, "realtime_base_url", "") or "").strip(),
        voice=str(getattr(settings, "realtime_voice", "") or "").strip(),
        instructions=instructions,
        tools=tools,
    )

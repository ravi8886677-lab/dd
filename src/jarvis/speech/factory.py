"""Pick the speech-to-text provider from settings.

The one place that names an adapter. ``None`` means "no hosted provider",
which is the default and leaves every caller on local Whisper.

Providers are named for the protocol they speak, never for a company:
``CLAUDE.md`` forbids depending on a proprietary cloud vendor, and the
same setting has to be able to point at a local ``whisper.cpp`` server.
"""

from __future__ import annotations

from typing import Any, Optional

from ..debug import debug_log
from .backend import SpeechToText
from .openai_stt import OpenAICompatibleSpeechToText

LOCAL = "local"
OPENAI_COMPATIBLE = "openai_compatible"
PROVIDERS = (LOCAL, OPENAI_COMPATIBLE)

# Configs written by earlier releases name the vendor. They keep working:
# the adapter behind the name never changed, only what it is called.
ALIASES = {"groq": OPENAI_COMPATIBLE}


def resolve_stt_provider(raw: Any) -> str:
    """Normalise a configured provider name, defaulting to local."""
    name = str(raw or "").strip().lower()
    name = ALIASES.get(name, name)
    if name in PROVIDERS:
        return name
    if name:
        # print, not debug_log: this runs inside load_settings, and
        # debug_log asks load_settings whether debug output is on. That
        # recursion re-reads config and re-queries the credential store
        # on every level before it unwinds.
        print(f"  ⚠️  Unknown speech provider {name!r} — using local", flush=True)
    return LOCAL


def get_stt_backend(settings: Any) -> Optional[SpeechToText]:
    """Return the hosted recogniser, or ``None`` to use local Whisper.

    A provider configured without an endpoint or without a key resolves to
    ``None`` rather than to a backend that fails on every call: the failure
    would be silent anyway, since callers fall back, so it is better to
    never take the detour. There is no default endpoint, so an unset
    ``stt_base_url`` means unconfigured — never a third party the user did
    not name.
    """
    provider = resolve_stt_provider(getattr(settings, "stt_provider", None))
    if provider != OPENAI_COMPATIBLE:
        return None

    base_url = str(getattr(settings, "stt_base_url", "") or "").strip()
    if not base_url:
        debug_log("⚠️ stt: hosted provider selected but no endpoint set, using local", "whisper")
        return None

    api_key = str(getattr(settings, "stt_api_key", "") or "").strip()
    if not api_key:
        debug_log("⚠️ stt: hosted provider selected but no key set, using local", "whisper")
        return None

    return OpenAICompatibleSpeechToText(
        api_key=api_key,
        model=str(getattr(settings, "stt_model", "") or "").strip(),
        base_url=base_url,
    )

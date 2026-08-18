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

    The endpoint decides whether this is configured, not the key. Without an
    endpoint there is nowhere to send audio and this resolves to ``None``;
    there is no default, so unset means unconfigured rather than a third
    party the user did not name. A key is optional, because a local
    recogniser does not want one.
    """
    provider = resolve_stt_provider(getattr(settings, "stt_provider", None))
    if provider != OPENAI_COMPATIBLE:
        return None

    base_url = str(getattr(settings, "stt_base_url", "") or "").strip()
    if not base_url:
        debug_log("⚠️ stt: hosted provider selected but no endpoint set, using local", "whisper")
        return None

    # No key requirement. A `whisper.cpp` server on this machine
    # authenticates nobody, and refusing to build the adapter without a key
    # would block the one configuration that makes naming the provider for
    # its protocol worth doing. An endpoint the user named is configured; a
    # destination that wants a key and did not get one answers with an error,
    # the adapter returns None, and the caller drops to local Whisper — the
    # same contract every other failure here follows.
    api_key = str(getattr(settings, "stt_api_key", "") or "").strip()

    return OpenAICompatibleSpeechToText(
        api_key=api_key,
        model=str(getattr(settings, "stt_model", "") or "").strip(),
        base_url=base_url,
    )

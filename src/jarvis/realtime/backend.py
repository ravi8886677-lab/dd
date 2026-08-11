"""The contract a realtime voice provider implements.

Everything above this file (the session runner, the tool bridge, the audio
coordination) is written against these types and knows no vendor's wire
format. Adding a provider means writing an adapter, not touching the
session.

Adapters translate in both directions and are the only place a vendor's
event names appear. What comes out is a ``RealtimeEvent``; what goes in is
audio, a function result, or an instruction to stop talking.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterator, List, Optional

from .tool_bridge import FunctionCall, FunctionResult


class RealtimeEventType(Enum):
    """What a provider can tell us."""

    AUDIO_DELTA = "audio_delta"          # speech to play, PCM bytes
    ASSISTANT_TEXT = "assistant_text"    # what the model is saying, as text
    USER_TRANSCRIPT = "user_transcript"  # what the model heard
    SPEECH_STARTED = "speech_started"    # the user cut in; stop playback now
    FUNCTION_CALL = "function_call"      # the model wants a tool run
    TURN_DONE = "turn_done"
    ERROR = "error"
    CLOSED = "closed"


@dataclass(frozen=True)
class RealtimeEvent:
    """One thing that happened in a session.

    Only the field belonging to ``type`` is populated. Audio arrives as raw
    PCM so the session never has to know a provider's container format.
    """

    type: RealtimeEventType
    audio: Optional[bytes] = None
    text: Optional[str] = None
    function_call: Optional[FunctionCall] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class RealtimeConfig:
    """What a session needs to open a connection.

    ``tools`` is already in the flattened realtime shape; see
    ``tool_bridge.realtime_tool_schema``.
    """

    model: str
    api_key: str
    base_url: str = ""
    voice: str = ""
    instructions: str = ""
    tools: Optional[List[Dict[str, Any]]] = None


class RealtimeVoiceBackend(ABC):
    """A live speech-to-speech connection.

    Implementations are used from one thread owning an asyncio loop, the
    pattern ``tools/external/mcp_runtime.py`` establishes. Callers of the
    session never see a coroutine, and neither does this interface.
    """

    @abstractmethod
    def connect(self, config: RealtimeConfig) -> None:
        """Open the session. Raises on failure so the caller can fall back."""

    @abstractmethod
    def send_audio(self, pcm: bytes) -> None:
        """Stream one chunk of microphone audio."""

    @abstractmethod
    def send_function_result(self, result: FunctionResult) -> None:
        """Hand back what a tool produced, against its call id."""

    @abstractmethod
    def interrupt(self) -> None:
        """Tell the provider to abandon the reply it is speaking."""

    @abstractmethod
    def events(self) -> Iterator[RealtimeEvent]:
        """Yield events until the session closes.

        Ends with a ``CLOSED`` event rather than raising, so a dropped
        socket and a clean shutdown look the same to the session runner
        and both reach the fallback path.
        """

    @abstractmethod
    def close(self) -> None:
        """Close the connection. Safe to call more than once."""


__all__ = [
    "FunctionCall",
    "FunctionResult",
    "RealtimeConfig",
    "RealtimeEvent",
    "RealtimeEventType",
    "RealtimeVoiceBackend",
]

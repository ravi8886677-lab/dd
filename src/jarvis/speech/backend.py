"""The contract a speech-to-text provider implements.

Callers hold audio as float32 samples in [-1, 1], which is what both the
listener and the dictation engine already have in hand. Encoding to
whatever a provider wants on the wire is the adapter's job.

``transcribe`` returns ``None`` rather than raising when it cannot produce
a transcript. Every caller has local Whisper behind it, so a hosted
provider failing must read as "use the local one", not as an error the
user sees. An empty string is a real answer meaning silence.

``transcribe_detailed`` is the same call with the per-segment metadata the
listener needs. The listener does not simply take Whisper's text: it drops
segments by ``avg_logprob`` and by ``no_speech_prob``, which is what stops
a news jingle from being heard as a command. A provider that returns only
a string would route around those filters, so the richer shape exists to
let a hosted transcript face exactly the same gate as a local one. The
default implementation degrades honestly — text, no segments — so a
provider that cannot supply metadata is merely unfiltered, not broken.
"""

from __future__ import annotations

import io
import wave
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence


@dataclass(frozen=True)
class Transcription:
    """A transcript plus whatever the provider knew about it.

    ``language`` is an ISO-639-1 code or ``None``. ``segments`` mirrors
    Whisper's own segment dicts — the keys the listener reads are
    ``text``, ``avg_logprob`` and ``no_speech_prob`` — and is empty when
    the provider does not break the audio down, which callers must treat
    as "no basis to filter" rather than as "nothing to filter".
    """

    text: str
    language: Optional[str] = None
    segments: Sequence[Dict[str, Any]] = field(default_factory=tuple)


class SpeechToText(ABC):
    """A speech recogniser."""

    @abstractmethod
    def transcribe(
        self,
        audio: Any,
        sample_rate: int = 16000,
        timeout_sec: float = 15.0,
    ) -> Optional[str]:
        """Return the transcript, or ``None`` if this provider could not.

        ``None`` means "fall back"; ``""`` means "heard nothing".
        """

    def transcribe_detailed(
        self,
        audio: Any,
        sample_rate: int = 16000,
        timeout_sec: float = 15.0,
    ) -> Optional[Transcription]:
        """``transcribe`` plus segment metadata, on the same contract.

        Overriding this is optional. The default wraps ``transcribe`` so
        every provider satisfies the richer interface from the day it is
        written, at the cost of contributing no segments to filter on.
        """
        text = self.transcribe(audio, sample_rate, timeout_sec)
        if text is None:
            return None
        return Transcription(text=text)


def pcm16_wav_bytes(audio: Any, sample_rate: int = 16000) -> bytes:
    """Encode float32 samples in [-1, 1] as a mono 16-bit PCM WAV.

    Providers take an uploaded audio file rather than raw samples, and WAV
    is the one container every one of them accepts. Clipping is explicit:
    without it, samples outside [-1, 1] wrap around into loud noise that a
    recogniser hears as garbage rather than as the clipping it is.
    """
    import numpy as np

    samples = np.asarray(audio, dtype=np.float32).flatten()
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype(np.int16)

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(int(sample_rate))
        wav.writeframes(pcm.tobytes())
    return buffer.getvalue()

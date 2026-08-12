"""The contract a speech-to-text provider implements.

Callers hold audio as float32 samples in [-1, 1], which is what both the
listener and the dictation engine already have in hand. Encoding to
whatever a provider wants on the wire is the adapter's job.

``transcribe`` returns ``None`` rather than raising when it cannot produce
a transcript. Every caller has local Whisper behind it, so a hosted
provider failing must read as "use the local one", not as an error the
user sees. An empty string is a real answer meaning silence.
"""

from __future__ import annotations

import io
import wave
from abc import ABC, abstractmethod
from typing import Any, Optional


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

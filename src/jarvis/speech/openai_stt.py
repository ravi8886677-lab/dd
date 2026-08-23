"""Speech-to-text over an OpenAI-compatible transcriptions endpoint.

A multipart upload of a WAV and a model name, which is all the endpoint
shape requires. The reason it exists is latency: local ``whisper_model:
medium`` is the largest fixed cost in the voice loop, and a faster
recogniser answers in a fraction of the time.

The endpoint is whatever the user points ``stt_base_url`` at — a local
``whisper.cpp`` server, an in-house box, or a hosted service. There is no
default: an unset endpoint means unconfigured, so nothing reaches a third
party the user did not name.

This shape takes a complete audio file and returns a complete transcript.
Anything that wants to feel incremental has to segment the audio itself
and send the pieces, which is exactly what the listener's VAD already does.

Every failure path returns ``None`` so the caller drops to local Whisper.
A hosted recogniser being down must cost speed, never the feature.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..debug import debug_log
from .backend import SpeechToText, Transcription, pcm16_wav_bytes
from .languages import normalise_language

DEFAULT_MODEL = "whisper-large-v3-turbo"

# ``verbose_json`` costs the same as ``json`` and adds the detected
# language plus the per-segment ``avg_logprob`` and ``no_speech_prob``
# that the listener's hallucination filters run on. Requesting the plain
# shape would mean hosted audio skipping filters local audio must pass.
RESPONSE_FORMAT = "verbose_json"

# Below this there is nothing to recognise, and an upload costs a round
# trip to be told so. The listener's own floor is comparable.
MIN_AUDIO_SECONDS = 0.15


def _describe_status(status: int) -> str:
    """An HTTP status as something worth reading at a microphone.

    A wrong key and a rate limit both surface as "hosted recognition
    stopped working", and they need opposite responses from the user, so
    the two that are actionable are named. The number is always included:
    it is what makes the message searchable when the wording is not.
    """
    if status == 401 or status == 403:
        return f"HTTP {status}, the API key was rejected"
    if status == 429:
        return f"HTTP {status}, rate limited"
    return f"HTTP {status}"


class OpenAICompatibleSpeechToText(SpeechToText):
    """Transcribe through any OpenAI-compatible transcriptions endpoint."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = "",
    ) -> None:
        self._api_key = api_key
        self._model = model or DEFAULT_MODEL
        self._base_url = base_url.rstrip("/")
        # Why the last call returned ``None``, in a few words fit to show a
        # user. ``None`` is still the whole contract for *what* happened, so
        # nothing has to read this; the listener does, because "hosted
        # recognition is not working" is otherwise indistinguishable from a
        # slow assistant. Set on every exit path so it can never describe an
        # older failure than the one just returned.
        self.last_error: Optional[str] = None

    def transcribe(
        self,
        audio: Any,
        sample_rate: int = 16000,
        timeout_sec: float = 15.0,
    ) -> Optional[str]:
        result = self.transcribe_detailed(audio, sample_rate, timeout_sec)
        if result is None:
            return None
        return result.text

    def transcribe_detailed(
        self,
        audio: Any,
        sample_rate: int = 16000,
        timeout_sec: float = 15.0,
    ) -> Optional[Transcription]:
        self.last_error = None

        if not self._api_key:
            self.last_error = "no API key configured"
            return None

        try:
            import numpy as np

            samples = np.asarray(audio, dtype=np.float32).flatten()
        except Exception as exc:
            self.last_error = "unusable audio"
            debug_log(f"⚠️ hosted stt: unusable audio: {exc}", "whisper")
            return None

        if samples.size < int(MIN_AUDIO_SECONDS * sample_rate):
            return Transcription(text="")

        try:
            import requests

            wav = pcm16_wav_bytes(samples, sample_rate)
            response = requests.post(
                f"{self._base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                files={"file": ("audio.wav", wav, "audio/wav")},
                data={"model": self._model, "response_format": RESPONSE_FORMAT},
                timeout=timeout_sec,
            )
        except Exception as exc:
            # A timeout is the one worth naming precisely: it is the only
            # failure the user experiences as a pause rather than as an
            # error, and the setting that governs it is theirs to change.
            if type(exc).__name__ in ("Timeout", "ConnectTimeout", "ReadTimeout"):
                self.last_error = f"no response within {timeout_sec:g}s"
            else:
                self.last_error = f"could not reach {self._base_url or 'the endpoint'}"
            debug_log(f"⚠️ hosted stt: request failed, using local: {exc}", "whisper")
            return None

        if response.status_code != 200:
            # 429 and 401 are the ones worth reading in a log: a rate limit
            # and a bad key look identical from the user's side otherwise,
            # since both silently become local transcription.
            self.last_error = _describe_status(response.status_code)
            debug_log(
                f"⚠️ hosted stt: HTTP {response.status_code}, using local", "whisper"
            )
            return None

        try:
            payload = response.json()
            text = str(payload.get("text") or "").strip()
        except Exception as exc:
            self.last_error = "unreadable response"
            debug_log(f"⚠️ hosted stt: unreadable response: {exc}", "whisper")
            return None

        # Groq reports the language by display name ("English"), where local
        # Whisper reports the code ("en"). Downstream expects the code.
        language = normalise_language(payload.get("language"))

        segments: List[Dict[str, Any]] = []
        raw_segments = payload.get("segments")
        if isinstance(raw_segments, list):
            segments = [seg for seg in raw_segments if isinstance(seg, dict)]

        debug_log(
            f"🗣️ hosted stt: {len(text)} chars, {len(segments)} segments, "
            f"language={language or 'unknown'}",
            "whisper",
        )
        return Transcription(text=text, language=language, segments=tuple(segments))

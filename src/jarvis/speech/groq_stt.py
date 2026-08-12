"""Groq speech-to-text adapter.

Groq serves Whisper behind an OpenAI-compatible transcriptions endpoint, so
this is a multipart upload of a WAV and a model name. The whole reason it
exists is latency: local ``whisper_model: medium`` is the largest fixed
cost in the voice loop, and a hosted turbo model answers in a fraction of
the time.

Every failure path returns ``None`` so the caller drops to local Whisper.
A hosted recogniser being down must cost speed, never the feature.
"""

from __future__ import annotations

from typing import Any, Optional

from ..debug import debug_log
from .backend import SpeechToText, pcm16_wav_bytes

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "whisper-large-v3-turbo"

# Below this there is nothing to recognise, and an upload costs a round
# trip to be told so. The listener's own floor is comparable.
MIN_AUDIO_SECONDS = 0.15


class GroqSpeechToText(SpeechToText):
    """Transcribe through Groq's hosted Whisper."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = "",
    ) -> None:
        self._api_key = api_key
        self._model = model or DEFAULT_MODEL
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")

    def transcribe(
        self,
        audio: Any,
        sample_rate: int = 16000,
        timeout_sec: float = 15.0,
    ) -> Optional[str]:
        if not self._api_key:
            return None

        try:
            import numpy as np

            samples = np.asarray(audio, dtype=np.float32).flatten()
        except Exception as exc:
            debug_log(f"⚠️ groq stt: unusable audio: {exc}", "whisper")
            return None

        if samples.size < int(MIN_AUDIO_SECONDS * sample_rate):
            return ""

        try:
            import requests

            wav = pcm16_wav_bytes(samples, sample_rate)
            response = requests.post(
                f"{self._base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                files={"file": ("audio.wav", wav, "audio/wav")},
                data={"model": self._model, "response_format": "json"},
                timeout=timeout_sec,
            )
        except Exception as exc:
            debug_log(f"⚠️ groq stt: request failed, using local: {exc}", "whisper")
            return None

        if response.status_code != 200:
            # 429 and 401 are the ones worth reading in a log: a rate limit
            # and a bad key look identical from the user's side otherwise,
            # since both silently become local transcription.
            debug_log(
                f"⚠️ groq stt: HTTP {response.status_code}, using local", "whisper"
            )
            return None

        try:
            text = str(response.json().get("text") or "").strip()
        except Exception as exc:
            debug_log(f"⚠️ groq stt: unreadable response: {exc}", "whisper")
            return None

        debug_log(f"🗣️ groq stt: {len(text)} chars", "whisper")
        return text

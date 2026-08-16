"""Hosted speech-to-text, and the fallback that must always survive it.

Local Whisper is the floor: every caller has it, and the hosted provider
exists only to be faster. So the contract that matters is not that Groq
works, it is that Groq failing is indistinguishable from never having been
configured. `None` means fall back; `""` means the audio held no speech.
"""

from __future__ import annotations

import io
import wave

import numpy as np
import pytest

from jarvis.speech.backend import pcm16_wav_bytes
from jarvis.speech.factory import get_stt_backend, resolve_stt_provider
from jarvis.speech.openai_stt import OpenAICompatibleSpeechToText


class _Cfg:
    def __init__(self, **kw):
        self.stt_provider = kw.get("provider", "local")
        self.stt_api_key = kw.get("api_key", "")
        self.stt_model = kw.get("model", "")
        self.stt_base_url = kw.get("base_url", "")


# ── WAV encoding ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_encoded_audio_is_a_mono_16bit_wav_at_the_given_rate():
    """Providers reject raw samples; the container has to be right."""
    data = pcm16_wav_bytes(np.zeros(1600, dtype=np.float32), 16000)

    with wave.open(io.BytesIO(data), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 16000
        assert wav.getnframes() == 1600


@pytest.mark.unit
def test_full_scale_samples_survive_encoding():
    data = pcm16_wav_bytes(np.array([1.0, -1.0, 0.0], dtype=np.float32), 16000)

    with wave.open(io.BytesIO(data), "rb") as wav:
        pcm = np.frombuffer(wav.readframes(3), dtype=np.int16)
    assert pcm[0] == 32767
    assert pcm[1] == -32767
    assert pcm[2] == 0


@pytest.mark.unit
def test_out_of_range_samples_clip_instead_of_wrapping():
    """Wrapping turns a loud passage into noise the recogniser cannot read.

    Overflow in int16 flips the sign, so a sample slightly above full
    scale becomes maximally negative — audible as a crack, and it destroys
    the word it lands on.
    """
    data = pcm16_wav_bytes(np.array([2.5, -2.5], dtype=np.float32), 16000)

    with wave.open(io.BytesIO(data), "rb") as wav:
        pcm = np.frombuffer(wav.readframes(2), dtype=np.int16)
    assert pcm[0] == 32767
    assert pcm[1] == -32767


@pytest.mark.unit
def test_the_sample_rate_is_recorded_not_assumed():
    """A rate mismatch plays speech at the wrong pitch and ruins accuracy."""
    with wave.open(io.BytesIO(pcm16_wav_bytes(np.zeros(10, np.float32), 24000)), "rb") as wav:
        assert wav.getframerate() == 24000


# ── Falling back ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_no_key_means_no_hosted_backend_rather_than_a_broken_one():
    assert get_stt_backend(_Cfg(provider="groq", api_key="")) is None


@pytest.mark.unit
def test_local_provider_means_no_hosted_backend():
    assert get_stt_backend(_Cfg(provider="local", api_key="k")) is None


@pytest.mark.unit
def test_an_unknown_provider_falls_back_to_local():
    assert resolve_stt_provider("nonsense") == "local"
    assert get_stt_backend(_Cfg(provider="nonsense", api_key="k")) is None


@pytest.mark.unit
def test_a_key_without_an_endpoint_is_not_enough():
    """No endpoint is assumed, so a key alone cannot reach anywhere."""
    assert get_stt_backend(_Cfg(provider="openai_compatible", api_key="k")) is None


@pytest.mark.unit
def test_an_endpoint_and_a_key_produce_a_backend():
    cfg = _Cfg(provider="openai_compatible", api_key="k", base_url="http://localhost:8080/v1")
    assert isinstance(get_stt_backend(cfg), OpenAICompatibleSpeechToText)


# ── Transcription outcomes ────────────────────────────────────────────


@pytest.mark.unit
def test_a_transport_failure_returns_none_so_the_caller_uses_whisper(monkeypatch):
    def _boom(*a, **kw):
        raise ConnectionError("network down")

    monkeypatch.setattr("requests.post", _boom)
    stt = OpenAICompatibleSpeechToText(api_key="k")

    assert stt.transcribe(np.zeros(16000, dtype=np.float32)) is None


@pytest.mark.unit
@pytest.mark.parametrize("status", [401, 429, 500])
def test_an_error_status_returns_none_rather_than_an_empty_transcript(monkeypatch, status):
    """An empty string would read as silence and lose the utterance."""

    class _Resp:
        status_code = status

        def json(self):
            return {}

    monkeypatch.setattr("requests.post", lambda *a, **kw: _Resp())
    stt = OpenAICompatibleSpeechToText(api_key="k")

    assert stt.transcribe(np.zeros(16000, dtype=np.float32)) is None


@pytest.mark.unit
def test_a_successful_response_returns_the_transcript(monkeypatch):
    class _Resp:
        status_code = 200

        def json(self):
            return {"text": "  hello jarvis  "}

    monkeypatch.setattr("requests.post", lambda *a, **kw: _Resp())
    stt = OpenAICompatibleSpeechToText(api_key="k")

    assert stt.transcribe(np.zeros(16000, dtype=np.float32)) == "hello jarvis"


@pytest.mark.unit
def test_audio_too_short_to_hold_speech_is_not_uploaded(monkeypatch):
    """A round trip to be told there was nothing is pure added latency."""
    called = []
    monkeypatch.setattr("requests.post", lambda *a, **kw: called.append(1))
    stt = OpenAICompatibleSpeechToText(api_key="k")

    assert stt.transcribe(np.zeros(100, dtype=np.float32)) == ""
    assert not called


@pytest.mark.unit
def test_the_request_carries_the_model_and_a_wav_file(monkeypatch):
    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"text": "ok"}

    def _capture(url, **kw):
        seen["url"] = url
        seen["data"] = kw.get("data")
        seen["files"] = kw.get("files")
        seen["headers"] = kw.get("headers")
        return _Resp()

    monkeypatch.setattr("requests.post", _capture)
    OpenAICompatibleSpeechToText(api_key="secret", model="whisper-large-v3-turbo").transcribe(
        np.zeros(16000, dtype=np.float32)
    )

    assert seen["url"].endswith("/audio/transcriptions")
    assert seen["data"]["model"] == "whisper-large-v3-turbo"
    assert seen["headers"]["Authorization"] == "Bearer secret"
    assert seen["files"]["file"][2] == "audio/wav"
    assert seen["files"]["file"][1][:4] == b"RIFF"

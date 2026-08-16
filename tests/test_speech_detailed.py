"""The richer transcription shape, and the filtering it exists to enable.

Local Whisper transcripts do not reach the intent judge raw: segments are
dropped by `no_speech_prob` and by `avg_logprob` first, which is what keeps
a radio jingle from being heard as a command. A hosted provider that
returned only a string would route around that gate entirely — the audio
would be judged on text nobody vetted.

So these tests are mostly about parity. The interesting assertions are not
"Groq parses" but "hosted audio faces the same filters as local audio", and
"a provider that cannot supply segments is treated as unvetted rather than
as clean".
"""

from __future__ import annotations

import numpy as np
import pytest

from jarvis.speech.backend import SpeechToText, Transcription
from jarvis.speech.openai_stt import OpenAICompatibleSpeechToText
from jarvis.speech.languages import normalise_language


# ── Language normalisation ────────────────────────────────────────────


@pytest.mark.unit
def test_a_display_name_becomes_the_code_the_app_uses():
    """Groq says "English" where local Whisper says "en" for one utterance."""
    assert normalise_language("English") == "en"
    assert normalise_language("Spanish") == "es"
    assert normalise_language("Haitian Creole") == "ht"


@pytest.mark.unit
def test_a_code_passes_through_untouched():
    """The local backend already reports codes; it must survive the trip."""
    assert normalise_language("en") == "en"
    assert normalise_language("yue") == "yue"


@pytest.mark.unit
def test_whisper_alias_spellings_resolve():
    """Whisper accepts several names per language and can emit any of them."""
    assert normalise_language("Mandarin") == "zh"
    assert normalise_language("Castilian") == "es"
    assert normalise_language("Burmese") == "my"


@pytest.mark.unit
def test_an_unknown_name_is_none_rather_than_a_guess():
    """A wrong code silently picks wrong locales downstream; None does not."""
    assert normalise_language("Klingon") is None
    assert normalise_language("") is None
    assert normalise_language(None) is None


# ── The default detailed implementation ───────────────────────────────


class _TextOnly(SpeechToText):
    """A provider written before the richer shape existed."""

    def __init__(self, answer):
        self.answer = answer

    def transcribe(self, audio, sample_rate=16000, timeout_sec=15.0):
        return self.answer


@pytest.mark.unit
def test_a_text_only_provider_still_satisfies_the_detailed_interface():
    result = _TextOnly("hello there").transcribe_detailed(np.zeros(16000))

    assert isinstance(result, Transcription)
    assert result.text == "hello there"
    assert result.language is None
    assert list(result.segments) == []


@pytest.mark.unit
def test_the_fall_back_signal_survives_the_wrapper():
    """None must not become Transcription(text="None") on the way through."""
    assert _TextOnly(None).transcribe_detailed(np.zeros(16000)) is None


# ── Groq's verbose_json parsing ───────────────────────────────────────


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _patch_post(monkeypatch, response, captured=None):
    import requests

    def fake_post(url, **kwargs):
        if captured is not None:
            captured["url"] = url
            captured.update(kwargs)
        return response

    monkeypatch.setattr(requests, "post", fake_post)


@pytest.mark.unit
def test_the_adapter_asks_for_the_shape_that_carries_segments(monkeypatch):
    """Plain json would omit exactly the fields the filters need."""
    captured = {}
    _patch_post(monkeypatch, _Response(payload={"text": "hi"}), captured)

    OpenAICompatibleSpeechToText(api_key="k").transcribe_detailed(np.zeros(16000, dtype=np.float32))

    assert captured["data"]["response_format"] == "verbose_json"


@pytest.mark.unit
def test_language_and_segments_come_back_from_a_verbose_response(monkeypatch):
    payload = {
        "text": " what is the weather today?",
        "language": "English",
        "segments": [
            {"text": " what is the weather today?",
             "avg_logprob": -0.379, "no_speech_prob": 0.0},
        ],
    }
    _patch_post(monkeypatch, _Response(payload=payload))

    result = OpenAICompatibleSpeechToText(api_key="k").transcribe_detailed(
        np.zeros(16000, dtype=np.float32)
    )

    assert result.text == "what is the weather today?"
    assert result.language == "en"          # not "English"
    assert len(result.segments) == 1
    assert result.segments[0]["no_speech_prob"] == 0.0


@pytest.mark.unit
def test_a_response_without_segments_is_still_a_transcript(monkeypatch):
    """Not every provider or model breaks the audio down; text still counts."""
    _patch_post(monkeypatch, _Response(payload={"text": "hello"}))

    result = OpenAICompatibleSpeechToText(api_key="k").transcribe_detailed(
        np.zeros(16000, dtype=np.float32)
    )

    assert result.text == "hello"
    assert list(result.segments) == []
    assert result.language is None


@pytest.mark.unit
def test_a_non_200_still_means_fall_back(monkeypatch):
    """The richer shape must not have widened the failure surface."""
    _patch_post(monkeypatch, _Response(status_code=429))

    assert OpenAICompatibleSpeechToText(api_key="k").transcribe_detailed(
        np.zeros(16000, dtype=np.float32)
    ) is None


@pytest.mark.unit
def test_the_plain_text_method_still_works_through_the_detailed_one(monkeypatch):
    """Dictation calls `transcribe`; it must not have been broken by this."""
    _patch_post(monkeypatch, _Response(payload={"text": " hello ", "language": "English"}))

    assert OpenAICompatibleSpeechToText(api_key="k").transcribe(
        np.zeros(16000, dtype=np.float32)
    ) == "hello"


@pytest.mark.unit
def test_the_plain_text_method_propagates_fall_back(monkeypatch):
    _patch_post(monkeypatch, _Response(status_code=500))

    assert OpenAICompatibleSpeechToText(api_key="k").transcribe(
        np.zeros(16000, dtype=np.float32)
    ) is None


@pytest.mark.unit
def test_audio_too_short_to_hold_speech_is_silence_not_failure():
    """`""` means heard nothing, `None` means could not hear — different."""
    result = OpenAICompatibleSpeechToText(api_key="k").transcribe_detailed(
        np.zeros(10, dtype=np.float32)
    )

    assert result is not None
    assert result.text == ""

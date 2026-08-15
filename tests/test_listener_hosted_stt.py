"""The listener's hosted-recognition path, and the local floor beneath it.

Hosted speech recognition was wired into dictation but not into the voice
loop, so the setting existed and the microphone ignored it. These tests pin
the behaviour that closes that gap, and — more importantly — the behaviour
that must survive it: local Whisper still answers whenever the hosted
provider does not, for any reason at all.

The failure this guards against is not "Groq is down". It is a hosted
transcript reaching the intent judge without passing the confidence and
no-speech filters that every local transcript passes, because that turns a
privacy-and-latency feature into a silent accuracy regression.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from jarvis.speech.backend import Transcription


def _create_listener(**kwargs):
    """A VoiceListener with the heavy subsystems mocked out."""
    cfg = MagicMock()
    cfg.whisper_model = "base"
    cfg.whisper_device = "auto"
    cfg.whisper_compute_type = "int8"
    cfg.whisper_backend = "faster-whisper"
    cfg.sample_rate = 16000
    cfg.vad_enabled = False
    cfg.vad_aggressiveness = 2
    cfg.echo_tolerance = 0.3
    cfg.echo_energy_threshold = 2.0
    cfg.hot_window_seconds = 3.0
    cfg.hot_window_enabled = True
    cfg.voice_collect_seconds = 2.0
    cfg.voice_max_collect_seconds = 60.0
    cfg.voice_device = None
    cfg.voice_debug = False
    cfg.voice_min_energy = 0.0045
    cfg.tune_enabled = False
    cfg.wake_word = "jarvis"
    cfg.wake_aliases = []
    cfg.wake_fuzzy_ratio = 0.78
    cfg.stop_commands = ["stop"]
    cfg.transcript_buffer_duration_sec = 120.0
    cfg.whisper_min_confidence = kwargs.get("min_confidence", 0.3)
    cfg.whisper_no_speech_threshold = kwargs.get("no_speech_threshold", 0.5)

    with patch("jarvis.listening.listener.webrtcvad", None), \
         patch("jarvis.listening.listener.sd", None), \
         patch("jarvis.listening.listener.np", None), \
         patch("jarvis.listening.listener.create_intent_judge", return_value=None):
        from jarvis.listening.listener import VoiceListener
        listener = VoiceListener(MagicMock(), cfg, MagicMock(), MagicMock())

    return listener


def _install_backend(listener, backend):
    """Pin a resolved hosted backend, bypassing settings lookup."""
    listener._hosted_stt = backend
    listener._hosted_stt_resolved = True
    return backend


class _Backend:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def transcribe_detailed(self, audio, sample_rate=16000, timeout_sec=15.0):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


# ── Falling back to local ─────────────────────────────────────────────


@pytest.mark.unit
def test_no_hosted_provider_means_the_local_backend_answers():
    """The default configuration must not take a network detour."""
    listener = _create_listener()
    _install_backend(listener, None)

    assert listener._transcribe_hosted(object()) is None


@pytest.mark.unit
def test_a_provider_returning_none_falls_through_to_local():
    """None is the adapters' word for every failure: key, quota, network."""
    listener = _create_listener()
    _install_backend(listener, _Backend(None))

    assert listener._transcribe_hosted(object()) is None


@pytest.mark.unit
def test_a_provider_that_raises_falls_through_instead_of_killing_the_thread():
    """This runs on the audio thread; an escaped exception ends listening."""
    listener = _create_listener()
    _install_backend(listener, _Backend(RuntimeError("connection reset")))

    assert listener._transcribe_hosted(object()) is None


@pytest.mark.unit
def test_the_provider_is_resolved_once_not_per_utterance():
    """Resolution reads settings and logs; per-utterance would spam both."""
    listener = _create_listener()
    with patch("jarvis.speech.factory.get_stt_backend", return_value=None) as resolve:
        listener._get_hosted_stt()
        listener._get_hosted_stt()
        listener._get_hosted_stt()

    assert resolve.call_count == 1


# ── Parity with the local filters ─────────────────────────────────────


@pytest.mark.unit
def test_a_hosted_segment_flagged_as_non_speech_is_dropped():
    """A confident transcription of silence is the classic Whisper failure."""
    listener = _create_listener(no_speech_threshold=0.5)
    _install_backend(listener, _Backend(Transcription(
        text="thank you for watching",
        language="en",
        segments=[{"text": " thank you for watching",
                   "avg_logprob": -0.1, "no_speech_prob": 0.9}],
    )))

    assert listener._transcribe_hosted(object()) == ""


@pytest.mark.unit
def test_a_low_confidence_hosted_segment_is_dropped():
    listener = _create_listener(min_confidence=0.5)
    _install_backend(listener, _Backend(Transcription(
        text="mumble",
        segments=[{"text": " mumble", "avg_logprob": -0.9, "no_speech_prob": 0.0}],
    )))

    assert listener._transcribe_hosted(object()) == ""


@pytest.mark.unit
def test_a_confident_hosted_segment_survives():
    listener = _create_listener(min_confidence=0.3)
    _install_backend(listener, _Backend(Transcription(
        text="what is the weather today",
        segments=[{"text": " what is the weather today",
                   "avg_logprob": -0.2, "no_speech_prob": 0.01}],
    )))

    assert listener._transcribe_hosted(object()) == "what is the weather today"


@pytest.mark.unit
def test_only_the_noisy_segments_are_dropped_from_a_mixed_utterance():
    """Rejecting the whole utterance for one bad segment loses real speech."""
    listener = _create_listener(min_confidence=0.3, no_speech_threshold=0.5)
    _install_backend(listener, _Backend(Transcription(
        text="ignored",
        segments=[
            {"text": " set a timer", "avg_logprob": -0.1, "no_speech_prob": 0.0},
            {"text": " subscribe now", "avg_logprob": -0.1, "no_speech_prob": 0.95},
            {"text": " for ten minutes", "avg_logprob": -0.1, "no_speech_prob": 0.0},
        ],
    )))

    assert listener._transcribe_hosted(object()) == "set a timer for ten minutes"


@pytest.mark.unit
def test_a_provider_without_segments_yields_its_text_unfiltered():
    """No segments is no basis to filter — not a clean bill of health."""
    listener = _create_listener()
    _install_backend(listener, _Backend(Transcription(text="  hello there  ")))

    assert listener._transcribe_hosted(object()) == "hello there"


# ── Language ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_a_detected_language_reaches_the_field_the_reply_engine_reads():
    listener = _create_listener()
    _install_backend(listener, _Backend(Transcription(
        text="bonjour", language="fr",
        segments=[{"text": " bonjour", "avg_logprob": -0.1, "no_speech_prob": 0.0}],
    )))

    listener._transcribe_hosted(object())

    assert listener._last_detected_language == "fr"


@pytest.mark.unit
def test_an_unknown_language_leaves_the_previous_one_alone():
    """Clobbering a known language with None is worse than keeping it."""
    listener = _create_listener()
    listener._last_detected_language = "de"
    _install_backend(listener, _Backend(Transcription(text="hallo", language=None)))

    listener._transcribe_hosted(object())

    assert listener._last_detected_language == "de"

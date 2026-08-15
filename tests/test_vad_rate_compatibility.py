"""The listener must never go deaf, whatever the sound card does.

A device that rejects 16 kHz sends the listener down the native-rate path,
where the stream runs at something like 44.1 kHz. webrtcvad accepts only
8/16/32/48 kHz and only 10/20/30 ms frames, so `is_speech` raises on every
frame — and the old handler answered "not speech" and carried on. Jarvis
came up, printed "Listening!", and ignored the user permanently.

The fix is not to make the VAD work at 44.1 kHz. It is to notice, say so,
and fall back to the energy threshold that already exists for the case where
webrtcvad is not installed at all. These tests pin that: a degraded detector
is acceptable, a silent one is not.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from jarvis.listening.listener import vad_accepts_frame


# ── The compatibility predicate ───────────────────────────────────────


@pytest.mark.unit
def test_the_rates_webrtcvad_actually_supports_are_accepted():
    for rate in (8000, 16000, 32000, 48000):
        assert vad_accepts_frame(rate, int(rate * 0.02)) is True


@pytest.mark.unit
def test_a_native_device_rate_is_rejected():
    """44.1 kHz is the common one, and the reason this bug existed."""
    assert vad_accepts_frame(44100, 882) is False
    assert vad_accepts_frame(22050, 441) is False


@pytest.mark.unit
def test_all_three_supported_frame_lengths_are_accepted():
    for ms in (10, 20, 30):
        assert vad_accepts_frame(16000, int(16000 * ms / 1000)) is True


@pytest.mark.unit
def test_a_frame_of_the_wrong_length_is_rejected_even_at_a_good_rate():
    """The rate can be fine while the slice size is not — both must hold."""
    assert vad_accepts_frame(16000, 320 + 1) is False
    assert vad_accepts_frame(16000, int(16000 * 0.025)) is False   # 25 ms
    assert vad_accepts_frame(16000, 0) is False


# ── The listener's behaviour when the VAD cannot be used ──────────────


def _create_listener(vad):
    cfg = MagicMock()
    cfg.sample_rate = 16000
    cfg.vad_enabled = True
    cfg.vad_aggressiveness = 2
    cfg.voice_min_energy = 0.01
    cfg.voice_debug = False
    cfg.hot_window_seconds = 3.0
    cfg.hot_window_enabled = True
    cfg.echo_tolerance = 0.3
    cfg.echo_energy_threshold = 2.0
    cfg.voice_collect_seconds = 2.0
    cfg.voice_max_collect_seconds = 60.0
    cfg.transcript_buffer_duration_sec = 120.0
    cfg.wake_word = "jarvis"
    cfg.wake_aliases = []

    with patch("jarvis.listening.listener.webrtcvad", None), \
         patch("jarvis.listening.listener.sd", None), \
         patch("jarvis.listening.listener.create_intent_judge", return_value=None):
        from jarvis.listening.listener import VoiceListener
        listener = VoiceListener(MagicMock(), cfg, MagicMock(), MagicMock())

    listener._vad = vad
    listener._vad_checked = False
    return listener


@pytest.fixture(autouse=True)
def _real_numpy_in_the_listener():
    """The module sets `np = None` when PortAudio is missing.

    `import sounddevice`, `import webrtcvad` and `import numpy` share one
    try block, so a machine with no PortAudio loses numpy too — which is
    exactly this container. These tests are about frame arithmetic, so they
    need the real thing regardless of the sound stack.
    """
    with patch("jarvis.listening.listener.np", np):
        yield


def _loud(samples):
    return np.full(samples, 0.5, dtype=np.float32)


def _quiet(samples):
    return np.full(samples, 0.0001, dtype=np.float32)


@pytest.mark.unit
def test_an_unsupported_stream_rate_falls_back_instead_of_going_deaf():
    """The regression itself: loud speech at 44.1 kHz must still register."""
    vad = MagicMock()
    listener = _create_listener(vad)
    listener._stream_samplerate = 44100

    assert listener._is_speech_frame(_loud(320)) is True
    assert listener._vad is None          # disabled, not left raising
    vad.is_speech.assert_not_called()      # never even asked


@pytest.mark.unit
def test_the_fallback_still_discriminates_rather_than_passing_everything():
    """Falling back must mean a cruder gate, not no gate at all."""
    listener = _create_listener(MagicMock())
    listener._stream_samplerate = 44100

    assert listener._is_speech_frame(_loud(320)) is True
    assert listener._is_speech_frame(_quiet(320)) is False


@pytest.mark.unit
def test_a_detector_that_raises_is_disabled_rather_than_believed():
    """An error on frame one recurs on every frame; do not answer 'silence'."""
    vad = MagicMock()
    vad.is_speech.side_effect = RuntimeError("invalid frame length")
    listener = _create_listener(vad)
    listener._stream_samplerate = 16000    # passes the pre-check

    assert listener._is_speech_frame(_loud(320)) is True
    assert listener._vad is None


@pytest.mark.unit
def test_a_supported_stream_keeps_using_the_real_detector():
    """The fix must not quietly demote every user to the energy threshold."""
    vad = MagicMock()
    vad.is_speech.return_value = False
    listener = _create_listener(vad)
    listener._stream_samplerate = 16000

    # Loud audio the VAD calls non-speech stays non-speech: proof the VAD,
    # not the energy threshold, is deciding.
    assert listener._is_speech_frame(_loud(320)) is False
    assert listener._vad is vad
    assert vad.is_speech.called


@pytest.mark.unit
def test_the_compatibility_check_runs_once_not_per_frame():
    vad = MagicMock()
    vad.is_speech.return_value = True
    listener = _create_listener(vad)
    listener._stream_samplerate = 16000

    for _ in range(5):
        listener._is_speech_frame(_loud(320))

    assert listener._vad_checked is True
    assert vad.is_speech.call_count == 5

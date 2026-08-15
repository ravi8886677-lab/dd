"""Listening while speaking, and the floor underneath it.

The microphone hears the speaker. Everything the listener does about that
today — fuzzy-matching a transcript against what was just said, energy
baselines, a shortened utterance window during TTS — is a text-level
workaround for a signal-level problem, and none of it can work while both
parties talk at once.

The fix is to subtract the speaker's output from the microphone before
anything looks at the audio. So these tests are mostly about two things:
that the subtraction happens on the audio every consumer sees, and that
every way it can fail lands on today's behaviour rather than on silence.
The second matters more. A canceller that removes nothing costs duplex; a
canceller that removes the wrong thing eats the user's speech.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from jarvis.audio.calibration import (
    CalibrationResult,
    delay_to_samples,
    generate_chirp,
    measure_delay,
)
from jarvis.audio.echo_cancel import (
    NullEchoCanceller,
    create_echo_canceller,
)
from jarvis.audio.reference_buffer import ReferenceBuffer, publish_playback


# ── The reference buffer ──────────────────────────────────────────────


@pytest.mark.unit
def test_the_newest_audio_comes_back_at_zero_delay():
    buffer = ReferenceBuffer(sample_rate=16000, history_sec=1.0)
    buffer.write(np.linspace(0.0, 1.0, 1000, dtype=np.float32))

    frame = buffer.read_aligned(160, 0)

    assert frame is not None
    assert frame.shape == (160,)
    assert frame[-1] == pytest.approx(1.0, abs=1e-3)


@pytest.mark.unit
def test_a_delay_fetches_what_was_playing_that_long_ago():
    buffer = ReferenceBuffer(sample_rate=16000, history_sec=1.0)
    marked = np.zeros(1000, dtype=np.float32)
    marked[300:460] = 0.75          # a burst 540 samples before the end
    buffer.write(marked)

    frame = buffer.read_aligned(160, 540)

    assert frame is not None
    assert np.allclose(frame, 0.75)


@pytest.mark.unit
def test_asking_before_enough_has_played_returns_nothing():
    """Silence would teach the adaptive filter that echoes do not exist."""
    buffer = ReferenceBuffer(sample_rate=16000, history_sec=1.0)
    buffer.write(np.ones(100, dtype=np.float32))

    assert buffer.read_aligned(160, 16000) is None
    assert buffer.read_aligned(160, 0) is None


@pytest.mark.unit
def test_a_delay_longer_than_the_history_is_refused():
    buffer = ReferenceBuffer(sample_rate=16000, history_sec=0.5)
    buffer.write(np.ones(8000, dtype=np.float32))

    assert buffer.read_aligned(160, 16000) is None


@pytest.mark.unit
def test_reads_stay_correct_after_the_ring_wraps():
    """Playback runs for minutes; the ring wraps constantly."""
    buffer = ReferenceBuffer(sample_rate=16000, history_sec=0.1)   # 1600 samples
    for value in (0.1, 0.2, 0.3, 0.4):
        buffer.write(np.full(1000, value, dtype=np.float32))

    frame = buffer.read_aligned(160, 0)

    assert frame is not None
    assert np.allclose(frame, 0.4)


@pytest.mark.unit
def test_clearing_forgets_everything():
    """Stale reference is worse than none once playback has stopped."""
    buffer = ReferenceBuffer(sample_rate=16000, history_sec=1.0)
    buffer.write(np.ones(2000, dtype=np.float32))

    buffer.clear()

    assert buffer.read_aligned(160, 0) is None


@pytest.mark.unit
def test_integer_pcm_is_scaled_not_taken_literally():
    """int16 straight through would be a reference 32768 times too loud."""
    from jarvis.audio import reference_buffer as rb

    buffer = ReferenceBuffer(sample_rate=16000, history_sec=1.0)
    with patch.object(rb, "playback_reference", buffer), \
         patch.object(rb, "_publishing", True):
        publish_playback(np.full(1000, 16384, dtype=np.int16), 16000)

    frame = buffer.read_aligned(160, 0)
    assert frame is not None
    assert frame.max() == pytest.approx(0.5, abs=0.01)


@pytest.mark.unit
def test_playback_at_another_rate_is_resampled_to_the_reference_rate():
    """Piper plays at 22050; the microphone runs at 16000."""
    from jarvis.audio import reference_buffer as rb

    buffer = ReferenceBuffer(sample_rate=16000, history_sec=1.0)
    with patch.object(rb, "playback_reference", buffer), \
         patch.object(rb, "_publishing", True):
        publish_playback(np.ones(22050, dtype=np.float32), 22050)

    # One second of audio at 22050 Hz should land as ~16000 samples.
    assert buffer.written == pytest.approx(16000, rel=0.01)


@pytest.mark.unit
def test_publishing_never_raises_whatever_it_is_handed():
    """This runs inside an audio callback, where an exception kills playback."""
    publish_playback(None, 16000)
    publish_playback([], 16000)
    publish_playback("not audio", 16000)


# ── Calibration ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_a_known_delay_is_measured_back():
    sample_rate = 16000
    chirp = generate_chirp(sample_rate, 0.5)
    delay = int(sample_rate * 0.120)

    mic = np.zeros(len(chirp) + delay + 4000, dtype=np.float32)
    mic[delay:delay + len(chirp)] += chirp * 0.6
    mic += np.random.default_rng(0).normal(0, 0.01, mic.shape).astype(np.float32)

    result = measure_delay(chirp, mic, sample_rate)

    assert result.usable
    assert result.delay_ms == pytest.approx(120.0, abs=2.0)


@pytest.mark.unit
def test_a_bluetooth_scale_delay_is_still_found():
    """Wireless output is the case that makes this worth measuring."""
    sample_rate = 16000
    chirp = generate_chirp(sample_rate, 0.5)
    delay = int(sample_rate * 0.280)

    mic = np.zeros(len(chirp) + delay + 4000, dtype=np.float32)
    mic[delay:delay + len(chirp)] += chirp * 0.4

    result = measure_delay(chirp, mic, sample_rate)

    assert result.usable
    assert result.delay_ms == pytest.approx(280.0, abs=2.0)


@pytest.mark.unit
def test_a_silent_microphone_fails_loudly_rather_than_guessing():
    """Muted output or the wrong input device must not produce a number."""
    sample_rate = 16000
    chirp = generate_chirp(sample_rate, 0.5)
    silence = np.zeros(len(chirp) + 8000, dtype=np.float32)

    result = measure_delay(chirp, silence, sample_rate)

    assert not result.ok
    assert not result.usable


@pytest.mark.unit
def test_noise_with_no_echo_in_it_is_not_mistaken_for_one():
    """A peak always exists; confidence is what says whether it means anything."""
    sample_rate = 16000
    chirp = generate_chirp(sample_rate, 0.5)
    noise = np.random.default_rng(1).normal(0, 0.05, len(chirp) + 8000).astype(np.float32)

    result = measure_delay(chirp, noise, sample_rate)

    assert not result.usable


@pytest.mark.unit
def test_a_recording_shorter_than_the_chirp_is_rejected():
    sample_rate = 16000
    chirp = generate_chirp(sample_rate, 0.5)

    result = measure_delay(chirp, chirp[:100], sample_rate)

    assert not result.ok


@pytest.mark.unit
def test_the_chirp_starts_and_ends_quietly():
    """An abrupt edge is a click, and a click correlates with anything."""
    chirp = generate_chirp(16000, 0.5)

    assert abs(float(chirp[0])) < 0.01
    assert abs(float(chirp[-1])) < 0.01
    assert float(np.abs(chirp).max()) > 0.1


@pytest.mark.unit
def test_a_measured_delay_converts_to_a_buffer_offset():
    assert delay_to_samples(120.0, 16000) == 1920
    assert delay_to_samples(0.0, 16000) == 0
    assert delay_to_samples(-5.0, 16000) == 0


# ── Choosing a canceller ──────────────────────────────────────────────


class _Cfg:
    def __init__(self, **kw):
        self.aec_enabled = kw.get("enabled", False)
        self.aec_delay_ms = kw.get("delay_ms", 0.0)
        self.aec_frame_ms = kw.get("frame_ms", 10)
        self.aec_filter_ms = kw.get("filter_ms", 200)


@pytest.mark.unit
def test_the_default_configuration_cancels_nothing():
    canceller = create_echo_canceller(_Cfg(), 16000)

    assert not canceller.active


@pytest.mark.unit
def test_a_missing_library_falls_back_instead_of_failing_startup():
    """Voice input must not depend on a wheel loading in a frozen build."""
    with patch("jarvis.audio.echo_cancel.PyAecEchoCanceller",
               side_effect=ImportError("no pyaec")):
        canceller = create_echo_canceller(_Cfg(enabled=True), 16000)

    assert not canceller.active


@pytest.mark.unit
def test_a_canceller_that_will_not_construct_falls_back_too():
    with patch("jarvis.audio.echo_cancel.PyAecEchoCanceller",
               side_effect=RuntimeError("bad frame size")):
        canceller = create_echo_canceller(_Cfg(enabled=True), 16000)

    assert not canceller.active


@pytest.mark.unit
def test_the_null_canceller_returns_the_microphone_untouched():
    frame = np.linspace(-0.5, 0.5, 160, dtype=np.float32)

    assert np.array_equal(NullEchoCanceller().cancel(frame, None), frame)


# ── The listener's use of it ──────────────────────────────────────────


def _create_listener(canceller, delay_samples=0):
    cfg = MagicMock()
    cfg.sample_rate = 16000
    cfg.vad_enabled = False
    cfg.voice_min_energy = 0.01
    cfg.voice_debug = False
    cfg.aec_enabled = False
    cfg.aec_delay_ms = 0.0
    cfg.hot_window_seconds = 3.0
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

    listener._echo_canceller = canceller
    listener._aec_delay_samples = delay_samples
    return listener


class _Subtracting:
    """A canceller that simply subtracts, so the wiring is what is tested."""

    frame_samples = 160
    active = True

    def __init__(self):
        self.calls = 0

    def cancel(self, near, far):
        self.calls += 1
        return np.asarray(near) - np.asarray(far)


@pytest.fixture(autouse=True)
def _real_numpy_in_the_listener():
    """The listener sets `np = None` when PortAudio is missing."""
    with patch("jarvis.listening.listener.np", np):
        yield


@pytest.mark.unit
def test_the_speakers_output_is_removed_from_the_microphone():
    """The regression this whole feature is: hearing your own voice."""
    from jarvis.audio import reference_buffer as rb

    buffer = ReferenceBuffer(sample_rate=16000, history_sec=1.0)
    own_voice = np.full(1600, 0.4, dtype=np.float32)
    buffer.write(own_voice)

    listener = _create_listener(_Subtracting())
    user = np.full(160, 0.1, dtype=np.float32)
    mic = user + 0.4                       # user plus our own speaker

    with patch.object(rb, "playback_reference", buffer), \
         patch("jarvis.listening.listener.playback_reference", buffer):
        cleaned = listener._cancel_own_voice(mic)

    assert np.allclose(cleaned, user, atol=1e-5)


@pytest.mark.unit
def test_nothing_playing_leaves_the_microphone_alone():
    """No reference means no echo to remove, not a reason to alter audio."""
    from jarvis.audio import reference_buffer as rb

    empty = ReferenceBuffer(sample_rate=16000, history_sec=1.0)
    listener = _create_listener(_Subtracting())
    mic = np.full(160, 0.2, dtype=np.float32)

    with patch.object(rb, "playback_reference", empty), \
         patch("jarvis.listening.listener.playback_reference", empty):
        assert np.array_equal(listener._cancel_own_voice(mic), mic)


@pytest.mark.unit
def test_an_inactive_canceller_is_a_pass_through():
    listener = _create_listener(NullEchoCanceller())
    mic = np.full(160, 0.2, dtype=np.float32)

    assert np.array_equal(listener._cancel_own_voice(mic), mic)


@pytest.mark.unit
def test_the_canceller_is_built_for_the_frame_size_the_listener_produces():
    """The bug this replaces: the feature shipped declining every frame.

    The capture loop slices `vad_frame_ms` frames; the canceller was built
    from a separate `aec_frame_ms`. With the shipped defaults those were 20
    and 10 ms, so the size guard rejected every single frame while startup
    printed that cancellation was active. Nothing in the library requires
    10 ms — this test pins the two to the same source of truth.
    """
    from jarvis.audio.echo_cancel import PyAecEchoCanceller, create_echo_canceller

    pytest.importorskip("pyaec")
    cfg = _Cfg(enabled=True)
    cfg.vad_frame_ms = 20

    canceller = create_echo_canceller(cfg, 16000, frame_ms=20)

    assert isinstance(canceller, PyAecEchoCanceller)
    assert canceller.frame_samples == 320       # what a 20 ms listener frame is


@pytest.mark.unit
def test_a_frame_size_mismatch_declines_rather_than_misaligning():
    """A real mismatch must still decline: subtracting misaligned audio is
    worse than subtracting nothing."""
    from jarvis.audio import reference_buffer as rb

    buffer = ReferenceBuffer(sample_rate=16000, history_sec=1.0)
    buffer.write(np.full(1600, 0.4, dtype=np.float32))

    canceller = _Subtracting()                  # wants 160
    listener = _create_listener(canceller)
    mic = np.full(240, 0.5, dtype=np.float32)   # neither 160 nor a multiple

    with patch.object(rb, "playback_reference", buffer), \
         patch("jarvis.listening.listener.playback_reference", buffer):
        result = listener._cancel_own_voice(mic)

    assert np.array_equal(result, mic)
    assert canceller.calls == 0


@pytest.mark.unit
def test_a_microphone_at_another_rate_declines():
    """On the native-rate fallback, frames are 44.1 kHz and the reference is
    16 kHz. Cancelling across that removes the user, not the echo."""
    from jarvis.audio import reference_buffer as rb

    buffer = ReferenceBuffer(sample_rate=16000, history_sec=1.0)
    buffer.write(np.full(1600, 0.4, dtype=np.float32))

    canceller = _Subtracting()
    listener = _create_listener(canceller)
    listener._stream_samplerate = 44100
    mic = np.full(160, 0.5, dtype=np.float32)

    with patch.object(rb, "playback_reference", buffer), \
         patch("jarvis.listening.listener.playback_reference", buffer):
        result = listener._cancel_own_voice(mic)

    assert np.array_equal(result, mic)
    assert canceller.calls == 0


@pytest.mark.unit
def test_the_configured_delay_selects_which_audio_is_subtracted():
    """Alignment is the whole game: the wrong offset removes the wrong sound."""
    from jarvis.audio import reference_buffer as rb

    buffer = ReferenceBuffer(sample_rate=16000, history_sec=1.0)
    played = np.zeros(1600, dtype=np.float32)
    played[1280:1440] = 0.3                 # 160 samples, 160 from the end
    buffer.write(played)

    listener = _create_listener(_Subtracting(), delay_samples=160)
    mic = np.full(160, 0.3, dtype=np.float32)

    with patch.object(rb, "playback_reference", buffer), \
         patch("jarvis.listening.listener.playback_reference", buffer):
        cleaned = listener._cancel_own_voice(mic)

    assert np.allclose(cleaned, 0.0, atol=1e-6)


# ── The real library ──────────────────────────────────────────────────


@pytest.mark.unit
def test_the_real_canceller_actually_removes_an_echo():
    """Everything above tests wiring with a fake. This tests the thing itself.

    A perfect subtraction is not the bar — the filter adapts, so early
    frames leak. The bar is that a microphone carrying mostly our own voice
    comes out substantially quieter than it went in.
    """
    pytest.importorskip("pyaec")
    from jarvis.audio.echo_cancel import PyAecEchoCanceller

    canceller = PyAecEchoCanceller(sample_rate=16000, frame_ms=10, filter_ms=100)
    rng = np.random.default_rng(7)
    frames = 150
    far_in, near_in, out = [], [], []

    for _ in range(frames):
        far = rng.normal(0, 0.25, canceller.frame_samples).astype(np.float32)
        near = (far * 0.8).astype(np.float32)          # our voice, via the room
        far_in.append(far)
        near_in.append(near)
        out.append(canceller.cancel(near, far))

    # Judge on the second half, once the filter has had time to converge.
    half = frames // 2
    before = float(np.sqrt(np.mean(np.concatenate(near_in[half:]) ** 2)))
    after = float(np.sqrt(np.mean(np.concatenate(out[half:]) ** 2)))

    assert after < before * 0.5, f"only {before:.4f} -> {after:.4f}"


@pytest.mark.unit
def test_double_talk_attenuates_the_user_but_does_not_erase_them():
    """The measured limit of this canceller, pinned so it cannot worsen.

    Speaking over Jarvis is the case duplex exists for, and it is the case
    this library handles worst. Its suppression comes almost entirely from
    the residual-echo preprocessor rather than the adaptive filter — with
    the preprocessor off, measured echo suppression is nil — and that
    preprocessor ducks whatever arrives while the far end is active,
    including the user.

    Measured here: roughly 40% of the user's level survives double talk.
    Enough for Whisper, not transparent. The threshold is set below that so
    the test guards the floor rather than the exact figure, and a future
    canceller with a real double-talk detector should raise it.
    """
    pytest.importorskip("pyaec")
    from jarvis.audio.echo_cancel import PyAecEchoCanceller

    canceller = PyAecEchoCanceller(sample_rate=16000, frame_ms=10, filter_ms=100)
    rng = np.random.default_rng(11)
    n = canceller.frame_samples
    t = np.arange(n) / 16000.0
    user = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    user_rms = float(np.sqrt(np.mean(user ** 2)))

    for _ in range(200):                       # converge on echo alone
        far = rng.normal(0, 0.25, n).astype(np.float32)
        canceller.cancel((far * 0.8).astype(np.float32), far)

    out = []
    for _ in range(50):                        # then talk over it
        far = rng.normal(0, 0.25, n).astype(np.float32)
        out.append(canceller.cancel((far * 0.8 + user).astype(np.float32), far))
    survived = float(np.sqrt(np.mean(np.concatenate(out) ** 2)))

    assert survived > user_rms * 0.25, (
        f"user speech nearly erased: {user_rms:.4f} -> {survived:.4f}"
    )



# ── Regressions found by review ───────────────────────────────────────


@pytest.mark.unit
def test_a_reference_goes_stale_once_playback_stops():
    """The worst of the review findings: without this the buffer claims to
    know what was playing forever, so the canceller spends the rest of the
    session subtracting a finished sentence from live speech — which
    silences the user, not the echo."""
    buffer = ReferenceBuffer(sample_rate=16000, history_sec=1.0)
    buffer.write(np.ones(4000, dtype=np.float32))
    assert buffer.read_aligned(160, 0) is not None

    # Age the newest sample, as happens the moment Jarvis stops talking.
    buffer._last_write_time -= 5.0

    assert buffer.read_aligned(160, 0) is None


@pytest.mark.unit
def test_playback_is_not_captured_while_cancellation_is_off():
    """This runs in an output callback. With the feature off nothing reads
    the result, so converting and resampling there is pure waste."""
    from jarvis.audio import reference_buffer as rb

    buffer = ReferenceBuffer(sample_rate=16000, history_sec=1.0)
    with patch.object(rb, "playback_reference", buffer), \
         patch.object(rb, "_publishing", False):
        publish_playback(np.ones(1000, dtype=np.float32), 16000)

    assert buffer.written == 0


@pytest.mark.unit
def test_turning_capture_off_forgets_what_was_captured():
    from jarvis.audio import reference_buffer as rb

    rb.set_publishing(True)
    rb.playback_reference.write(np.ones(2000, dtype=np.float32))
    rb.set_publishing(False)

    assert rb.playback_reference.read_aligned(160, 0) is None


@pytest.mark.unit
def test_the_first_arrival_is_measured_not_the_loudest():
    """A desk bounce can beat the direct path. Taking the loudest
    over-estimates, and over-estimating cannot be recovered: the canceller
    is then fed audio older than the echo, and a filter can add delay but
    never remove it."""
    sample_rate = 16000
    chirp = generate_chirp(sample_rate, 0.5)
    direct = int(sample_rate * 0.100)
    reflection = int(sample_rate * 0.400)

    mic = np.zeros(len(chirp) + reflection + 8000, dtype=np.float32)
    mic[direct:direct + len(chirp)] += chirp * 0.20
    mic[reflection:reflection + len(chirp)] += chirp * 0.60    # 3x louder

    result = measure_delay(chirp, mic, sample_rate)

    assert result.usable
    assert result.delay_ms == pytest.approx(100.0, abs=3.0)


@pytest.mark.unit
def test_an_inverted_echo_is_measured_rather_than_refused():
    """Out-of-phase wiring produces a negative peak. The echo is plainly
    there and the canceller handles polarity itself."""
    sample_rate = 16000
    chirp = generate_chirp(sample_rate, 0.5)
    delay = int(sample_rate * 0.100)

    mic = np.zeros(len(chirp) + delay + 8000, dtype=np.float32)
    mic[delay:delay + len(chirp)] -= chirp * 0.30              # inverted

    result = measure_delay(chirp, mic, sample_rate)

    assert result.usable
    assert result.delay_ms == pytest.approx(100.0, abs=3.0)


@pytest.mark.unit
def test_declining_to_cancel_is_announced_rather_than_only_debug_logged(capsys):
    """Startup says cancellation is active; these guards can then decline
    every frame for the whole session. Silence there is the failure class
    this branch spent a review learning about: announced and absent."""
    from jarvis.audio import reference_buffer as rb

    buffer = ReferenceBuffer(sample_rate=16000, history_sec=1.0)
    buffer.write(np.full(1600, 0.4, dtype=np.float32))

    listener = _create_listener(_Subtracting())
    listener._stream_samplerate = 44100

    with patch.object(rb, "playback_reference", buffer), \
         patch("jarvis.listening.listener.playback_reference", buffer):
        listener._cancel_own_voice(np.full(160, 0.5, dtype=np.float32))
        listener._cancel_own_voice(np.full(160, 0.5, dtype=np.float32))

    printed = capsys.readouterr().out
    assert "not running" in printed
    assert printed.count("not running") == 1      # once, not once per frame

"""Audio buffers keep their dtype and their values, whatever numpy does.

Removing the `numpy<2.0.0` ceiling is a packaging change, and packaging
changes are supposed to be invisible. The one place numpy 2 could make it
visible is here: every audio path in this app converts between float32 in
[-1, 1] and int16 PCM, and NEP 50 changed how scalars promote in exactly
that kind of expression. A silent promotion to float64 doubles the memory
on the hot audio path; a silent change to out-of-range integer conversion
turns clipping into wrap-around, which sounds like loud noise rather than
like distortion.

So these assert the two properties that must hold, not the numpy version
that happens to be installed:

- the dtype coming out is the dtype the caller was promised;
- a sample past full scale clips to the rail instead of wrapping to the
  opposite one.

They pass under numpy 1 and numpy 2. If a future numpy breaks either,
this fails with the reason rather than as a distant audio bug.
"""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")

pytestmark = pytest.mark.unit


def _float_frame(values) -> "np.ndarray":
    return np.asarray(values, dtype=np.float32)


class TestFloatToPcmConversion:
    """float32 in [-1, 1] to int16, the conversion every upload makes."""

    def test_wav_encoding_round_trips_the_sample_values(self) -> None:
        from jarvis.speech.backend import pcm16_wav_bytes

        frame = _float_frame([0.0, 0.5, -0.5, 1.0, -1.0])
        wav = pcm16_wav_bytes(frame, 16000)

        import io
        import wave

        with wave.open(io.BytesIO(wav), "rb") as handle:
            assert handle.getnchannels() == 1
            assert handle.getsampwidth() == 2
            assert handle.getframerate() == 16000
            decoded = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16)

        assert decoded.dtype == np.int16
        expected = np.asarray([0, 16383, -16383, 32767, -32767], dtype=np.int16)
        assert np.array_equal(decoded, expected), (
            f"sample values changed: {decoded.tolist()} != {expected.tolist()}"
        )

    def test_samples_past_full_scale_clip_rather_than_wrap(self) -> None:
        """The failure this prevents is silence turning into loud noise."""
        from jarvis.speech.backend import pcm16_wav_bytes

        import io
        import wave

        frame = _float_frame([4.0, -4.0])
        wav = pcm16_wav_bytes(frame, 16000)
        with wave.open(io.BytesIO(wav), "rb") as handle:
            decoded = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16)

        assert decoded[0] > 0, "positive overload wrapped to negative"
        assert decoded[1] < 0, "negative overload wrapped to positive"
        assert decoded.tolist() == [32767, -32767]


class TestEchoCancellerConversions:
    """The canceller crosses float32 and int16 twice per frame."""

    def test_to_int16_clips_at_the_rails(self) -> None:
        from jarvis.audio.echo_cancel import _to_int16

        out = _to_int16(_float_frame([0.0, 1.0, -1.0, 2.5, -2.5]))
        assert out.dtype == np.int16
        assert out.tolist() == [0, 32767, -32767, 32767, -32767]

    def test_scaling_back_to_float_stays_float32(self) -> None:
        """int16 / python float must not promote the frame to float64."""
        cleaned = np.asarray([0, 16384, -16384], dtype=np.int16)
        restored = cleaned.astype(np.float32) / 32768.0
        assert restored.dtype == np.float32, (
            f"the audio frame promoted to {restored.dtype}, doubling its memory "
            "on the hot path"
        )
        assert restored.tolist() == pytest.approx([0.0, 0.5, -0.5])


class TestPlaybackReferenceConversions:
    """The reference ring normalises whatever the output engine plays."""

    def test_integer_playback_is_scaled_to_float32(self) -> None:
        raw = np.asarray([0, 16384, -16384], dtype=np.int16)
        assert np.issubdtype(raw.dtype, np.integer)
        block = raw.astype(np.float32) / 32768.0
        assert block.dtype == np.float32
        assert block.tolist() == pytest.approx([0.0, 0.5, -0.5])

    def test_float_playback_keeps_float32(self) -> None:
        raw = np.asarray([0.0, 0.5, -0.5], dtype=np.float64)
        assert not np.issubdtype(raw.dtype, np.integer)
        block = raw.astype(np.float32)
        assert block.dtype == np.float32

    def test_resampling_returns_float32(self) -> None:
        from jarvis.audio.reference_buffer import _resample_linear

        out = _resample_linear(_float_frame(np.linspace(-1.0, 1.0, 480)), 48000, 16000)
        assert out.dtype == np.float32
        assert out.size == 160


class TestScalarPromotionOnTheAudioPath:
    """NEP 50 is the specific numpy 2 change that could reach these."""

    def test_multiplying_a_float32_frame_by_a_python_float_stays_float32(self) -> None:
        frame = _float_frame([0.1, 0.2])
        assert (frame * 32767.0).dtype == np.float32
        assert (np.clip(frame, -1.0, 1.0) * 32767.0).dtype == np.float32

    def test_dividing_a_float32_frame_by_a_python_float_stays_float32(self) -> None:
        frame = _float_frame([0.1, 0.2])
        assert (frame / 32768.0).dtype == np.float32

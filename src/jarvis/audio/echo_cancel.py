"""Subtracting Jarvis's own voice from the microphone.

Without this the microphone hears the speaker, so the only way not to hear
yourself is not to listen properly while talking — which is what half
duplex means. Everything the listener does to compensate today (fuzzy
matching a transcript against what was just said, energy baselines, a
shortened utterance window during TTS) is a text-level workaround for a
signal-level problem, and none of it can work while both parties speak at
once.

The interface exists so the missing-library case is ordinary rather than
special. ``pyaec`` ships prebuilt wheels for every platform the app targets,
but a wheel can still fail to load inside a frozen build, and voice input
must not depend on that going right: ``NullEchoCanceller`` passes audio
through untouched, which is exactly today's behaviour.

Sample convention throughout is float32 in [-1, 1], matching the listener
and the speech backends. The underlying library wants int16, and that
conversion lives here rather than leaking outwards.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

# Guarded for the same reason the listener guards it: a machine missing
# numpy must lose this module's function, not the import of every module
# that mentions it. listener.py and tts.py import this at module scope.
try:
    import numpy as np
except ImportError:  # pragma: no cover - exercised by the import tests
    np = None

from ..debug import debug_log
from .reference_buffer import set_publishing

# The adaptive filter has to be longer than the echo it is removing — it can
# only cancel reverberation it can still see. 200 ms covers a normal room;
# longer costs proportionally more CPU for no gain in a small space.
DEFAULT_FILTER_MS = 200

# The canceller must be built for the frame size the capture loop actually
# produces, which is `vad_frame_ms`. Configuring the two independently is
# how this shipped doing nothing at all: the listener sliced 20 ms frames,
# the canceller was built for 10 ms, and every frame was declined for a
# size mismatch while the banner said cancellation was active. There is no
# library reason for 10 ms — pyaec measures 14.8 dB of suppression at 20 ms
# frames — so the listener's frame size wins and this is only a fallback
# for callers that do not state one.
DEFAULT_FRAME_MS = 20


class EchoCanceller(ABC):
    """Removes the far end (speaker output) from the near end (microphone)."""

    @abstractmethod
    def cancel(self, near, far) -> Any:
        """Return ``near`` with ``far`` removed.

        Both are float32 in [-1, 1] and the same length. Implementations
        return audio of that same length, always — a caller on the audio
        thread has nothing sensible to do with a short frame.
        """

    @property
    def frame_samples(self) -> int:
        """Frame size this canceller requires, in samples."""
        return 0

    @property
    def active(self) -> bool:
        """Whether this instance actually cancels anything."""
        return True


class NullEchoCanceller(EchoCanceller):
    """Passes the microphone through untouched.

    Not a stub for tests — it is the supported configuration when echo
    cancellation is off or unavailable, and it is what makes every caller
    able to ignore the difference.
    """

    def cancel(self, near, far) -> Any:
        return near

    @property
    def active(self) -> bool:
        return False


class PyAecEchoCanceller(EchoCanceller):
    """Adaptive echo cancellation via ``pyaec``.

    The filter adapts, so it needs a few hundred milliseconds of speech to
    converge and will let some echo through at the start of each utterance.
    That is inherent to the technique, not a defect to tune away — the
    listener's existing echo heuristics stay useful as a second line.

    Measured limitation, worth knowing before promising anything: almost
    all of this library's suppression comes from its residual-echo
    preprocessor, not from the adaptive filter. With ``enable_preprocess``
    off, measured echo suppression was nil — the residual stayed at the
    level of the echo itself over eight seconds of adaptation. So the
    preprocessor cannot be turned off, and it ducks whatever arrives while
    the far end is active, the user included: about 40% of the user's level
    survives double talk.

    What that buys and does not buy. Barge-in becomes reliable, because
    Jarvis stops hearing itself — that is the larger half of the problem and
    it is solved. Speaking over Jarvis with no loss of transcription quality
    is not solved, and will not be by tuning this. It needs a canceller with
    a real double-talk detector (WebRTC's AEC3), which has no maintained
    cross-platform Python wheel today; ``webrtc-audio-processing`` on PyPI
    ships an sdist and an ARMv7 wheel, and does not build cleanly.
    """

    def __init__(self, sample_rate: int = 16000,
                 frame_ms: int = DEFAULT_FRAME_MS,
                 filter_ms: int = DEFAULT_FILTER_MS) -> None:
        from pyaec import Aec  # imported here so the module stays importable

        self._sample_rate = int(sample_rate)
        self._frame_samples = max(1, int(self._sample_rate * frame_ms / 1000))
        filter_length = max(self._frame_samples,
                            int(self._sample_rate * filter_ms / 1000))
        self._aec = Aec(
            frame_size=self._frame_samples,
            filter_length=filter_length,
            sample_rate=self._sample_rate,
        )
        debug_log(
            f"AEC ready: {self._sample_rate} Hz, {frame_ms} ms frames, "
            f"{filter_ms} ms filter",
            "voice",
        )

    @property
    def frame_samples(self) -> int:
        return self._frame_samples

    def cancel(self, near, far) -> Any:
        near_arr = np.asarray(near, dtype=np.float32).ravel()
        if far is None:
            return near_arr

        far_arr = np.asarray(far, dtype=np.float32).ravel()
        if far_arr.size != near_arr.size or near_arr.size != self._frame_samples:
            # Wrong-sized frames mean the caller's framing disagrees with
            # ours. Cancelling anyway would subtract misaligned audio, which
            # is worse than not cancelling.
            return near_arr

        try:
            near_i16 = _to_int16(near_arr)
            far_i16 = _to_int16(far_arr)
            out = self._aec.cancel_echo(list(near_i16), list(far_i16))
            cleaned = np.asarray(out, dtype=np.int16)
        except Exception as exc:
            debug_log(f"⚠️ AEC failed on a frame: {exc}", "voice")
            return near_arr

        if cleaned.size != near_arr.size:
            return near_arr
        return (cleaned.astype(np.float32) / 32768.0)


def _to_int16(samples):
    """float32 in [-1, 1] to int16, clipping rather than wrapping.

    Without the clip, a sample above 1.0 wraps to a large negative value —
    loud noise the canceller would then faithfully try to model.
    """
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)


def create_echo_canceller(settings: Any, sample_rate: int = 16000,
                          frame_ms: Optional[int] = None) -> EchoCanceller:
    """Build the configured canceller, falling back rather than failing.

    ``frame_ms`` must be the frame size the caller will actually hand over.
    A canceller built for a different one declines every frame, which looks
    from the outside exactly like the feature being broken while the logs
    say it is running.

    Every failure — disabled, library absent, constructor unhappy — lands on
    ``NullEchoCanceller``, so the caller has one type to hold and voice input
    keeps working exactly as it does today.
    """
    if np is None or not bool(getattr(settings, "aec_enabled", False)):
        return NullEchoCanceller()

    try:
        canceller = PyAecEchoCanceller(
            sample_rate=sample_rate,
            frame_ms=int(frame_ms if frame_ms
                         else getattr(settings, "vad_frame_ms", DEFAULT_FRAME_MS)),
            filter_ms=int(getattr(settings, "aec_filter_ms", DEFAULT_FILTER_MS)),
        )
    except ImportError:
        print("  ⚠️  Echo cancellation unavailable (pyaec not installed)", flush=True)
        return NullEchoCanceller()
    except Exception as exc:
        print(f"  ⚠️  Echo cancellation unavailable ({exc})", flush=True)
        return NullEchoCanceller()

    # Only now does anything read the playback reference, so only now is it
    # worth the cost of filling it.
    set_publishing(True)
    print("  🔇 Echo cancellation active — barge-in while Jarvis speaks", flush=True)
    return canceller

"""What the speaker was playing, so the microphone can have it subtracted.

Echo cancellation needs the far end: the exact samples leaving the speaker
at the moment a microphone frame arrived. Nothing in the app knew that —
TTS played through its own callback and the listener never saw those
samples — and that missing signal, not the arithmetic, is what stopped
Jarvis from listening while it speaks.

This is the shared surface between them. The TTS engines write what they
play; the listener asks for the frame that lines up with a mic frame. The
two run on different threads at different rates and neither may block the
other, so the buffer is a plain preallocated ring under one lock: writes
never allocate, reads never wait on synthesis.

Alignment is the whole game. Sound leaves the speaker, crosses the room and
comes back through the microphone tens to hundreds of milliseconds later,
and the amount varies by machine — Bluetooth output is far worse than
wired. So reads take a delay and fetch what was playing *that long ago*.
Getting it wrong does not error; it just cancels the wrong audio, which is
why `calibration.py` measures the delay rather than guessing it.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

# Guarded for the same reason the listener guards it: a machine missing
# numpy must lose this module's function, not the import of every module
# that mentions it. listener.py and tts.py import this at module scope.
try:
    import numpy as np
except (ImportError, OSError):  # pragma: no cover - see the import tests
    # OSError covers numpy installed but unloadable (a broken BLAS), which
    # is the failure this package's siblings already handle.
    np = None

# Enough history to cover any plausible speaker-to-microphone round trip
# plus the frame itself. Bluetooth output is the demanding case and can run
# to a few hundred milliseconds; two seconds is comfortable at 16 kHz for
# well under a megabyte.
DEFAULT_HISTORY_SEC = 2.0

# How far past the end of the requested window a read may still be served.
# Output callbacks arrive tens of milliseconds apart, so a little slack
# stops a normal scheduling gap being read as "playback stopped"; more than
# this and the audio genuinely is history.
STALE_GRACE_SEC = 0.25


class ReferenceBuffer:
    """A ring of recently played samples, addressable by age."""

    def __init__(self, sample_rate: int = 16000,
                 history_sec: float = DEFAULT_HISTORY_SEC) -> None:
        self._sample_rate = int(sample_rate)
        self._capacity = max(1, int(self._sample_rate * history_sec))
        self._samples = np.zeros(self._capacity, dtype=np.float32) if np is not None else None
        self._write_pos = 0
        # Total samples ever written. The absolute count is what makes
        # "how far back" meaningful across wraps of the ring.
        self._written = 0
        # When the newest sample was written. Without this, `_written` only
        # ever grows, so once anything has played the buffer claims to know
        # what was playing for the rest of the session — and the canceller
        # spends that session subtracting a finished sentence from live
        # speech, which silences the user rather than the echo.
        self._last_write_time = 0.0
        self._lock = threading.Lock()

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def written(self) -> int:
        with self._lock:
            return self._written

    def write(self, samples) -> None:
        """Record samples that are being played. Never blocks on a reader.

        Called from an audio output callback, where overruns are silent and
        expensive: no allocation, no resampling, no logging on the hot path.
        """
        if samples is None or np is None:
            return
        block = np.asarray(samples, dtype=np.float32).ravel()
        if block.size == 0:
            return

        # A block longer than the ring can only contribute its tail; keeping
        # more would mean overwriting itself mid-copy.
        if block.size > self._capacity:
            block = block[-self._capacity:]

        with self._lock:
            start = self._write_pos
            end = start + block.size
            if end <= self._capacity:
                self._samples[start:end] = block
            else:
                split = self._capacity - start
                self._samples[start:] = block[:split]
                self._samples[:end - self._capacity] = block[split:]
            self._write_pos = end % self._capacity
            self._written += block.size
            self._last_write_time = time.time()

    def read_aligned(self, frame_samples: int, delay_samples: int):
        """Return what was playing ``delay_samples`` ago, or ``None``.

        ``None`` means the answer is not knowable — the buffer has not run
        long enough, or the delay reaches past what is still held. Returning
        silence instead would be worse than useless: the canceller would
        adapt its filter towards "there is no echo" and then fail to remove
        a real one for seconds afterwards.
        """
        if frame_samples <= 0 or np is None:
            return None
        delay_samples = max(0, int(delay_samples))
        span = frame_samples + delay_samples
        if span > self._capacity:
            return None

        with self._lock:
            if self._written < span:
                return None
            # Nothing has played recently enough for the requested window to
            # contain real audio. Playback stopping is the common case and
            # must read as "no echo", not as "here is what we said a minute
            # ago". Generous by a wide margin: this only has to beat the gap
            # between output callbacks, not be precise.
            age = time.time() - self._last_write_time
            if age > (span / float(self._sample_rate)) + STALE_GRACE_SEC:
                return None
            # The frame ends `delay_samples` before the newest sample.
            end = (self._write_pos - delay_samples) % self._capacity
            start = (end - frame_samples) % self._capacity
            if start < end:
                return self._samples[start:end].copy()
            return np.concatenate((self._samples[start:], self._samples[:end]))

    def clear(self) -> None:
        """Forget everything. Used when playback stops.

        Stale reference audio is worse than none: the canceller would try to
        subtract a sentence that finished seconds ago from live speech.
        """
        if np is None:
            return
        with self._lock:
            self._samples.fill(0.0)
            self._write_pos = 0
            self._written = 0
            self._last_write_time = 0.0


# ── The shared instance ────────────────────────────────────────────────
#
# One buffer, reached by both the output side that fills it and the input
# side that drains it, in the same spirit as the process-wide PortAudio
# lock. Passing it down through the TTS engines, the daemon and the
# listener would thread a parameter through four layers that otherwise have
# nothing to say to each other.

playback_reference = ReferenceBuffer()

# Publishing costs a conversion, a resample and a write inside an output
# callback. With echo cancellation off — the default — nothing ever reads
# the result, so the whole cost is waste on the hot path of every user who
# has not enabled the feature.
_publishing = False


def set_publishing(enabled: bool) -> None:
    """Turn playback capture on. Called when a real canceller is built."""
    global _publishing
    _publishing = bool(enabled)
    if not enabled:
        playback_reference.clear()


def publish_playback(samples, sample_rate: int) -> None:
    """Record audio being played, resampled to the reference rate.

    Called from an audio output callback: it must not raise, whatever the
    engine hands it, because an exception there kills playback rather than
    logging anything useful.
    """
    if np is None or not _publishing:
        return
    try:
        raw = np.asarray(samples).ravel()
        if raw.size == 0:
            return
        # Engines play int16 PCM; the reference is float32 in [-1, 1] like
        # every other sample path in the app. Skipping this scaling would
        # hand the canceller a reference 32768 times too loud, and it would
        # dutifully subtract that from the microphone.
        if np.issubdtype(raw.dtype, np.integer):
            block = raw.astype(np.float32) / 32768.0
        else:
            block = raw.astype(np.float32)
        target = playback_reference.sample_rate
        if int(sample_rate) != target:
            block = _resample_linear(block, int(sample_rate), target)
        playback_reference.write(block)
    except Exception:
        pass


def _resample_linear(samples, src_rate: int, dst_rate: int):
    """Cheap linear resampling, matching the listener's own approach.

    Quality barely matters here — the canceller correlates this against the
    microphone, and both paths see the same distortion.
    """
    if src_rate == dst_rate or samples.size == 0:
        return samples
    count = max(1, int(round(samples.size * dst_rate / float(src_rate))))
    positions = np.linspace(0.0, samples.size - 1, count)
    return np.interp(positions, np.arange(samples.size), samples).astype(np.float32)

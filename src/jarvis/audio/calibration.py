"""Measuring how long Jarvis's own voice takes to come back.

Echo cancellation subtracts what the speaker played from what the microphone
heard, which only works if the two are lined up. They are not: sound leaves
the speaker, crosses the room, and returns through the microphone tens to
hundreds of milliseconds later, and the amount depends on the machine —
buffer sizes, drivers, and above all whether output is wired or Bluetooth,
which can add a third of a second on its own.

Guessing that number is the one thing that makes echo cancellation look
broken rather than imperfect: subtract audio from the wrong moment and you
remove the user's speech while leaving the echo. So it is measured. Play a
sweep, record what comes back, and cross-correlate — the lag at the peak is
the round trip.

The confidence score matters as much as the delay. A quiet room with the
speaker muted produces a peak too, somewhere meaningless, and a calibration
that silently returns a wrong number is worse than one that admits it
failed and leaves the feature off.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Guarded like the rest of this package: listener.py imports `delay_to_samples`
# at module scope, and a machine without numpy must lose calibration rather
# than lose the listener.
try:
    import numpy as np
except (ImportError, OSError):  # pragma: no cover - see the import tests
    # OSError covers numpy installed but unloadable (a broken BLAS), which
    # is the failure this package's siblings already handle.
    np = None

# Long enough for the correlation to have something distinctive to lock
# onto, short enough that a user will sit through it.
DEFAULT_CHIRP_SEC = 0.5

# Sweeping across the band the voice path actually carries. Below 200 Hz
# laptop speakers reproduce little; above 6 kHz telephony-band capture rolls
# off. A sweep beats a tone because every moment of it is distinguishable,
# so the correlation has exactly one peak instead of one per cycle.
CHIRP_LOW_HZ = 200.0
CHIRP_HIGH_HZ = 6000.0

# A round trip longer than this is not a room, it is a bug or a very odd
# wireless path — and searching further mostly invites false peaks.
MAX_DELAY_SEC = 0.5

# Below this the peak is not meaningfully above the surrounding noise, so
# the measurement is a coin toss dressed up as a number.
MIN_CONFIDENCE = 0.30

# How strong an earlier arrival must be, relative to the strongest one, to
# be taken as the true first arrival rather than as noise ahead of it.
FIRST_ARRIVAL_RATIO = 0.3


def background_of(magnitude, peak_index: int) -> float:
    """Typical correlation away from the peak — the field it stands out of."""
    if np is None:
        return 0.0
    others = np.delete(magnitude, peak_index)
    return float(np.mean(others)) if others.size else 0.0


@dataclass(frozen=True)
class CalibrationResult:
    """What a calibration attempt concluded."""

    delay_ms: float
    confidence: float
    ok: bool
    detail: str

    @property
    def usable(self) -> bool:
        return self.ok and self.confidence >= MIN_CONFIDENCE


def generate_chirp(sample_rate: int = 16000,
                   duration_sec: float = DEFAULT_CHIRP_SEC):
    """A linear frequency sweep, windowed at both ends.

    The window matters: an abrupt start is a click, and a click is broadband
    energy that correlates with almost anything, which is precisely the
    false peak this is trying to avoid.
    """
    if np is None:
        raise RuntimeError("numpy is required to generate a calibration sweep")
    n = max(1, int(sample_rate * duration_sec))
    t = np.arange(n, dtype=np.float64) / float(sample_rate)
    span = max(t[-1], 1e-9)
    # Instantaneous frequency rises linearly, so phase is quadratic in t.
    phase = 2 * np.pi * (CHIRP_LOW_HZ * t +
                         (CHIRP_HIGH_HZ - CHIRP_LOW_HZ) * t * t / (2 * span))
    signal = np.sin(phase)

    fade = max(1, int(sample_rate * 0.01))          # 10 ms in and out
    envelope = np.ones(n)
    envelope[:fade] = np.linspace(0.0, 1.0, fade)
    envelope[-fade:] = np.linspace(1.0, 0.0, fade)

    return (signal * envelope * 0.5).astype(np.float32)


def measure_delay(played, recorded, sample_rate: int = 16000) -> CalibrationResult:
    """Find how far ``recorded`` lags ``played``.

    ``recorded`` should start at the moment playback started and run longer
    than ``played`` by at least the delay being searched for.
    """
    if np is None:
        return CalibrationResult(0.0, 0.0, False, "numpy unavailable")
    ref = np.asarray(played, dtype=np.float64).ravel()
    mic = np.asarray(recorded, dtype=np.float64).ravel()

    if ref.size == 0 or mic.size == 0:
        return CalibrationResult(0.0, 0.0, False, "no audio to compare")
    if mic.size < ref.size:
        return CalibrationResult(
            0.0, 0.0, False,
            "recording is shorter than the chirp — capture did not keep up",
        )

    mic_level = float(np.sqrt(np.mean(mic ** 2)))
    if mic_level < 1e-4:
        return CalibrationResult(
            0.0, 0.0, False,
            "microphone heard nothing — check output volume and input device",
        )

    max_lag = min(int(sample_rate * MAX_DELAY_SEC), mic.size - ref.size)
    if max_lag < 1:
        return CalibrationResult(
            0.0, 0.0, False, "recording too short to search for a delay"
        )

    # Normalising both removes level from the comparison, so the peak
    # reflects shape alone and the score is comparable between machines.
    ref = (ref - ref.mean()) / (np.std(ref) or 1.0)
    scores = np.empty(max_lag + 1)
    for lag in range(max_lag + 1):
        window = mic[lag:lag + ref.size]
        std = np.std(window)
        if std < 1e-9:
            scores[lag] = 0.0
            continue
        scores[lag] = float(np.dot(ref, (window - window.mean()) / std) / ref.size)

    # Correlate on magnitude, not sign. A speaker wired out of phase, or a
    # reflection arriving inverted, produces a strong *negative* peak — the
    # echo is plainly there and the canceller's filter handles polarity by
    # itself, so treating it as "no echo found" would refuse a perfectly
    # good measurement and blame the user's volume for it.
    magnitude = np.abs(scores)
    strongest = int(np.argmax(magnitude))
    peak = float(magnitude[strongest])

    # The strongest arrival is not the first one. A desk or screen bounce
    # can beat the direct path — ordinary geometry for a laptop with
    # downward-firing speakers and a mic on the hinge — and taking the
    # loudest would over-estimate the delay. Over-estimating is the
    # unrecoverable direction: the canceller is then fed audio older than
    # the echo, and an FIR filter can add delay but never remove it. So walk
    # back to the earliest arrival that is a real part of the same echo.
    # Measured against the background rather than against the peak: a
    # reflection several times louder than the direct path would otherwise
    # put the floor above the direct arrival and hide it.
    first_arrival_floor = background_of(magnitude, strongest) + FIRST_ARRIVAL_RATIO * (
        peak - background_of(magnitude, strongest)
    )
    best = strongest
    for lag in range(strongest + 1):
        if magnitude[lag] >= first_arrival_floor:
            best = lag
            break

    # Confidence is the peak against the rest of the search, not against
    # zero: a peak of 0.4 among a field of 0.38 means nothing was found.
    confidence = max(0.0, peak - background_of(magnitude, strongest))

    delay_ms = best * 1000.0 / sample_rate
    if confidence < MIN_CONFIDENCE:
        return CalibrationResult(
            delay_ms, confidence, False,
            "no clear echo found — is output muted, or on headphones?",
        )

    return CalibrationResult(
        delay_ms, confidence, True,
        f"round trip {delay_ms:.0f} ms (confidence {confidence:.2f})",
    )


def delay_to_samples(delay_ms: float, sample_rate: int = 16000) -> int:
    """Convert a measured delay into the offset the reference buffer wants."""
    return max(0, int(round(delay_ms * sample_rate / 1000.0)))


def run_calibration(sample_rate: int = 16000,
                    duration_sec: float = DEFAULT_CHIRP_SEC) -> CalibrationResult:
    """Play a sweep, record it back, and report the round trip.

    The measurement has to happen on the machine that will run the app, so
    this exists to be run there — without it ``aec_delay_ms`` has no honest
    value and echo cancellation cannot be turned on.
    """
    import sounddevice as sd

    from ..utils.audio_lock import portaudio_lock

    chirp = generate_chirp(sample_rate, duration_sec)
    # Record past the end of the chirp by the longest delay worth searching,
    # plus a margin: a recording that stops too early cannot contain the
    # answer, and the failure would read as "no echo" rather than "too short".
    tail = int(sample_rate * (MAX_DELAY_SEC + 0.2))
    # One stream, not two. sounddevice's convenience API stops any previous
    # stream when a new one starts, so `play()` followed by `rec()` aborts
    # the sweep microseconds after it begins — the microphone then records
    # a silent room and calibration can never succeed. `playrec` opens a
    # single duplex stream, which is also the only way the two clocks are
    # guaranteed to share a start.
    padded = np.concatenate([chirp, np.zeros(tail, dtype=np.float32)])

    with portaudio_lock:
        captured = sd.playrec(padded, samplerate=sample_rate,
                              channels=1, dtype="float32")
    # The wait happens outside the lock. audio_lock.py's own contract is to
    # take it innermost, around the call only — holding it across a blocking
    # wait stalls the listener and every other stream for a second or more.
    sd.wait()

    recording = np.asarray(captured, dtype=np.float32).ravel()
    return measure_delay(chirp, recording, sample_rate)


def _main() -> int:
    """``python -m jarvis.audio.calibration``."""
    print("\n🔧 Echo cancellation calibration")
    print("   Playing a short sweep through your speakers and listening for it.")
    print("   Keep the room quiet, use speakers rather than headphones, and")
    print("   set the output volume where you normally have it.\n")

    try:
        result = run_calibration()
    except Exception as exc:
        print(f"   ❌ Could not run: {exc}")
        return 1

    if not result.usable:
        print(f"   ❌ {result.detail}")
        print("      Echo cancellation should stay off until this succeeds.")
        return 1

    print(f"   ✅ {result.detail}\n")
    print("   Put this in your config to enable echo cancellation:\n")
    print('       "aec_enabled": true,')
    print(f'       "aec_delay_ms": {result.delay_ms:.0f}\n')
    print("   Then talk over Jarvis while it speaks. If you hear it answer")
    print("   its own words, re-run this. If your words come out chopped,")
    print("   lower aec_filter_ms.\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(_main())

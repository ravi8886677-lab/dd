# Audio (acoustic echo cancellation) — spec

`src/jarvis/audio/` is the signal-level half of letting Jarvis listen while it
speaks. Three modules, one job between them:

| Module | Role |
|---|---|
| `reference_buffer.py` | Holds what the speaker just played, addressable by age |
| `echo_cancel.py` | Subtracts that from the microphone frame |
| `calibration.py` | Measures the speaker→microphone delay the other two need |

## Why it exists

The microphone hears the speaker. Without cancellation the only way not to
hear yourself is not to listen properly while talking, which is what half
duplex means.

Everything the listener does to compensate is a text-level workaround for a
signal-level problem: fuzzy-matching a transcript against what was just
said, energy baselines, a shortened utterance window during TTS. None of it
can work while both parties speak at once, because by then the damage is in
the audio, not the text.

## The signal path

```
TTS output callback ──▶ publish_playback() ──▶ playback_reference (ring)
                                                      │
                                                 read_aligned(n, delay)
                                                      ▼
Microphone frame ──────▶ _cancel_own_voice() ──▶ canceller.cancel(near, far)
```

Sample convention throughout is **float32 in [-1, 1]**, matching the listener
and the speech backends. The underlying library wants int16; that conversion
lives inside `echo_cancel.py` rather than leaking outwards.

### Why a shared module-level buffer

`playback_reference` is a process-wide instance, in the same spirit as the
PortAudio lock. The output side that fills it and the input side that drains
it have nothing else to say to each other, and passing the buffer down would
thread a parameter through the TTS engines, the daemon and the listener.

Publishing is **off until a real canceller is built**. `set_publishing(True)`
is called only by `create_echo_canceller` on success, because with
cancellation off (the default) nothing reads the result and the conversion,
resample and write would be pure waste inside an output callback.

## Alignment is the whole problem

Sound leaves the speaker, crosses the room and returns through the microphone
tens to hundreds of milliseconds later. The amount depends on the machine:
buffer sizes, drivers, and above all whether output is wired or Bluetooth,
which can add a third of a second on its own and drifts.

Subtracting audio from the wrong moment removes the user's speech and leaves
the echo, so the delay is **measured, never guessed**. That is what
`calibration.py` is for, and why `aec_delay_ms` has no useful default.

### Calibration contract

```python
generate_chirp(sample_rate, duration_sec) -> np.ndarray
measure_delay(played, recorded, sample_rate) -> CalibrationResult
delay_to_samples(delay_ms, sample_rate) -> int
run_calibration(sample_rate, duration_sec) -> CalibrationResult   # plays + records
```

`python -m jarvis.audio.calibration` runs it and prints the config lines.

`CalibrationResult.usable` is `ok and confidence >= MIN_CONFIDENCE` (0.30).
The confidence score matters as much as the delay: a quiet room with the
speaker muted produces a correlation peak too, somewhere meaningless, and a
calibration that silently returns a wrong number is worse than one that
admits it failed and leaves the feature off.

Three details in `measure_delay` that are easy to undo by accident:

- **A sweep, not a tone.** Every moment of a sweep is distinguishable, so the
  correlation has exactly one peak instead of one per cycle. It is windowed
  10 ms at each end because an abrupt start is a click, and a click is
  broadband energy that correlates with almost anything.
- **Correlate on magnitude, not sign.** A speaker wired out of phase, or a
  reflection arriving inverted, produces a strong *negative* peak. The echo is
  plainly there and the adaptive filter handles polarity itself, so treating
  it as "no echo found" would refuse a good measurement and blame the user's
  volume.
- **First arrival, not strongest.** A desk or screen bounce can beat the
  direct path (ordinary geometry for a laptop with downward-firing speakers
  and a mic on the hinge). Over-estimating the delay is the unrecoverable
  direction: an FIR filter can add delay but never remove it. So the search
  walks back to the earliest arrival above `FIRST_ARRIVAL_RATIO` of the peak,
  measured against the background rather than against the peak.

`run_calibration` uses a single `sd.playrec` duplex stream. `play()` followed
by `rec()` cannot work: sounddevice's convenience API stops any previous
stream when a new one starts, so the sweep would abort microseconds after it
begins and the microphone would record a silent room. One stream is also the
only way the two clocks are guaranteed to share a start. The PortAudio lock
is taken around the call only, never across `sd.wait()`.

## Fail-open discipline

Every failure lands on `NullEchoCanceller`, which passes the microphone
through untouched. That is not a test stub: it is the supported configuration
when cancellation is off or unavailable, and it is what lets every caller hold
one type and ignore the difference.

`create_echo_canceller` returns it when numpy is missing, when `aec_enabled`
is false, when `pyaec` will not import, and when the constructor raises.
A wheel can fail to load inside a frozen build, and voice input must not
depend on that going right.

`ReferenceBuffer.read_aligned` returns `None` rather than silence when it
cannot answer honestly, and `clear()` wipes on stop. Feeding silence would be
worse than useless: the adaptive filter would learn "there is no echo" and
then fail to remove a real one for seconds afterwards.

The buffer also refuses to answer once playback has stopped. `_written` only
grows, so without a timestamp the buffer would claim to know what was playing
for the rest of the session, and the canceller would spend it subtracting a
finished sentence from live speech, silencing the user rather than the echo.
`STALE_GRACE_SEC` (0.25) is generous by a wide margin: it only has to beat the
gap between output callbacks.

## Frame size must match the listener

`create_echo_canceller(settings, sample_rate, frame_ms)` takes the frame size
the caller will actually hand over. `DEFAULT_FRAME_MS` is 20 to match
`vad_frame_ms`, and exists only as a fallback for callers that do not state
one.

Configuring the two independently is how this feature can be running and doing
nothing: a canceller built for 10 ms frames declines every 20 ms frame it is
given for a size mismatch, while the startup banner says cancellation is
active. There is no library reason for 10 ms; pyaec measures 14.8 dB of
suppression at 20 ms frames, so the listener's frame size wins.

### When the listener declines a frame

`Listener._cancel_own_voice` returns the frame untouched, and announces it
**once and visibly** via `_announce_aec_idle`, when:

| Condition | Why |
|---|---|
| No canceller, or `not canceller.active` | Nothing to do |
| Microphone running at its native rate, not `_samplerate` | Frames are not resampled until the utterance is finalised, so cancelling would subtract 16 kHz audio from 44.1 kHz audio |
| Frame size ≠ `canceller.frame_samples` | The framings disagree; subtracting misaligned audio is worse than not cancelling |
| `read_aligned` returned `None` | Nothing was playing that long ago, so there is no echo to remove |

The visible announcement is the point of `_announce_aec_idle`. Startup prints
that cancellation is active; these guards can then decline every frame for the
rest of the session. A `debug_log` is not enough for that, because the user
sees a feature announced and silently absent, concludes it works badly, and
never learns it never ran. Same reasoning as `_disable_vad`: a degraded
feature is survivable, a degraded feature that reports success is not.

## Measured limitation — read before promising anything

Almost all of pyaec's suppression comes from its **residual-echo
preprocessor**, not from the adaptive filter. With `enable_preprocess` off,
measured suppression is nil: the residual stays at the level of the echo
itself over eight seconds of adaptation.

So the preprocessor cannot be turned off, and it ducks whatever arrives while
the far end is active, the user included. **About 40% of the user's level
survives double talk.**

| Outcome | Status |
|---|---|
| Barge-in (Jarvis stops hearing itself) | Solved, and it is the larger half |
| Speaking over Jarvis at full transcription quality | Not solved, and not reachable by tuning |

The second needs a canceller with a real double-talk detector (WebRTC's AEC3),
which has no maintained cross-platform Python wheel today;
`webrtc-audio-processing` on PyPI ships an sdist and an ARMv7 wheel and does
not build cleanly.

The adaptive filter also needs a few hundred milliseconds of speech to
converge, and will let some echo through at the start of each utterance. That
is inherent to the technique, not a defect to tune away, and it is why the
listener's existing text-level echo heuristics stay useful as a second line.

## Configuration

```json
{
  "aec_enabled": false,
  "aec_delay_ms": 0.0,
  "aec_filter_ms": 200
}
```

| Setting | Default | Description |
|---|---|---|
| `aec_enabled` | `false` | Master switch. Off means `NullEchoCanceller` and no playback publishing. |
| `aec_delay_ms` | `0.0` | Speaker→microphone round trip, from `python -m jarvis.audio.calibration`. Wrong values cancel the wrong audio. Re-measure per output device; Bluetooth is the awkward one (100–300 ms, and it drifts). |
| `aec_filter_ms` | `200` | Adaptive filter length. Must exceed the echo tail it removes, since it can only cancel reverberation it can still see. 200 ms covers a normal room; longer costs proportional CPU for no gain in a small space. Lower it if the user's speech comes out chopped. |

`vad_frame_ms` is read as the canceller's frame size, so the two cannot drift
apart.

**Cancellation requires Piper.** Chatterbox hands a file to pygame and never
sees samples, so there is no playback reference to publish and nothing to
subtract.

Cost is not the constraint: pyaec measures 0.113 ms per 10 ms frame, about 1%
of one core, with prebuilt wheels for win_amd64, win_arm64, macOS x86/ARM and
Linux x86/ARM. `pyaec` is a marker-guarded line in `requirements.txt` rather
than a hard one, so `pip install -r requirements.txt` does not fail outright on
a platform it has no wheel for.

## Testing

`tests/test_echo_cancellation.py` (35), `tests/test_audio_import_independence.py`
(7). Three rules, each of which has already cost a shipped defect:

- **Write the test from the requirement, not the implementation.** Cancellation
  shipped declining every frame while the banner said it was active, and a test
  asserted that the mismatch *declined correctly*, so the suite was green. The
  requirement is "cancellation runs on the frames the listener produces" and the
  test must assert that.
- **Watch a new regression test fail before trusting it.** Re-introduce the
  defect, confirm red, restore, confirm green. A test that passes while the bug
  is still present certifies nothing.
- **Restore `sys.modules` *and* the parent package attribute** when faking an
  import failure. `import_module` rebinds the attribute, so restoring only
  `sys.modules` leaves `from x import y` and `sys.modules["x.y"]` disagreeing,
  and something breaks in an unrelated file much later. Isolation-passes /
  suite-fails is the signature of exactly this, not of flakiness.

Numpy is imported under `try/except (ImportError, OSError)` in all three
modules, because `listener.py` and `tts.py` import them at module scope: a
machine missing numpy must lose this package's function, not the import of
every module that mentions it. `OSError` covers numpy installed but unloadable
(a broken BLAS). `test_audio_import_independence.py` is what holds that line.

The calibration maths is testable without hardware: `measure_delay` takes
arrays, so a synthetic delayed-and-attenuated chirp exercises the full
correlation path. Only `run_calibration` needs a device.

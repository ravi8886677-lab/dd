# Speech-to-text providers — spec

`src/jarvis/speech/` is the provider interface behind transcription. Local
Whisper is not one of its implementations: it is what every caller already
has, and what this package exists to *optionally* step in front of.

| Module | Role |
|---|---|
| `backend.py` | The `SpeechToText` contract, `Transcription`, WAV encoding |
| `factory.py` | Resolves settings to an adapter, or to `None` |
| `openai_stt.py` | Transcription over any OpenAI-compatible transcriptions endpoint |
| `languages.py` | Display name → ISO-639-1 code |

## Why it exists

Local `whisper_model: medium` is the largest fixed cost in the voice loop. A
hosted turbo model answers in a fraction of the time, which is the only reason
this package exists.

**Offline stays the default and the fallback.** `stt_provider` is `local` out
of the box, `get_stt_backend` returns `None` for it, and every failure path in
every adapter returns `None` so the caller drops to local Whisper. A hosted
recogniser being down costs speed, never the feature.

**Nothing is assumed about where audio goes.** The provider is named for the
protocol it speaks, never for a company, and there is no default endpoint:
`stt_base_url` unset means unconfigured, so recognition stays local until the
user names a destination. That destination can be a local `whisper.cpp` server
as easily as a hosted one, which is what keeps the offline path real.

Configs written by earlier releases name `groq`. `ALIASES` maps it to
`openai_compatible` so those installs keep working; the adapter behind the name
is unchanged.

## Contract

```python
class SpeechToText(ABC):
    def transcribe(audio, sample_rate=16000, timeout_sec=15.0) -> Optional[str]
    def transcribe_detailed(audio, sample_rate=16000, timeout_sec=15.0) -> Optional[Transcription]
```

Callers hold audio as **float32 samples in [-1, 1]**, which is what the
listener and the dictation engine already have. Encoding to whatever a
provider wants on the wire is the adapter's job; `pcm16_wav_bytes` does it,
because an uploaded file is what providers take and WAV is the one container
all of them accept.

### `None` and `""` are different answers

| Return | Meaning | Caller does |
|---|---|---|
| `None` | This provider could not produce a transcript | Fall back to local Whisper |
| `""` | The provider heard silence | Accept it; there is nothing to transcribe |

Collapsing these would make silence trigger a redundant local pass, or make a
dead provider look like a quiet room.

`None` says *what* happened and `last_error` says *why*, in words fit to show
a user. Nothing has to read it, and no decision depends on it: the fallback
is driven entirely by `None`. It exists because "hosted recognition stopped
working" and "the assistant has gone slow" are the same experience otherwise,
and they need different responses from the person hearing them. Adapters set
it on every path that returns `None` and clear it on success, so it can never
describe an older failure than the one just returned.

### The timeout belongs to the conversation, not to the HTTP call

`timeout_sec` is not a network setting. A spoken sentence has a local
recogniser standing by, so waiting longer than a person will tolerate buys
nothing that the fallback would not deliver sooner. The caller passes
`stt_timeout_sec` (5s) rather than inheriting the adapter's 15s default,
which is a sensible ceiling for a file upload and dead air in a dialogue.

A caller that passes nothing gets the adapter's default. That is a safe
value, not a good one, and no caller in the voice loop should rely on it.

### Why `transcribe_detailed` exists

The listener does not simply take Whisper's text. It drops segments by
`avg_logprob` and by `no_speech_prob`, which is what stops a news jingle or a
hallucinated phrase from being heard as a command.

A provider returning only a string would route *around* those filters, so
hosted audio would face a weaker gate than local audio for no stated reason.
`Transcription` carries `text`, `language` and Whisper-shaped `segments` so a
hosted transcript meets exactly the same filters.

`transcribe_detailed` has a default implementation that wraps `transcribe`, so
a new adapter satisfies the richer interface from the day it is written. It
degrades honestly: text, no segments. **Empty `segments` means "no basis to
filter", not "nothing to filter"** — the listener treats it as unfiltered text
that still faces the downstream repetition check.

## Resolution

```python
resolve_stt_provider(raw) -> str          # normalises, defaults to "local"
get_stt_backend(settings) -> Optional[SpeechToText]
```

`factory.py` is the one place that names an adapter. `None` means "no hosted
provider" and leaves every caller on local Whisper.

**The endpoint decides configured-ness, not the key.** A provider named with no
`stt_base_url` resolves to `None`: there is nowhere to send audio, and no
default to inherit. A key is optional, because a local `whisper.cpp` server
authenticates nobody and requiring one would block the offline path this
package is named for. A destination that wants a key and did not get one
answers with an error, the adapter returns `None`, and the caller drops to
local Whisper.

`resolve_stt_provider` warns about an unknown name with `print`, not
`debug_log`. It runs inside `load_settings`, and `debug_log` asks
`load_settings` whether debug output is on: that recursion re-reads config and
re-queries the credential store at every level before it unwinds.

## The hosted request

`POST {base_url}/audio/transcriptions`, multipart, `Bearer` auth.

`response_format=verbose_json` is **not optional**. It costs the same as
`json` and adds the detected language plus the per-segment `avg_logprob` and
`no_speech_prob` the listener's filters run on. Requesting the plain shape
would mean hosted audio skipping filters local audio must pass.

Audio shorter than `MIN_AUDIO_SECONDS` (0.15) returns `Transcription("")`
without a request. There is nothing to recognise, and an upload costs a round
trip to be told so.

Clipping in `pcm16_wav_bytes` is explicit: without it, samples outside [-1, 1]
wrap into loud noise that a recogniser hears as garbage rather than as the
clipping it is.

### No streaming

The transcriptions shape offers no streaming. `/openai/v1/realtime`
returns 404; the only endpoint is `POST /openai/v1/audio/transcriptions`,
whole file in, whole transcript out.

Anything that wants to feel incremental has to segment the audio itself and
send the pieces, which is what the listener's VAD already does. Do not plan a
lower-latency design around a streaming endpoint that does not exist.

## Language normalisation

Local Whisper reports `"en"`. Some endpoints' `verbose_json` reports `"English"` for the
same audio, because it serialises Whisper's display name rather than the key.

Everything downstream — tool locale selection, the TTS voice picker — was
written against the two-letter code, so the hosted path is translated or those
choices silently degrade.

`normalise_language` accepts either form: a code passes through, a display name
is translated, case and surrounding space are ignored because the value comes
off the wire. The table is Whisper's own `LANGUAGES` inverted plus the alias
spellings Whisper accepts, so any name it can emit round-trips.

An unrecognised name yields `None` rather than a guess. No language at all is
recoverable downstream; a confidently wrong one is not.

## Listener integration

`_get_hosted_stt` resolves once and caches the result **including the `None`**.
`get_stt_backend` reads settings and logs when a provider is half-configured,
and the listener would otherwise repeat that work, and that log line, on every
utterance.

`_transcribe_hosted` deliberately does **not** hold `transcribe_lock`. That
lock guards the local Whisper model, which a hosted call never touches, so
taking it would serialise voice against dictation for the length of a network
round trip and buy nothing.

It catches exceptions anyway. Adapters are contracted to return `None` rather
than raise, but a caller on the audio thread cannot afford to rely on that.

A detected `language` is recorded to `_last_detected_language`; segments go
through `_filter_segment_dicts(..., "hosted")`, the same filter local
transcripts face.

## Configuration

```json
{
  "stt_provider": "local",
  "stt_api_key": "",
  "stt_model": "",
  "stt_base_url": "",
  "stt_timeout_sec": 5.0
}
```

| Setting | Default | Description |
|---|---|---|
| `stt_provider` | `"local"` | `local` or `openai_compatible`. `groq` is accepted as an alias. Unknown names warn and fall back to `local`. |
| `stt_api_key` | `""` | Read through the credential store. Optional: a local endpoint needs none, and a remote one that wants a key answers with an error, which falls back to local. |
| `stt_model` | `""` | Empty means the adapter's default (`whisper-large-v3-turbo`). |
| `stt_base_url` | `""` | **Required** to reach anything: empty means unconfigured and recognition stays local. Point it at any OpenAI-compatible transcriptions endpoint, including a local one. |
| `stt_timeout_sec` | `5.0` | How long one utterance may wait before the local model answers instead. Non-numeric or non-positive values fall back to the default, because "give up immediately" reads as a typo. |

The README must not claim "100% local" while a hosted provider is selectable.

## Testing

`tests/test_speech_stt.py` (13), `tests/test_speech_detailed.py` (13),
`tests/test_listener_hosted_stt.py` (11),
`tests/test_hosted_stt_experience.py` (11).

- **Assert the fallback, not just the return value.** The contract is that a
  failing provider costs speed and nothing else, so the test that matters is
  that local Whisper still runs and the utterance survives.
- **Cover `None` and `""` separately.** They are different answers and a test
  that only checks falsiness cannot tell them apart.
- **Stub the HTTP call.** Jarvis is offline-first and its suite runs with no
  network.
- **Assert what the user is told, not only what the function returns.** A
  failure reported solely through `debug_log` is a failure reported to
  nobody, because debug output is off by default. The test is that something
  reaches stdout, once, naming the reason.
- **Assert hosted transcripts face the segment filters.** A hosted path that
  skips `avg_logprob` / `no_speech_prob` is the specific regression
  `transcribe_detailed` exists to prevent, and it is invisible in the text.

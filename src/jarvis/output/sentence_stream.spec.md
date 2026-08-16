# Sentence streaming — spec

`sentence_stream.py` splits a reply into speakable pieces and carries the
per-item bookkeeping the TTS worker needs. It is text handling only: it opens
no stream, holds no lock and knows nothing about an engine.

## Why it exists

Time-to-first-audio is otherwise the synthesis time of the *whole* reply.
`_speak_once` synthesises every chunk into a list, concatenates, and only then
opens the output stream.

Split a four-sentence answer into four queue items and the first is spoken
while the second is still being synthesised, cutting the wait to roughly a
quarter, with no streaming required from the language model at all.

Gated by `tts_stream_sentences` (default `false`).

## Contract

```python
split_sentences(text) -> List[str]

class SentenceStreamer:
    def push(chunk: str) -> Iterator[str]   # complete sentences this fragment finished
    def flush() -> Iterator[str]            # the remainder; the model has stopped

@dataclass
class SpeechChunk:
    text: str
    is_last: bool = True
    completion_callback: Optional[Callable[[], None]] = None
    duration_callback: Optional[Callable[[float], None]] = None
    total_duration: Optional[float] = None
    @classmethod
    def coerce(item) -> SpeechChunk
```

`SentenceStreamer` is the only stateful part, holding the remainder between
calls. It is the shape a token-streaming reply path would feed; `split_sentences`
is what the TTS engines call today on a complete reply.

## Why `SpeechChunk` carries its own callbacks

Two failures make naive splitting unsafe, and this dataclass exists to fix
both. Both are invisible in a single-sentence reply, which is why they need
stating rather than rediscovering.

**Callbacks belong to the item, not the engine.** An engine has one set of
fields and a streamed reply has many items, so reading callbacks off shared
instance state fires the completion callback after the *first* item. In the
listener that opens the hot window while Jarvis is still talking, and it hears
its own remaining sentences as user speech. Only the last chunk carries
`completion_callback`, and the worker checks `chunk.is_last` before firing it.

**`total_duration` is the whole reply, not the chunk.** The echo detector
divides the reply's word count by the last reported duration, so a per-sentence
figure would misreport words-per-second by roughly the number of chunks and
skew every echo decision of the turn. The estimate is computed once from the
whole processed text and sent with the **first** chunk only, so it is not
re-sent once per sentence, and the detector does not have to wait for the last
one.

`coerce` accepts a bare string so callers and tests can queue text directly.

A reply that splits to one sentence or fewer is handed to the ordinary
`speak()` path, so the streaming route never adds overhead to a short answer.

An interrupt drains the queue rather than stopping the current chunk alone: a
barge-in "stop" has to stop the reply, not just the clause in flight.

## Splitting rules

Each rule below exists because its absence produced a specific audible defect.

### Sentence-final punctuation

`_SENTENCE_END` matches Latin `.!?` **followed by whitespace or end of
string**, CJK `。！？…` as self-delimiting, and runs of newlines.

The CJK marks are not decoration. Without them a Chinese or Japanese reply
never splits at all, so those users get none of the latency this feature
exists for. Full-width marks need no following space; Latin marks do, or
"3.5" splits.

### Abbreviations are deliberately few

`_ABBREVIATIONS` is a short frozen set (`mr`, `mrs`, `ms`, `dr`, `prof`, `jr`,
`vs`, `etc`, `eg`, `ie`, `approx`, `dept`, `fig`, `inc`, `ltd`, `corp`, `vol`).

**Adding to it is the dangerous direction.** An entry suppresses a real
sentence boundary whenever the word is also an ordinary word: "The answer is
no. Try again." welds into one chunk if `no` is listed for "number", and the
same goes for `am`, `pm`, `co`, `st` and `al`. A missing abbreviation costs one
odd pause; a wrong one silently welds two sentences.

Decimals are handled separately (`_ends_in_decimal`), so `3.` in "3.14" is not
a boundary.

### Fragments merge rather than speak alone

Below a minimum length a "sentence" is usually an initial, a list marker or a
stray "OK.", and speaking it alone sounds clipped. Fragments merge backwards
into the previous piece where there is one, forwards for a leading fragment.

The minimum is **script-aware**, not a single number:

| Script | Constant | Value |
|---|---|---|
| Latin and similar | `MIN_SENTENCE_CHARS` | 12 |
| Dense (CJK, Hangul) | `MIN_SENTENCE_CHARS_DENSE` | 4 |

Eight characters of Chinese is a whole sentence, so a uniform character count
would merge complete sentences in dense scripts and produce the long
unsplittable chunks the feature exists to avoid. `_DENSE_SCRIPT` is a
**Unicode range test, not a language list**, so it covers scripts nobody
thought to enumerate. This is the `CLAUDE.md` rule against hardcoded language
patterns applied to punctuation.

### The streamer keeps its remainder verbatim

`push` finds the last real boundary, emits everything before it, and keeps the
rest **including its trailing space**. Stripping the remainder welds two words
together when the next fragment arrives: "It will be" + "warm" becomes "It will
bewarm".

`push` returns an empty iterator when no boundary has arrived yet. `flush`
releases the remainder stripped, because nothing more is coming.

## Configuration

| Setting | Default | Description |
|---|---|---|
| `tts_stream_sentences` | `false` | Speak a reply a sentence at a time. Off means one synthesis pass for the whole reply. |

## Testing

`tests/test_sentence_streaming.py` (23).

- **Assert the callback fires once, after the last chunk.** This is the
  feedback-loop guard, and it is the whole reason `SpeechChunk` exists. A test
  that only checks the text splits correctly cannot see it.
- **Assert the reported duration is the whole reply's.** A per-chunk duration
  is a plausible-looking number that quietly breaks echo detection.
- **Cover a dense script.** A Latin-only suite passes while CJK replies never
  split.
- **Feed the streamer fragments that break mid-word**, since that is what a
  token stream does, and word-welding is invisible in whole-sentence tests.

The verification this module cannot do is the one that matters most: whether
Jarvis ever answers its own speech with `tts_stream_sentences: true` on real
hardware. No mocked microphone can prove that loop does not happen.

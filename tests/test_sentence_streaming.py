"""Speaking a reply a sentence at a time, without breaking the turn.

Piper synthesises the entire reply before it opens the output stream, so
time-to-first-audio is the synthesis cost of everything the user is about to
hear. Splitting the reply into queue items fixes that for free — the worker
already drains one item at a time.

What it breaks, if done naively, is the turn itself. The engines used to
read their callbacks off shared instance fields, so the completion callback
fired after the *first* item; in the listener that opens the hot window
while Jarvis is still speaking, and every remaining sentence is heard as
user input. And the echo detector divides the whole reply's word count by
the last duration it was told, so per-sentence durations skew the whole
turn's echo decisions.

These tests pin the fix at the queue, where it belongs: an item carries its
own callbacks and knows whether it ends the reply.
"""

from __future__ import annotations

import queue
import threading

import pytest

from jarvis.output.sentence_stream import (
    SentenceStreamer,
    SpeechChunk,
    split_sentences,
)


# ── Splitting ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_plain_prose_splits_at_the_sentence_marks():
    assert split_sentences("Hello there. How are you today? Good.") == [
        "Hello there.",
        "How are you today? Good.",
    ]


@pytest.mark.unit
def test_an_abbreviation_is_not_a_sentence_end():
    """Splitting at "Dr." puts a pause inside a name."""
    assert split_sentences("Ask Dr. Smith about it. Then go.") == [
        "Ask Dr. Smith about it. Then go.",
    ]


@pytest.mark.unit
def test_a_decimal_point_is_not_a_sentence_end():
    assert split_sentences("It costs 3.14 euros in total.") == [
        "It costs 3.14 euros in total.",
    ]


@pytest.mark.unit
def test_a_fragment_is_merged_rather_than_spoken_alone():
    """A lone "Ok." between pauses sounds like a glitch, not an answer."""
    assert split_sentences("Ok. That is a much longer sentence.") == [
        "Ok. That is a much longer sentence.",
    ]


@pytest.mark.unit
def test_text_with_no_punctuation_is_one_piece():
    assert split_sentences("no punctuation at all here") == [
        "no punctuation at all here",
    ]


@pytest.mark.unit
def test_empty_text_yields_nothing_to_say():
    assert split_sentences("") == []
    assert split_sentences("   \n  ") == []


# ── Streaming ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_sentences_are_released_only_once_complete():
    streamer = SentenceStreamer()

    assert list(streamer.push("The weather today is su")) == []
    assert list(streamer.push("nny. It will be ")) == ["The weather today is sunny."]
    assert list(streamer.push("warm later.")) == ["It will be warm later."]


@pytest.mark.unit
def test_the_remainder_keeps_its_spacing():
    """Re-joining a stripped remainder welds two words into one."""
    streamer = SentenceStreamer()
    list(streamer.push("Done. It will be "))

    assert list(streamer.push("warm.")) == ["It will be warm."]


@pytest.mark.unit
def test_flush_releases_an_unfinished_tail():
    """A model that stops mid-sentence must still be heard."""
    streamer = SentenceStreamer()
    list(streamer.push("All good. And then"))

    assert list(streamer.flush()) == ["And then"]


@pytest.mark.unit
def test_flush_on_a_clean_boundary_yields_nothing():
    streamer = SentenceStreamer()
    list(streamer.push("All good here."))

    assert list(streamer.flush()) == []


# ── The queue record ──────────────────────────────────────────────────


@pytest.mark.unit
def test_a_bare_string_is_treated_as_a_whole_reply():
    """Anything still queuing plain text must keep completing its turn."""
    chunk = SpeechChunk.coerce("hello")

    assert chunk.text == "hello"
    assert chunk.is_last is True


@pytest.mark.unit
def test_coercing_a_chunk_leaves_it_alone():
    original = SpeechChunk(text="hi", is_last=False)

    assert SpeechChunk.coerce(original) is original


# ── The engines ───────────────────────────────────────────────────────


class _FakeEngine:
    """The queue-facing half of a TTS engine, with no audio in it."""

    def __init__(self):
        from jarvis.output.tts import PiperTTS

        self.enabled = True
        self._q: queue.Queue = queue.Queue()
        self._thread = object()          # pretend the worker is running
        # A real Event, because queueing a reply now clears it: an interrupt
        # aimed at the previous reply must not silence this one.
        self._should_interrupt = threading.Event()
        self.speak = PiperTTS.speak.__get__(self)
        self.speak_sentences = PiperTTS.speak_sentences.__get__(self)
        self._drain_queue = PiperTTS._drain_queue.__get__(self)

    def start(self):                     # pragma: no cover - never needed
        raise AssertionError("worker should already be running")

    def drain(self):
        items = []
        while not self._q.empty():
            items.append(self._q.get_nowait())
        return items


@pytest.mark.unit
def test_a_single_sentence_reply_is_queued_as_one_piece():
    """Splitting buys nothing here, and costs an extra queue round trip."""
    engine = _FakeEngine()

    assert engine.speak_sentences("Just the one thing.") == 1
    assert len(engine.drain()) == 1


@pytest.mark.unit
def test_a_multi_sentence_reply_is_queued_piece_by_piece():
    engine = _FakeEngine()

    count = engine.speak_sentences(
        "The weather is sunny today. It will be warm later. Bring a hat."
    )
    items = engine.drain()

    assert count == 3
    assert len(items) == 3
    assert "sunny" in items[0].text


@pytest.mark.unit
def test_only_the_final_piece_completes_the_turn():
    """The regression this guards: hot window opening mid-reply."""
    engine = _FakeEngine()
    done = lambda: None

    engine.speak_sentences(
        "The weather is sunny today. It will be warm later. Bring a hat.",
        completion_callback=done,
    )
    items = engine.drain()

    assert [i.is_last for i in items] == [False, False, True]
    assert [i.completion_callback for i in items] == [None, None, done]


@pytest.mark.unit
def test_the_duration_reported_covers_the_whole_reply_not_one_sentence():
    """Echo detection divides the full reply's words by this figure."""
    engine = _FakeEngine()
    seen = []

    engine.speak_sentences(
        "The weather is sunny today. It will be warm later. Bring a hat.",
        duration_callback=seen.append,
    )
    items = engine.drain()

    # Exactly one item reports, and it reports the whole reply.
    reporting = [i for i in items if i.duration_callback is not None]
    assert len(reporting) == 1
    assert reporting[0] is items[0]
    assert all(i.total_duration == items[0].total_duration for i in items)
    assert items[0].total_duration > 0


@pytest.mark.unit
def test_plain_speak_still_queues_one_completing_chunk():
    """Every existing caller goes through this path; it must not change."""
    engine = _FakeEngine()
    done = lambda: None

    engine.speak("Hello there. How are you today?", completion_callback=done)
    items = engine.drain()

    assert len(items) == 1
    assert items[0].is_last is True
    assert items[0].completion_callback is done


@pytest.mark.unit
def test_draining_discards_speech_not_yet_started():
    """A barge-in must stop the reply, not just the clause in flight."""
    engine = _FakeEngine()
    engine.speak_sentences(
        "The weather is sunny today. It will be warm later. Bring a hat."
    )

    engine._drain_queue()

    assert engine._q.empty()


@pytest.mark.unit
def test_nothing_is_queued_for_empty_text():
    engine = _FakeEngine()

    assert engine.speak_sentences("   ") == 0
    assert engine._q.empty()


# ── _speak_once itself ────────────────────────────────────────────────
#
# Nothing above this line reaches `_speak_once`. That gap is how a real
# defect shipped: the method's `chunk: SpeechChunk` parameter was rebound by
# the pre-existing `for chunk in self._voice.synthesize(...)` loop, so every
# later `chunk.*` access hit a Piper AudioChunk instead. Piper is the default
# engine, so Jarvis was silent on every reply — and the `finally` raised a
# second AttributeError that escaped into the worker, leaving the hot window
# shut. Stubs that only exercise `speak()` cannot see any of it.


class _FakeAudioChunk:
    def __init__(self, samples):
        self.audio_int16_array = samples


def _piper_under_test(sentences_audio):
    """A PiperTTS with synthesis and playback faked, but its real methods."""
    import sys
    from unittest.mock import MagicMock
    import numpy as np

    from jarvis.output.tts import PiperTTS

    engine = PiperTTS.__new__(PiperTTS)
    engine.enabled = True
    engine._q = queue.Queue()
    engine._is_speaking = threading.Event()
    engine._should_interrupt = threading.Event()
    engine._audio_lock = threading.Lock()
    engine._audio_stream = None
    engine._last_spoken_text = ""
    engine._sample_rate = 22050
    engine.speaker = None
    engine.length_scale = 1.0
    engine.noise_scale = 0.667
    engine.noise_w = 0.8
    engine._init_error = None
    engine._ensure_initialized = lambda: True
    engine._notify_speaking_state = lambda speaking: None

    voice = MagicMock()
    voice.synthesize.return_value = [
        _FakeAudioChunk(np.asarray(sentences_audio, dtype=np.int16))
    ]
    engine._voice = voice

    # `_speak_once` imports piper's SynthesisConfig before it synthesises.
    # Without this the method raises on that import and never reaches the
    # loop — which is how an earlier draft of these tests passed while the
    # defect they were written for was still present.
    fake_piper_config = MagicMock()
    sys.modules["piper"] = MagicMock()
    sys.modules["piper.config"] = fake_piper_config

    fake_sd = MagicMock()
    fake_sd.CallbackAbort = type("CallbackAbort", (Exception,), {})
    fake_sd.CallbackStop = type("CallbackStop", (Exception,), {})
    stream = MagicMock()
    stream.active = False               # "playback finished immediately"
    fake_sd.OutputStream.return_value = stream
    sys.modules["sounddevice"] = fake_sd

    return engine


@pytest.mark.unit
def test_speaking_a_chunk_does_not_lose_the_chunk_to_the_synthesis_loop():
    """The showstopper: `chunk` rebound by `for chunk in ...synthesize()`."""
    from jarvis.output.tts import PiperTTS

    engine = _piper_under_test([100, 200, 300, 400])
    fired = []

    PiperTTS._speak_once(
        engine,
        SpeechChunk(text="Hello there.", is_last=True,
                    completion_callback=lambda: fired.append(True)),
    )

    assert fired == [True], "completion callback never ran — chunk was clobbered"
    # Proof the synthesis loop really ran: a vacuous pass would mean the
    # method raised before reaching it and this test guarded nothing.
    assert engine._voice.synthesize.called


@pytest.mark.unit
def test_a_non_final_chunk_leaves_the_engine_reporting_that_it_is_speaking():
    """Between sentences Jarvis is still mid-reply.

    Claiming otherwise makes the listener skip `interrupt()` on a stop
    command and mark Jarvis's own next sentence as user speech.
    """
    from jarvis.output.tts import PiperTTS

    engine = _piper_under_test([100, 200])
    engine._q.put(SpeechChunk(text="and more.", is_last=True))   # queue not empty

    PiperTTS._speak_once(engine, SpeechChunk(text="First part.", is_last=False))

    assert engine._is_speaking.is_set()


@pytest.mark.unit
def test_the_final_chunk_ends_the_speaking_state():
    from jarvis.output.tts import PiperTTS

    engine = _piper_under_test([100, 200])

    PiperTTS._speak_once(engine, SpeechChunk(text="All done.", is_last=True))

    assert not engine._is_speaking.is_set()


@pytest.mark.unit
def test_an_interrupt_landing_between_chunks_is_not_cleared_by_the_next_one():
    """`interrupt()` drains the queue and sets the flag, but the worker may
    already hold the next sentence. Clearing the flag on entry played that
    sentence in full — the user's "stop" silenced the tail, not the clause."""
    from jarvis.output.tts import PiperTTS

    engine = _piper_under_test([100, 200])
    engine._should_interrupt.set()               # interrupt already pending

    PiperTTS._speak_once(engine, SpeechChunk(text="Should not play.", is_last=True))

    assert engine._should_interrupt.is_set(), "interrupt was swallowed"

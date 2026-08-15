"""Splitting a reply into speakable pieces, and the record that carries one.

Time-to-first-audio today is the synthesis time of the *whole* reply:
``_speak_once`` synthesises every chunk into a list, concatenates, and only
then opens the output stream. Split a four-sentence answer into four queue
items and the first is spoken while the second is still being synthesised,
which cuts the wait to roughly a quarter without the language model needing
to stream anything at all.

Two things make that unsafe if done naively, and ``SpeechChunk`` exists to
fix both. The engines used to read their callbacks off shared instance
state, so the completion callback fired after the *first* item of a
multi-item reply — in the listener that opens the hot window while Jarvis is
still talking, and it hears its own remaining sentences as user speech. And
the echo detector divides the whole reply's word count by the last reported
duration, so per-sentence durations skew every echo decision of the turn.
A chunk therefore carries its own callbacks, knows whether it is the last,
and carries an estimate of the *whole* reply's duration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterator, List, Optional

# A sentence ends at . ? ! or a line break, allowing for closing quotes and
# brackets after the mark. Kept deliberately simple: the cost of splitting
# slightly wrong is a marginally odd pause, not a wrong answer.
_SENTENCE_END = re.compile(r'[.!?]["\')\]]*(?=\s|$)|\n+')

# Full stops that do not end a sentence. Splitting on these produces a pause
# in the middle of a phrase, which sounds like a stutter.
_ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "mt",
    "vs", "etc", "eg", "ie", "approx", "dept", "est", "fig",
    "inc", "ltd", "co", "corp", "no", "vol", "al", "am", "pm",
})

# Below this a "sentence" is usually a fragment — an initial, a list marker,
# a stray "OK." — and speaking it alone sounds clipped.
MIN_SENTENCE_CHARS = 12


def _ends_in_abbreviation(text: str) -> bool:
    match = re.search(r'(\w+)\.$', text.strip())
    if match is None:
        return False
    return match.group(1).lower() in _ABBREVIATIONS


def _ends_in_decimal(text: str) -> bool:
    """`3.` in "3.14" is not the end of a sentence."""
    return re.search(r'\d\.$', text.strip()) is not None


def split_sentences(text: str) -> List[str]:
    """Split prose into speakable sentences.

    Fragments are merged forward rather than spoken alone, so the result is
    never a list of one-word utterances even for heavily punctuated input.
    """
    if not text or not text.strip():
        return []

    pieces: List[str] = []
    start = 0
    for match in _SENTENCE_END.finditer(text):
        candidate = text[start:match.end()]
        if _ends_in_abbreviation(candidate) or _ends_in_decimal(candidate):
            continue
        stripped = candidate.strip()
        if stripped:
            pieces.append(stripped)
        start = match.end()

    tail = text[start:].strip()
    if tail:
        pieces.append(tail)

    # Merge anything too short to stand on its own into a neighbour —
    # backwards where there is one, forwards for a leading fragment.
    merged: List[str] = []
    carry = ""
    for piece in pieces:
        if carry:
            piece = f"{carry} {piece}"
            carry = ""
        if len(piece) < MIN_SENTENCE_CHARS:
            if merged:
                merged[-1] = f"{merged[-1]} {piece}"
            else:
                carry = piece
            continue
        merged.append(piece)
    if carry:
        merged.append(carry)
    return merged


def _ends_on_a_boundary(text: str) -> bool:
    """Whether ``text`` finishes a sentence rather than stopping mid-one."""
    tail = text.rstrip()
    if not re.search(r'[.!?]["\')\]]*$', tail):
        return False
    return not _ends_in_abbreviation(tail) and not _ends_in_decimal(tail)


class SentenceStreamer:
    """Accumulate streamed text and release complete sentences.

    The language model emits arbitrary fragments; the synthesiser wants
    whole sentences. This holds the remainder between calls, which is the
    only stateful part of the pipeline, and ``flush`` releases whatever is
    left when the model stops.
    """

    def __init__(self) -> None:
        self._buffer = ""

    def push(self, chunk: str) -> Iterator[str]:
        """Absorb a fragment; yield every complete sentence it finished."""
        if not chunk:
            return iter(())
        self._buffer += chunk

        # Find the end of the last real sentence boundary in the buffer.
        # The remainder is kept *verbatim*, including its trailing space:
        # re-joining a stripped remainder with the next fragment welds two
        # words together ("It will be" + "warm" -> "It will bewarm").
        boundary = 0
        for match in _SENTENCE_END.finditer(self._buffer):
            candidate = self._buffer[boundary:match.end()]
            if _ends_in_abbreviation(candidate) or _ends_in_decimal(candidate):
                continue
            boundary = match.end()

        if boundary == 0:
            return iter(())

        complete, self._buffer = self._buffer[:boundary], self._buffer[boundary:]
        return iter(split_sentences(complete))

    def flush(self) -> Iterator[str]:
        """Release the remainder. The model has stopped; nothing more comes."""
        remainder = self._buffer.strip()
        self._buffer = ""
        return iter([remainder] if remainder else [])


@dataclass
class SpeechChunk:
    """One item of speech, carrying everything the worker needs about it.

    Callbacks live here rather than on the engine because the engine has
    one set of fields and a streamed reply has many items: reading shared
    state means the last caller's completion callback fires after the first
    item, whoever queued it.
    """

    text: str
    is_last: bool = True
    completion_callback: Optional[Callable[[], None]] = None
    duration_callback: Optional[Callable[[float], None]] = None
    # Duration of the whole reply, not of this chunk. The echo detector
    # measures against the full reply text, so a per-chunk figure would
    # misreport words-per-second by roughly the number of chunks.
    total_duration: Optional[float] = None

    @classmethod
    def coerce(cls, item) -> "SpeechChunk":
        """Accept a bare string, for callers and tests that queue text."""
        if isinstance(item, cls):
            return item
        return cls(text=str(item), is_last=True)

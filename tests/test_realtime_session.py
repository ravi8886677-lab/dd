"""The session runner: what happens between a provider and the rest of Jarvis.

This is the provider-agnostic half, so everything here is asserted against
a scripted backend rather than a live connection. The contracts that matter
are in ``src/jarvis/realtime/realtime.spec.md``: the model owns the
conversation, Jarvis owns capability, interruption stops playback at once,
a failure falls back instead of dying, and nothing said is lost.
"""

from __future__ import annotations

from typing import Iterator, List
from unittest.mock import patch

import pytest

from jarvis.realtime.backend import (
    RealtimeConfig,
    RealtimeEvent,
    RealtimeEventType,
    RealtimeVoiceBackend,
)
from jarvis.realtime.session import RealtimeSession
from jarvis.realtime.tool_bridge import FunctionCall, FunctionResult
from jarvis.tools.types import ToolExecutionResult


class ScriptedBackend(RealtimeVoiceBackend):
    """A provider that plays back a fixed list of events."""

    def __init__(self, events: List[RealtimeEvent], *, fail_connect: bool = False):
        self._events = events
        self._fail_connect = fail_connect
        self.sent_results: List[FunctionResult] = []
        self.sent_audio: List[bytes] = []
        self.interrupted = 0
        self.closed = 0
        self.connected_with: RealtimeConfig | None = None

    def connect(self, config: RealtimeConfig) -> None:
        if self._fail_connect:
            raise ConnectionError("provider unreachable")
        self.connected_with = config

    def send_audio(self, pcm: bytes) -> None:
        self.sent_audio.append(pcm)

    def send_function_result(self, result: FunctionResult) -> None:
        self.sent_results.append(result)

    def interrupt(self) -> None:
        self.interrupted += 1

    def events(self) -> Iterator[RealtimeEvent]:
        yield from self._events

    def close(self) -> None:
        self.closed += 1


class _Cfg:
    pass


def _session(backend, **kwargs):
    played: List[bytes] = []
    interrupts: List[int] = []
    session = RealtimeSession(
        backend,
        db=None,
        cfg=_Cfg(),
        on_audio=played.append,
        on_interrupt=lambda: interrupts.append(1),
        **kwargs,
    )
    return session, played, interrupts


# ── Falling back ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_a_provider_that_will_not_connect_reports_failure_instead_of_raising():
    """The caller has a local pipeline to fall back to; it needs an answer."""
    backend = ScriptedBackend([], fail_connect=True)
    session, _, _ = _session(backend)

    assert session.start(RealtimeConfig(model="m", api_key="k")) is False


@pytest.mark.unit
def test_a_successful_connection_reports_success():
    backend = ScriptedBackend([])
    session, _, _ = _session(backend)

    assert session.start(RealtimeConfig(model="m", api_key="k")) is True


# ── Speaking ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_model_audio_reaches_playback_in_order():
    backend = ScriptedBackend([
        RealtimeEvent(RealtimeEventType.AUDIO_DELTA, audio=b"one"),
        RealtimeEvent(RealtimeEventType.AUDIO_DELTA, audio=b"two"),
        RealtimeEvent(RealtimeEventType.CLOSED),
    ])
    session, played, _ = _session(backend)
    session.start(RealtimeConfig(model="m", api_key="k"))
    session.run_until_closed()

    assert played == [b"one", b"two"]


# ── Interruption ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_the_user_cutting_in_stops_playback_and_tells_the_provider():
    """Barge-in has to stop local sound, not just the remote generation.

    Audio already handed to the speakers keeps playing otherwise, so the
    user hears the reply they interrupted continue over their own voice.
    """
    backend = ScriptedBackend([
        RealtimeEvent(RealtimeEventType.AUDIO_DELTA, audio=b"talking"),
        RealtimeEvent(RealtimeEventType.SPEECH_STARTED),
        RealtimeEvent(RealtimeEventType.CLOSED),
    ])
    session, _, interrupts = _session(backend)
    session.start(RealtimeConfig(model="m", api_key="k"))
    session.run_until_closed()

    assert interrupts, "local playback was never stopped"
    assert backend.interrupted == 1, "the provider was never told to stop"


# ── Tool handoff ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_a_function_call_runs_and_its_result_goes_back_to_the_model():
    backend = ScriptedBackend([
        RealtimeEvent(
            RealtimeEventType.FUNCTION_CALL,
            function_call=FunctionCall("c1", "webSearch", '{"query": "rain"}'),
        ),
        RealtimeEvent(RealtimeEventType.CLOSED),
    ])
    session, _, _ = _session(backend)
    session.start(RealtimeConfig(model="m", api_key="k"))

    with patch(
        "jarvis.realtime.tool_bridge.run_tool_with_retries",
        return_value=ToolExecutionResult(success=True, reply_text="it is raining"),
    ):
        session.run_until_closed()

    assert len(backend.sent_results) == 1
    assert backend.sent_results[0].call_id == "c1"
    assert "it is raining" in backend.sent_results[0].output


@pytest.mark.unit
def test_a_failing_tool_still_gets_an_answer_back():
    """A call with no reply leaves the model waiting mid-conversation."""
    backend = ScriptedBackend([
        RealtimeEvent(
            RealtimeEventType.FUNCTION_CALL,
            function_call=FunctionCall("c2", "webSearch", "{}"),
        ),
        RealtimeEvent(RealtimeEventType.CLOSED),
    ])
    session, _, _ = _session(backend)
    session.start(RealtimeConfig(model="m", api_key="k"))

    with patch(
        "jarvis.realtime.tool_bridge.run_tool_with_retries",
        side_effect=RuntimeError("boom"),
    ):
        session.run_until_closed()

    assert len(backend.sent_results) == 1
    assert backend.sent_results[0].success is False


# ── Nothing said is lost ──────────────────────────────────────────────


@pytest.mark.unit
def test_both_sides_of_the_conversation_are_captured_for_memory():
    """Switching front ends must not create unrememberable conversations."""
    backend = ScriptedBackend([
        RealtimeEvent(RealtimeEventType.USER_TRANSCRIPT, text="what is the weather"),
        RealtimeEvent(RealtimeEventType.ASSISTANT_TEXT, text="it is raining"),
        RealtimeEvent(RealtimeEventType.CLOSED),
    ])
    session, _, _ = _session(backend)
    session.start(RealtimeConfig(model="m", api_key="k"))
    session.run_until_closed()

    turns = session.transcript()
    assert [(t.role, t.text) for t in turns] == [
        ("user", "what is the weather"),
        ("assistant", "it is raining"),
    ]


@pytest.mark.unit
def test_a_recoverable_provider_error_does_not_end_the_conversation():
    """Providers report routine failures as errors, barge-in among them.

    Cancelling a response the server already finished is an error payload,
    and it happens on every interruption. Ending the session there would
    kill the conversation on the feature this subsystem exists for.
    """
    backend = ScriptedBackend([
        RealtimeEvent(RealtimeEventType.ERROR, error="Cancellation failed: no active response"),
        RealtimeEvent(RealtimeEventType.AUDIO_DELTA, audio=b"still here"),
        RealtimeEvent(RealtimeEventType.CLOSED),
    ])
    session, played, _ = _session(backend)
    session.start(RealtimeConfig(model="m", api_key="k"))
    session.run_until_closed()

    assert played == [b"still here"], "the session stopped on a recoverable error"


@pytest.mark.unit
def test_a_transcript_survives_a_session_that_errored():
    """A dropped socket must not discard what was already said."""
    backend = ScriptedBackend([
        RealtimeEvent(RealtimeEventType.USER_TRANSCRIPT, text="remember this"),
        RealtimeEvent(RealtimeEventType.ERROR, error="socket died"),
        RealtimeEvent(RealtimeEventType.CLOSED),
    ])
    session, _, _ = _session(backend)
    session.start(RealtimeConfig(model="m", api_key="k"))
    session.run_until_closed()

    assert [t.text for t in session.transcript()] == ["remember this"]


@pytest.mark.unit
def test_consecutive_text_from_one_side_becomes_one_turn():
    """Providers stream text in fragments; memory wants sentences."""
    backend = ScriptedBackend([
        RealtimeEvent(RealtimeEventType.ASSISTANT_TEXT, text="it is "),
        RealtimeEvent(RealtimeEventType.ASSISTANT_TEXT, text="raining"),
        RealtimeEvent(RealtimeEventType.CLOSED),
    ])
    session, _, _ = _session(backend)
    session.start(RealtimeConfig(model="m", api_key="k"))
    session.run_until_closed()

    assert [t.text for t in session.transcript()] == ["it is raining"]


# ── Shutdown ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_closing_the_connection_ends_the_session():
    backend = ScriptedBackend([
        RealtimeEvent(RealtimeEventType.CLOSED),
    ])
    session, _, _ = _session(backend)
    session.start(RealtimeConfig(model="m", api_key="k"))
    session.run_until_closed()
    session.close()

    assert backend.closed >= 1


@pytest.mark.unit
def test_microphone_audio_only_leaves_while_a_session_is_running():
    """Audio must not reach a provider before start or after close."""
    backend = ScriptedBackend([RealtimeEvent(RealtimeEventType.CLOSED)])
    session, _, _ = _session(backend)

    session.send_audio(b"before")
    session.start(RealtimeConfig(model="m", api_key="k"))
    session.send_audio(b"during")
    session.close()
    session.send_audio(b"after")

    assert backend.sent_audio == [b"during"]

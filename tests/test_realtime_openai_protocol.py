"""Translating the OpenAI realtime wire protocol into Jarvis events.

The socket half of the adapter needs a live endpoint to exercise, but the
translation does not, and it is where the protocol risk actually sits: a
misread event name is a feature that silently does nothing.

Audio and transcript events were renamed between the beta and GA shapes of
this API, so both spellings are accepted. An adapter that understands only
one of them looks connected and stays mute.
"""

from __future__ import annotations

import base64

import pytest

from jarvis.realtime.backend import RealtimeEventType
from jarvis.realtime.openai_realtime import translate_server_event


def _audio_payload(name: str, raw: bytes) -> dict:
    return {"type": name, "delta": base64.b64encode(raw).decode("ascii")}


# ── Audio ─────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "event_name",
    ["response.audio.delta", "response.output_audio.delta"],
)
def test_model_speech_arrives_as_decoded_pcm(event_name):
    event = translate_server_event(_audio_payload(event_name, b"\x01\x02\x03"))

    assert event is not None
    assert event.type is RealtimeEventType.AUDIO_DELTA
    assert event.audio == b"\x01\x02\x03"


@pytest.mark.unit
def test_undecodable_audio_is_dropped_rather_than_played_as_noise():
    event = translate_server_event({"type": "response.audio.delta", "delta": "!!!not base64"})

    assert event is None


# ── Transcripts ───────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "event_name",
    ["response.audio_transcript.delta", "response.output_audio_transcript.delta"],
)
def test_what_the_model_says_is_captured_as_assistant_text(event_name):
    event = translate_server_event({"type": event_name, "delta": "it is raining"})

    assert event is not None
    assert event.type is RealtimeEventType.ASSISTANT_TEXT
    assert event.text == "it is raining"


@pytest.mark.unit
def test_what_the_user_said_is_captured_as_user_text():
    event = translate_server_event(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "what is the weather",
        }
    )

    assert event is not None
    assert event.type is RealtimeEventType.USER_TRANSCRIPT
    assert event.text == "what is the weather"


# ── Turn taking ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_the_user_starting_to_speak_is_a_barge_in():
    """This is the event the whole feature is bought for."""
    event = translate_server_event({"type": "input_audio_buffer.speech_started"})

    assert event is not None
    assert event.type is RealtimeEventType.SPEECH_STARTED


@pytest.mark.unit
def test_a_finished_response_ends_the_turn():
    event = translate_server_event({"type": "response.done"})

    assert event is not None
    assert event.type is RealtimeEventType.TURN_DONE


# ── Function calls ────────────────────────────────────────────────────


@pytest.mark.unit
def test_a_completed_function_call_carries_name_id_and_arguments():
    event = translate_server_event(
        {
            "type": "response.function_call_arguments.done",
            "call_id": "call_123",
            "name": "webSearch",
            "arguments": '{"query": "rain"}',
        }
    )

    assert event is not None
    assert event.type is RealtimeEventType.FUNCTION_CALL
    assert event.function_call is not None
    assert event.function_call.call_id == "call_123"
    assert event.function_call.name == "webSearch"
    assert event.function_call.arguments_json == '{"query": "rain"}'


@pytest.mark.unit
def test_a_function_call_with_no_name_is_ignored():
    """An unnamed call cannot be routed, and must not reach the tool path."""
    event = translate_server_event(
        {"type": "response.function_call_arguments.done", "call_id": "c", "arguments": "{}"}
    )

    assert event is None


# ── Failure ───────────────────────────────────────────────────────────


@pytest.mark.unit
def test_a_provider_error_becomes_an_error_event_with_its_message():
    event = translate_server_event(
        {"type": "error", "error": {"message": "invalid api key"}}
    )

    assert event is not None
    assert event.type is RealtimeEventType.ERROR
    assert "invalid api key" in (event.error or "")


@pytest.mark.unit
def test_an_error_with_no_message_still_reports_an_error():
    event = translate_server_event({"type": "error"})

    assert event is not None
    assert event.type is RealtimeEventType.ERROR
    assert event.error


# ── Everything else ───────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        {"type": "session.created"},
        {"type": "rate_limits.updated"},
        {"type": "something.invented.later"},
        {},
        {"type": ""},
    ],
)
def test_events_we_do_not_act_on_are_ignored_quietly(payload):
    """Providers add events; an unknown one is not a failure."""
    assert translate_server_event(payload) is None

"""Translating the Gemini Live wire protocol into Jarvis events.

Gemini differs from the other adapter in ways that matter rather than in
naming. One server message can carry several parts, so translation returns
a list. Function arguments arrive as a decoded object rather than as JSON
text. Interruption is announced by the server instead of being inferred
from the user starting to speak. And the two directions do not share a
sample rate.

The socket needs a live endpoint to exercise; this does not, and this is
where the protocol risk sits.
"""

from __future__ import annotations

import base64
import json

import pytest

from jarvis.realtime.backend import RealtimeEventType
from jarvis.realtime.gemini_realtime import (
    INPUT_SAMPLE_RATE,
    OUTPUT_SAMPLE_RATE,
    gemini_tool_declarations,
    translate_server_message,
)


def _audio_part(raw: bytes) -> dict:
    return {
        "serverContent": {
            "modelTurn": {
                "parts": [
                    {
                        "inlineData": {
                            "mimeType": "audio/pcm",
                            "data": base64.b64encode(raw).decode("ascii"),
                        }
                    }
                ]
            }
        }
    }


# ── Audio ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_model_speech_arrives_as_decoded_pcm():
    events = translate_server_message(_audio_part(b"\x01\x02\x03"))

    assert len(events) == 1
    assert events[0].type is RealtimeEventType.AUDIO_DELTA
    assert events[0].audio == b"\x01\x02\x03"


@pytest.mark.unit
def test_every_audio_part_of_one_message_is_played():
    """A turn can arrive as several parts; dropping any of them clips speech."""
    message = {
        "serverContent": {
            "modelTurn": {
                "parts": [
                    {"inlineData": {"mimeType": "audio/pcm", "data": base64.b64encode(b"aa").decode()}},
                    {"inlineData": {"mimeType": "audio/pcm", "data": base64.b64encode(b"bb").decode()}},
                ]
            }
        }
    }

    assert [e.audio for e in translate_server_message(message)] == [b"aa", b"bb"]


@pytest.mark.unit
def test_a_text_part_is_not_mistaken_for_audio():
    message = {"serverContent": {"modelTurn": {"parts": [{"text": "hello"}]}}}
    events = translate_server_message(message)

    assert [e.type for e in events] == [RealtimeEventType.ASSISTANT_TEXT]
    assert events[0].text == "hello"


@pytest.mark.unit
def test_undecodable_audio_is_dropped_rather_than_played_as_noise():
    message = {
        "serverContent": {
            "modelTurn": {"parts": [{"inlineData": {"mimeType": "audio/pcm", "data": "!!!"}}]}
        }
    }

    assert translate_server_message(message) == []


# ── Turn taking ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_the_server_announcing_an_interruption_stops_playback():
    """Gemini reports the barge-in itself rather than leaving it inferred."""
    events = translate_server_message({"serverContent": {"interrupted": True}})

    assert [e.type for e in events] == [RealtimeEventType.SPEECH_STARTED]


@pytest.mark.unit
def test_turn_complete_ends_the_turn():
    events = translate_server_message({"serverContent": {"turnComplete": True}})

    assert [e.type for e in events] == [RealtimeEventType.TURN_DONE]


# ── Transcripts ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_output_transcription_is_assistant_text():
    events = translate_server_message(
        {"serverContent": {"outputTranscription": {"text": "it is raining"}}}
    )

    assert [(e.type, e.text) for e in events] == [
        (RealtimeEventType.ASSISTANT_TEXT, "it is raining")
    ]


@pytest.mark.unit
def test_input_transcription_is_user_text():
    events = translate_server_message(
        {"serverContent": {"inputTranscription": {"text": "what is the weather"}}}
    )

    assert [(e.type, e.text) for e in events] == [
        (RealtimeEventType.USER_TRANSCRIPT, "what is the weather")
    ]


# ── Function calls ────────────────────────────────────────────────────


@pytest.mark.unit
def test_function_arguments_are_re_encoded_as_json_for_the_bridge():
    """Gemini decodes arguments; the bridge parses them. Bridge the gap here."""
    events = translate_server_message(
        {
            "toolCall": {
                "functionCalls": [
                    {"id": "c1", "name": "webSearch", "args": {"query": "rain"}}
                ]
            }
        }
    )

    assert len(events) == 1
    call = events[0].function_call
    assert call is not None
    assert call.call_id == "c1"
    assert call.name == "webSearch"
    assert json.loads(call.arguments_json) == {"query": "rain"}


@pytest.mark.unit
def test_several_function_calls_in_one_message_all_run():
    """Gemini batches calls; dropping any leaves the model waiting on it."""
    events = translate_server_message(
        {
            "toolCall": {
                "functionCalls": [
                    {"id": "a", "name": "webSearch", "args": {}},
                    {"id": "b", "name": "weather", "args": {}},
                ]
            }
        }
    )

    assert [e.function_call.name for e in events] == ["webSearch", "weather"]


@pytest.mark.unit
def test_a_call_with_no_arguments_still_produces_valid_json():
    events = translate_server_message(
        {"toolCall": {"functionCalls": [{"id": "c", "name": "stop"}]}}
    )

    assert json.loads(events[0].function_call.arguments_json) == {}


@pytest.mark.unit
def test_a_function_call_with_no_name_is_ignored():
    events = translate_server_message(
        {"toolCall": {"functionCalls": [{"id": "c", "args": {}}]}}
    )

    assert events == []


# ── Failure and the rest ──────────────────────────────────────────────


@pytest.mark.unit
def test_an_error_payload_becomes_an_error_event():
    events = translate_server_message({"error": {"message": "invalid api key"}})

    assert events[0].type is RealtimeEventType.ERROR
    assert "invalid api key" in (events[0].error or "")


@pytest.mark.unit
@pytest.mark.parametrize(
    "message",
    [{"setupComplete": {}}, {"usageMetadata": {}}, {"invented": "later"}, {}],
)
def test_messages_we_do_not_act_on_are_ignored_quietly(message):
    assert translate_server_message(message) == []


# ── Tool declarations ─────────────────────────────────────────────────


@pytest.mark.unit
def test_tools_are_wrapped_as_function_declarations():
    """Gemini nests tools under functionDeclarations, unlike a flat list."""
    flat = [
        {
            "type": "function",
            "name": "webSearch",
            "description": "Search the web.",
            "parameters": {"type": "object", "properties": {}},
        }
    ]

    declared = gemini_tool_declarations(flat)

    assert len(declared) == 1
    decls = declared[0]["functionDeclarations"]
    assert decls[0]["name"] == "webSearch"
    assert decls[0]["description"] == "Search the web."
    # The realtime "type" marker is an OpenAI-ism and must not leak through.
    assert "type" not in decls[0]


@pytest.mark.unit
def test_no_tools_produces_no_declaration_block():
    """An empty tools array is rejected; omitting it entirely is not."""
    assert gemini_tool_declarations([]) == []


# ── Audio rates ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_the_two_directions_do_not_share_a_sample_rate():
    """Mixing these up plays every reply at the wrong pitch."""
    assert INPUT_SAMPLE_RATE == 16000
    assert OUTPUT_SAMPLE_RATE == 24000

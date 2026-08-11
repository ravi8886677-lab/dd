"""The bridge between a realtime voice session and Jarvis's tools.

A realtime model asks for work by emitting a function call. Everything it
asks for has to arrive at the same tool path the local reply loop uses, so
that the confirmation gate and the trust store still decide what runs. The
session must also survive whatever the model emits: bad JSON, an unknown
tool name and a tool that fails are all normal traffic, not crashes.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from jarvis.realtime.tool_bridge import (
    FunctionCall,
    dispatch_function_call,
    realtime_tool_schema,
)
from jarvis.tools.types import ToolExecutionResult


# ── Schema translation ────────────────────────────────────────────────

CHAT_SHAPE = [
    {
        "type": "function",
        "function": {
            "name": "webSearch",
            "description": "Search the web.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }
]


@pytest.mark.unit
def test_tool_schema_is_flattened_for_a_realtime_session():
    """Realtime sessions name the tool at the top level, chat nests it."""
    flat = realtime_tool_schema(CHAT_SHAPE)

    assert len(flat) == 1
    tool = flat[0]
    assert tool["type"] == "function"
    assert tool["name"] == "webSearch"
    assert tool["description"] == "Search the web."
    assert tool["parameters"]["required"] == ["query"]
    # The nested form must be gone, not merely duplicated alongside.
    assert "function" not in tool


@pytest.mark.unit
def test_every_offered_tool_survives_translation():
    """A tool dropped here is a capability the voice front end silently lacks."""
    chat_tools = [
        {"type": "function", "function": {"name": f"tool{i}", "description": "d", "parameters": {}}}
        for i in range(5)
    ]
    assert [t["name"] for t in realtime_tool_schema(chat_tools)] == [
        f"tool{i}" for i in range(5)
    ]


@pytest.mark.unit
def test_a_tool_with_no_parameters_still_gets_a_valid_object_schema():
    """An absent schema must not become `null`; providers reject that."""
    flat = realtime_tool_schema(
        [{"type": "function", "function": {"name": "stop", "description": "d"}}]
    )
    assert flat[0]["parameters"]["type"] == "object"


@pytest.mark.unit
def test_malformed_entries_are_skipped_rather_than_killing_the_session():
    flat = realtime_tool_schema(
        [{"type": "function"}, {"nonsense": True}, *CHAT_SHAPE]
    )
    assert [t["name"] for t in flat] == ["webSearch"]


# ── Dispatch ──────────────────────────────────────────────────────────


class _Cfg:
    """Minimal settings stand-in; the bridge only passes it through."""


@pytest.mark.unit
def test_a_successful_call_returns_the_tool_output_against_its_call_id():
    call = FunctionCall(call_id="c1", name="webSearch", arguments_json='{"query": "rain"}')

    with patch(
        "jarvis.realtime.tool_bridge.run_tool_with_retries",
        return_value=ToolExecutionResult(success=True, reply_text="it is raining"),
    ):
        result = dispatch_function_call(None, _Cfg(), call)

    assert result.success is True
    assert result.call_id == "c1"
    assert "it is raining" in result.output


@pytest.mark.unit
def test_parsed_arguments_reach_the_tool_layer():
    """The model sends JSON text; the tool path expects a dict."""
    seen = {}

    def _capture(db, cfg, tool_name, tool_args, *args, **kwargs):
        seen["name"] = tool_name
        seen["args"] = tool_args
        return ToolExecutionResult(success=True, reply_text="ok")

    call = FunctionCall(call_id="c2", name="webSearch", arguments_json='{"query": "rain"}')
    with patch("jarvis.realtime.tool_bridge.run_tool_with_retries", _capture):
        dispatch_function_call(None, _Cfg(), call)

    assert seen["name"] == "webSearch"
    assert seen["args"] == {"query": "rain"}


@pytest.mark.unit
def test_malformed_json_arguments_produce_an_error_not_an_exception():
    """Models emit truncated JSON. The session has to keep talking."""
    call = FunctionCall(call_id="c3", name="webSearch", arguments_json='{"query": "ra')

    result = dispatch_function_call(None, _Cfg(), call)

    assert result.success is False
    assert result.call_id == "c3"
    assert result.output  # something explanatory goes back to the model


@pytest.mark.unit
def test_empty_arguments_are_treated_as_no_arguments():
    """A no-parameter tool is called with empty text, not with `null`."""
    seen = {}

    def _capture(db, cfg, tool_name, tool_args, *args, **kwargs):
        seen["args"] = tool_args
        return ToolExecutionResult(success=True, reply_text="ok")

    with patch("jarvis.realtime.tool_bridge.run_tool_with_retries", _capture):
        dispatch_function_call(None, _Cfg(), FunctionCall("c4", "stop", ""))

    assert seen["args"] == {}


@pytest.mark.unit
def test_a_refused_tool_comes_back_as_a_failed_result():
    """Whatever the gate or trust store refuses must read as refusal.

    The realtime model is told the call failed and why. Reporting a
    refusal as success would let it narrate a tool run that never happened.
    """
    call = FunctionCall(call_id="c5", name="somethingWithheld", arguments_json="{}")

    with patch(
        "jarvis.realtime.tool_bridge.run_tool_with_retries",
        return_value=ToolExecutionResult(
            success=False, reply_text=None, error_message="tool not available"
        ),
    ):
        result = dispatch_function_call(None, _Cfg(), call)

    assert result.success is False
    assert "tool not available" in result.output


@pytest.mark.unit
def test_a_tool_that_raises_does_not_take_the_session_down():
    """An exception inside a tool ends that call, not the conversation."""
    call = FunctionCall(call_id="c6", name="webSearch", arguments_json="{}")

    with patch(
        "jarvis.realtime.tool_bridge.run_tool_with_retries",
        side_effect=RuntimeError("network gone"),
    ):
        result = dispatch_function_call(None, _Cfg(), call)

    assert result.success is False
    assert result.call_id == "c6"


@pytest.mark.unit
def test_the_output_is_json_serialisable_text():
    """It goes back over the wire, so it has to survive encoding."""
    call = FunctionCall(call_id="c7", name="webSearch", arguments_json="{}")

    with patch(
        "jarvis.realtime.tool_bridge.run_tool_with_retries",
        return_value=ToolExecutionResult(success=True, reply_text="ünïcode ✅"),
    ):
        result = dispatch_function_call(None, _Cfg(), call)

    json.dumps({"output": result.output})

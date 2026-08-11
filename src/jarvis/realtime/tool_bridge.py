"""Carry a realtime session's function calls into Jarvis's tool path.

Two shapes meet here. A realtime session names a tool at the top level of
its schema, while ``generate_tools_json_schema`` emits the nested
Chat-Completions form, so the offered tools are flattened on the way out.
Coming back the other way, a function call is text the model generated,
which means the arguments may not parse and the name may not exist.

Nothing in here decides whether a tool is allowed to run. That decision
stays with ``run_tool_with_retries`` and the gate behind it, so a realtime
session is a new way to ask rather than a new authority.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..debug import debug_log
from ..tools.registry import run_tool_with_retries


@dataclass(frozen=True)
class FunctionCall:
    """A tool the realtime model wants run, as it came off the wire."""

    call_id: str
    name: str
    arguments_json: str


@dataclass(frozen=True)
class FunctionResult:
    """What goes back to the model in place of that call."""

    call_id: str
    output: str
    success: bool


def realtime_tool_schema(chat_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten Chat-Completions tool definitions for a realtime session.

    Entries that carry no usable name are dropped rather than passed on:
    a provider rejects the whole session over one malformed tool, which
    would cost every other tool as well.
    """
    flattened: List[Dict[str, Any]] = []

    for entry in chat_tools or []:
        if not isinstance(entry, dict):
            continue
        fn = entry.get("function")
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name") or "").strip()
        if not name:
            continue

        parameters = fn.get("parameters")
        if not isinstance(parameters, dict) or not parameters:
            parameters = {"type": "object", "properties": {}, "required": []}

        flattened.append(
            {
                "type": "function",
                "name": name,
                "description": str(fn.get("description") or ""),
                "parameters": parameters,
            }
        )

    dropped = len(chat_tools or []) - len(flattened)
    if dropped:
        debug_log(f"realtime: dropped {dropped} malformed tool definition(s)")

    return flattened


def _parse_arguments(arguments_json: str) -> Optional[Dict[str, Any]]:
    """Return the call's arguments, or ``None`` when they do not parse.

    Empty text means a tool that takes nothing, which is a real call and
    not a failure.
    """
    raw = (arguments_json or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def dispatch_function_call(
    db: Any,
    cfg: Any,
    call: FunctionCall,
    *,
    system_prompt: str = "",
    transcript: str = "",
) -> FunctionResult:
    """Run one function call and return what the model should be told.

    Every outcome is a ``FunctionResult``. A session that raises here stops
    talking mid-conversation, so a failed tool, an unparseable argument
    list and a tool that throws all come back as a refusal the model can
    read out and recover from.
    """
    arguments = _parse_arguments(call.arguments_json)
    if arguments is None:
        debug_log(f"🔌 realtime: unparseable arguments for {call.name}")
        return FunctionResult(
            call_id=call.call_id,
            output=f"The arguments for {call.name} were not valid JSON. Ask again.",
            success=False,
        )

    debug_log(f"🔧 realtime: dispatching {call.name}")

    try:
        result = run_tool_with_retries(
            db,
            cfg,
            call.name,
            arguments,
            system_prompt,
            transcript,
            transcript,
        )
    except Exception as exc:  # a tool must not take the conversation down
        debug_log(f"⚠️ realtime: {call.name} raised: {exc}")
        return FunctionResult(
            call_id=call.call_id,
            output=f"{call.name} failed: {exc}",
            success=False,
        )

    if not result.success:
        reason = result.error_message or "the tool reported no result"
        debug_log(f"🚫 realtime: {call.name} refused or failed: {reason}")
        return FunctionResult(call_id=call.call_id, output=reason, success=False)

    return FunctionResult(
        call_id=call.call_id,
        output=result.reply_text or "",
        success=True,
    )

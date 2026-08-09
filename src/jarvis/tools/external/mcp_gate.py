"""Whether an external MCP tool call may run right now.

An MCP server's tool descriptions reach the model as instructions, and
web pages, transcripts and file contents reach it as tool *results*. So
the model's decision to call a consequential external tool can be the
product of text an attacker wrote. Something has to stand between that
decision and the action.

That something is YOLO mode (``jarvis.approval``): a window the user
opens by hand, during which Jarvis gets on with things. Outside the
window a consequential call does not run — the model is told to ask the
user to turn YOLO on, and the user turns it on in the UI. The model has
no way to open the window itself, which is the whole point; see
``jarvis/approval.py``.

Read-only tools are unaffected and run whether or not YOLO is on.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from ... import approval
from ...debug import debug_log


class GateOutcome(Enum):
    """What the gate decided about a call."""

    ALLOWED = "allowed"    # run it
    BLOCKED = "blocked"    # nothing ran; the user has to enable YOLO


def describe_call(
    server_name: str, tool_name: str, arguments: Optional[Dict[str, Any]]
) -> str:
    """A one-line, human-readable summary of what was asked for."""
    if not arguments:
        return f"{tool_name} on {server_name}"
    try:
        rendered = ", ".join(
            f"{k}={json.dumps(v, default=str)}" for k, v in sorted(arguments.items())
        )
    except Exception:  # noqa: BLE001
        rendered = str(arguments)
    if len(rendered) > 300:
        rendered = rendered[:297] + "..."
    return f"{tool_name} on {server_name} with {rendered}"


def check_allowed(
    server_name: str,
    tool_name: str,
    arguments: Optional[Dict[str, Any]],
) -> Tuple[GateOutcome, str]:
    """Decide whether this call may run, and return text for the model.

    The blocked message names the action and says plainly what the user
    has to do, because the model's job at that point is to relay a
    request, not to retry.
    """
    if approval.is_active():
        debug_log(
            f"MCP '{server_name}__{tool_name}' allowed: YOLO is on "
            f"({approval.describe_remaining()})",
            "mcp",
        )
        return GateOutcome.ALLOWED, ""

    description = describe_call(server_name, tool_name, arguments)
    debug_log(f"MCP '{server_name}__{tool_name}' blocked: YOLO is off", "mcp")
    return (
        GateOutcome.BLOCKED,
        f"NOT DONE: {description}. This needs YOLO mode, which is currently off. "
        "Tell the user what you were about to do and ask them to turn YOLO on "
        "from the Jarvis tray menu or the dashboard. You cannot turn it on "
        "yourself. Do not retry until they say it is on.",
    )

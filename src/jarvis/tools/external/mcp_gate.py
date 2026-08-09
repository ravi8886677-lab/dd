"""Confirmation gate for external MCP tool calls.

An MCP server's tool descriptions reach the model as instructions, and
web pages, transcripts and file contents reach it as tool *results*. So
the model's decision to call a destructive external tool can be the
product of text an attacker wrote. The gate makes that decision
un-actionable on its own.

It works the same way ``computerUse``'s gate does, for the same reason:
a short code is printed to the user's screen on a channel the model
never reads back, so the model cannot approve itself no matter what it
was told. An approval is bound to the exact server, tool and arguments
it was issued for, so a code obtained for a harmless call cannot be
replayed against a damaging one.

Unlike ``computerUse`` there is no trust window here. Computer use
approves a burst of clicks and scrolls that would be unusable one code
at a time; an MCP call that reaches this gate is a discrete, named,
consequential action, and there is no equivalent flood to smooth over.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
import time
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from ...debug import debug_log
from ... import confirm_ui

# Long enough to read a sentence aloud and answer, short enough that a
# stale approval cannot be spent much later in a conversation.
_CODE_TTL_SEC = 180.0

# One pending proposal at a time:
# (code, signature, description, issued_at, (server, tool)).
_pending: Optional[Tuple[str, str, str, float, Tuple[str, str]]] = None


class GateOutcome(Enum):
    """What the gate decided about a call."""

    APPROVED = "approved"    # run it
    PROPOSED = "proposed"    # nothing ran; a code is on the user's screen
    REFUSED = "refused"      # a code was supplied and it was not valid


def reset_pending() -> None:
    """Drop any outstanding proposal."""
    global _pending
    _pending = None
    confirm_ui.dismiss("")


def pending_target() -> Optional[Tuple[str, str]]:
    """The ``(server, tool)`` a proposal is currently waiting on, if any.

    Used to let the approval call through while everything else is held
    back. Identity, not a supplied code, decides this: a read-only tool
    needs no code, so accepting "carries a confirmation_code" as proof
    would let any call name itself the approval.
    """
    if _pending is None:
        return None
    return _pending[4]


def _announce(message: str) -> None:
    """Show the user something the gate depends on.

    Deliberately not the tool layer's ``user_print``: that is suppressed
    under ``voice_debug``, and a confirmation code nobody can read turns
    the tool off permanently instead of failing loudly.
    """
    try:
        print(message, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass


def _signature(server_name: str, tool_name: str, arguments: Optional[Dict[str, Any]]) -> str:
    """Bind an approval to one exact call.

    Arguments are canonicalised because the model may re-serialise its
    own JSON between turns; key order changing must not invalidate an
    approval the user genuinely gave.
    """
    try:
        payload = json.dumps(
            arguments or {}, sort_keys=True, separators=(",", ":"), default=str
        )
    except Exception:  # noqa: BLE001
        payload = repr(arguments)
    material = f"{server_name}\x00{tool_name}\x00{payload}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def describe_call(
    server_name: str, tool_name: str, arguments: Optional[Dict[str, Any]]
) -> str:
    """A one-line, human-readable summary of what is about to happen."""
    if not arguments:
        return f"{tool_name} on {server_name}"
    try:
        rendered = ", ".join(f"{k}={json.dumps(v, default=str)}" for k, v in sorted(arguments.items()))
    except Exception:  # noqa: BLE001
        rendered = str(arguments)
    if len(rendered) > 300:
        rendered = rendered[:297] + "..."
    return f"{tool_name} on {server_name} with {rendered}"


def check_confirmation(
    server_name: str,
    tool_name: str,
    arguments: Optional[Dict[str, Any]],
    supplied_code: Optional[str],
) -> Tuple[GateOutcome, str]:
    """Decide whether this call may run, and return text for the model.

    The returned text never contains the code — it tells the model to
    ask the user for it, because the model genuinely cannot see it.
    """
    global _pending

    signature = _signature(server_name, tool_name, arguments)
    description = describe_call(server_name, tool_name, arguments)

    code = (supplied_code or "").strip()
    if not code:
        return _propose(signature, description, (server_name, tool_name))

    if _pending is None:
        return (
            GateOutcome.REFUSED,
            f"There is no pending confirmation for {tool_name}, so nothing was done. "
            "Propose the action again to get a fresh code.",
        )

    pending_code, pending_signature, pending_description, issued_at, _target = _pending

    if time.time() - issued_at > _CODE_TTL_SEC:
        _pending = None
        _announce("  ⌛ That confirmation code expired — nothing was done.")
        confirm_ui.dismiss("Expired — nothing was done.")
        return (
            GateOutcome.REFUSED,
            "That confirmation code had expired, so nothing was done. Propose the "
            "action again if it is still wanted.",
        )

    if not secrets.compare_digest(code, pending_code):
        # Burn the proposal rather than allowing repeated guesses at a
        # four-digit code.
        _pending = None
        _announce("  🚫 Wrong confirmation code — that request was cancelled.")
        confirm_ui.dismiss("Wrong code — cancelled.")
        debug_log(
            f"MCP gate refused '{server_name}__{tool_name}': wrong code", "mcp"
        )
        return (
            GateOutcome.REFUSED,
            "That confirmation code was wrong, so the request was cancelled. Ask the "
            "user to read the code again and propose the action afresh.",
        )

    if not secrets.compare_digest(signature, pending_signature):
        _pending = None
        _announce("  🚫 That code was issued for a different action — cancelled.")
        confirm_ui.dismiss("Code was for a different action — cancelled.")
        debug_log(
            f"MCP gate refused '{server_name}__{tool_name}': signature mismatch "
            f"(approved: {pending_description})",
            "mcp",
        )
        return (
            GateOutcome.REFUSED,
            "That code was issued for a different action, so nothing was done. "
            "Propose this action to get its own code.",
        )

    # Single use: spend it.
    _pending = None
    confirm_ui.dismiss("")
    debug_log(f"MCP gate approved '{server_name}__{tool_name}'", "mcp")
    return GateOutcome.APPROVED, ""


def _propose(
    signature: str, description: str, target: Tuple[str, str]
) -> Tuple[GateOutcome, str]:
    global _pending

    code = f"{secrets.randbelow(9000) + 1000}"
    _pending = (code, signature, description, time.time(), target)

    _announce(
        f"\n  ⚠️ Jarvis wants to run: {description}\n"
        f"  🔐 To allow it, tell Jarvis this code: {code}\n"
    )
    # ...and again somewhere the user is actually looking. The stderr
    # line above stays: it is the CLI path and the fallback if no UI is
    # registered, so this call can only ever add a channel.
    confirm_ui.present_code(code, description, _CODE_TTL_SEC)
    debug_log(f"MCP gate proposed: {description}", "mcp")

    return (
        GateOutcome.PROPOSED,
        f"PROPOSED (not done yet): {description}. A confirmation code has been shown "
        "on the user's screen. Tell the user what this will do and ask them to read "
        "you the code. Do not guess it — you cannot see it. When they give it to you, "
        "call the same tool again with the same arguments plus confirmation_code.",
    )

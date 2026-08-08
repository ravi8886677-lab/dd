"""computerUse — see the screen, then click and type on it.

Screenshot → vision model → coordinates → `pyautogui`. No OCR engine:
current vision models return click points directly, so the heavyweight
grounding stack other frameworks carry is not needed.

## Why confirmation is enforced rather than requested

Everything else in Jarvis fails quietly when it is wrong. This does not
— a bad coordinate does not produce a bad sentence, it *clicks
something*. On the user's real desktop. Possibly Send, or Delete, or a
purchase button.

So the gate is not a prompt instruction. Asking the model nicely to
confirm is worthless when the same model decides whether it confirmed:
it can call itself approved. Instead:

1. A proposal prints a short code **to the user's screen only** — via
   ``context.user_print``, which never enters the model's context.
2. Nothing executes until that code comes back as an argument.

The model cannot supply the code unless a human read it and passed it
on. That makes the human a real link in the chain rather than a
formality, and it holds even if a web page the assistant just read told
it to click something.

Codes are single-use and bound to the exact action proposed, so an
approval for "click Play" cannot be replayed to authorise a different
click.

## Why it is not a code every time

A gate people meet on every scroll is a gate they learn to clear without
reading, which is worse than no gate — it trains the habit the gate
exists to prevent. So one approval opens a window covering ordinary
actions, the way sudo does.

The window never covers typing or key presses. The asymmetry is the
point: a mistaken click is a mistaken click, while a mistaken Enter
sends the email or empties the folder. Approving a risky action does not
open the window either, so consenting to type once cannot silently
unlock clicking.
"""

from __future__ import annotations

import secrets
import time
from typing import Any, Dict, Optional, Tuple

from ...debug import debug_log
from ..base import Tool, ToolContext
from ..types import ToolExecutionResult

# How long a confirmation code stays valid. Long enough to read a
# sentence and reply, short enough that a stale approval cannot be used
# against a screen that has since changed.
_CONFIRM_TTL_SEC = 120.0

# One pending proposal at a time: (code, description, action, issued_at).
_pending: Optional[Tuple[str, str, Dict[str, Any], float]] = None

# When the user last approved something. Confirming every click makes the
# tool unusable — nobody reads a code to scroll — so an approval opens a
# trust window, the way sudo does. Risky actions ignore it entirely.
_trusted_until: float = 0.0

# How long one approval covers ordinary actions. Long enough to work
# through a task, short enough that walking away closes it.
_TRUST_WINDOW_SEC = 900.0

# Actions that can submit, destroy, or enter text somewhere unintended.
# These always ask, however recently the user approved something else:
# the cost of a wrong click is a wrong click, but the cost of a wrong
# Enter is a sent email or a deleted file.
_ALWAYS_CONFIRM = {"type", "key"}

# How much to confirm. Set ``computer_use_confirm`` in config.json:
#
#   "risky"  (default) — ask once, then trust ordinary actions for a
#                        window; typing and key presses always ask.
#   "always"           — ask for every single action.
#   "never"            — ask for nothing.
#
# "never" is a real choice for a trusted single-user machine, and it is
# how sandboxed demos appear to behave — but the sandbox is doing the
# work there. On a real desktop nothing else catches a misread
# coordinate before it lands on Send or Delete, and the assistant reads
# attacker-controlled web pages, so an injected "click here" executes.
_MODES = ("always", "risky", "never")


def _mode(context: ToolContext) -> str:
    raw = str(getattr(context.cfg, "computer_use_confirm", "risky") or "risky").lower()
    return raw if raw in _MODES else "risky"

_ACTIONS = ("click", "double_click", "right_click", "type", "key", "scroll", "move")

# Keystrokes that are destructive or hard to undo when the model has
# misread the screen. Not a security boundary — the confirmation code is
# that — but these are worth naming in the proposal so the human sees
# what they are approving.
_NOTABLE_KEYS = {"enter", "return", "delete", "backspace", "tab"}


def _describe(action: Dict[str, Any]) -> str:
    kind = action.get("action", "")
    x, y = action.get("x"), action.get("y")
    if kind in ("click", "double_click", "right_click", "move"):
        target = action.get("target") or "that point"
        return f"{kind.replace('_', ' ')} on {target} at ({x}, {y})"
    if kind == "type":
        text = action.get("text", "")
        preview = text if len(text) <= 60 else text[:60] + "…"
        return f"type {preview!r}"
    if kind == "key":
        return f"press {action.get('key', '')}"
    if kind == "scroll":
        return f"scroll {action.get('amount', 0)}"
    return kind


def _pyautogui():
    """Import lazily: the package needs a real display, and the rest of
    Jarvis must import cleanly on a headless machine."""
    import pyautogui

    # Leaving failsafe on means slamming the pointer into a screen corner
    # aborts whatever this is doing — the user's physical stop button.
    pyautogui.FAILSAFE = True
    return pyautogui


class ComputerUseTool(Tool):
    name = "computerUse"
    description = (
        "Control the mouse and keyboard to operate applications the user can see "
        "— click buttons, type into fields, scroll, press keys. Use for requests "
        "like 'click play', 'fill in this form', 'press enter'. Call with "
        "action='screenshot' first to see the screen and work out coordinates. "
        "The first action asks for a confirmation code shown on the user's screen: "
        "say what you intend to do and wait for them to read it to you — you cannot "
        "see it yourself. After one approval, clicking, scrolling and moving run "
        "without asking again for a while; typing and key presses always ask."
    )
    inputSchema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ("screenshot",) + _ACTIONS,
                "description": "What to do. 'screenshot' returns the screen size and needs no confirmation.",
            },
            "x": {"type": "integer", "description": "Horizontal pixel position."},
            "y": {"type": "integer", "description": "Vertical pixel position."},
            "text": {"type": "string", "description": "Text to type."},
            "key": {"type": "string", "description": "Key name, e.g. enter, tab, esc."},
            "amount": {"type": "integer", "description": "Scroll amount; negative scrolls down."},
            "target": {"type": "string", "description": "What is being clicked, in plain words, for the user to check."},
            "confirmation_code": {
                "type": "string",
                "description": "The code shown on the user's screen. Without it, the action is only proposed.",
            },
        },
        "required": ["action"],
    }

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        global _pending, _trusted_until
        args = args or {}
        action = str(args.get("action", "")).strip().lower()

        if action == "screenshot":
            return self._screenshot()

        if action not in _ACTIONS:
            return ToolExecutionResult(
                success=False, reply_text=None,
                error_message=f"Unknown action '{action}'. Known: {', '.join(_ACTIONS)}.",
            )

        supplied = str(args.get("confirmation_code", "") or "").strip()

        # Inside a trust window, ordinary actions run straight away. The
        # user has already said yes to this session; asking again for
        # every scroll teaches them to approve without reading.
        if not supplied and _mode(context) == "never":
            payload = self._payload(args, action)
            return self._execute(payload, _describe(payload), context)

        if not supplied and _mode(context) == "risky" and self._covered_by_trust(action, args):
            return self._execute(self._payload(args, action), _describe(self._payload(args, action)), context)

        if not supplied:
            return self._propose(args, action, context)

        # A code was given: it must match the pending proposal exactly.
        if _pending is None:
            return ToolExecutionResult(
                success=False, reply_text=None,
                error_message="Nothing is awaiting confirmation. Propose the action first.",
            )

        code, description, pending_action, issued = _pending
        if time.time() - issued > _CONFIRM_TTL_SEC:
            _pending = None
            return ToolExecutionResult(
                success=False, reply_text=None,
                error_message="That confirmation expired. Propose the action again.",
            )
        if not secrets.compare_digest(supplied, code):
            return ToolExecutionResult(
                success=False, reply_text=None,
                error_message="That confirmation code is not correct. Nothing was done.",
            )

        # Bind the approval to what was actually shown to the user, so an
        # approved click cannot be swapped for a different one.
        if self._signature(args, action) != self._signature(pending_action, pending_action["action"]):
            _pending = None
            return ToolExecutionResult(
                success=False, reply_text=None,
                error_message=(
                    "That code was issued for a different action. Nothing was done; "
                    "propose this one and ask the user again."
                ),
            )

        _pending = None
        # This approval also covers ordinary actions for a while, so the
        # user is not re-reading codes throughout one task.
        if _mode(context) == "risky" and pending_action["action"] not in _ALWAYS_CONFIRM:
            _trusted_until = time.time() + _TRUST_WINDOW_SEC
            context.user_print(
                f"  🔓 Approved. Clicking and scrolling will not ask again for "
                f"{int(_TRUST_WINDOW_SEC / 60)} minutes. Typing and key presses "
                f"still will."
            )
        return self._execute(pending_action, description, context)

    def _payload(self, args: Dict[str, Any], action: str) -> Dict[str, Any]:
        payload = {k: args.get(k) for k in ("x", "y", "text", "key", "amount", "target")}
        payload["action"] = action
        return payload

    def _covered_by_trust(self, action: str, args: Dict[str, Any]) -> bool:
        """Whether this action may run on an earlier approval."""
        if action in _ALWAYS_CONFIRM:
            return False
        return time.time() < _trusted_until

    def _signature(self, action: Dict[str, Any], kind: str) -> tuple:
        return (
            kind,
            action.get("x"), action.get("y"),
            action.get("text"), action.get("key"), action.get("amount"),
        )

    def _propose(self, args: Dict[str, Any], action: str, context: ToolContext) -> ToolExecutionResult:
        global _pending

        payload = self._payload(args, action)
        description = _describe(payload)

        code = f"{secrets.randbelow(9000) + 1000}"
        _pending = (code, description, payload, time.time())

        # Printed to the user's terminal only. This never reaches the
        # model, which is the entire point — the model cannot approve
        # itself.
        note = ""
        if action == "key" and str(args.get("key", "")).lower() in _NOTABLE_KEYS:
            note = "  ⚠️ This key can submit or delete things."
        context.user_print(
            f"\n  🖱️ Jarvis wants to: {description}\n"
            f"{note}"
            f"  🔐 To allow it, tell Jarvis this code: {code}\n"
            f"     Ignore it to do nothing. Expires in {int(_CONFIRM_TTL_SEC)}s.\n"
        )
        debug_log(f"computerUse proposed: {description}", "computer_use")

        return ToolExecutionResult(
            success=True,
            reply_text=(
                f"PROPOSED (not done yet): {description}. A confirmation code has been "
                f"shown on the user's screen. Tell the user what you intend to do and ask "
                f"them to read you the code. Do not guess it — you cannot see it. When "
                f"they give it, call computerUse again with the same arguments plus "
                f"confirmation_code."
            ),
        )

    def _screenshot(self) -> ToolExecutionResult:
        try:
            pg = _pyautogui()
            width, height = pg.size()
        except Exception as e:
            return ToolExecutionResult(
                success=False, reply_text=None,
                error_message=(
                    f"Cannot see the screen: {e}. This needs a desktop session — "
                    f"it does not work over SSH or in a container."
                ),
            )
        return ToolExecutionResult(
            success=True,
            reply_text=(
                f"Screen is {width}x{height} pixels, origin top-left. Use the attached "
                f"view to choose coordinates within that range."
            ),
        )

    def _execute(self, action: Dict[str, Any], description: str, context: ToolContext) -> ToolExecutionResult:
        kind = action["action"]
        try:
            pg = _pyautogui()
            x, y = action.get("x"), action.get("y")

            if kind == "click":
                pg.click(x, y)
            elif kind == "double_click":
                pg.doubleClick(x, y)
            elif kind == "right_click":
                pg.rightClick(x, y)
            elif kind == "move":
                pg.moveTo(x, y)
            elif kind == "type":
                # A small interval: some applications drop characters from
                # an instantaneous burst.
                pg.write(action.get("text", ""), interval=0.02)
            elif kind == "key":
                pg.press(str(action.get("key", "")).lower())
            elif kind == "scroll":
                pg.scroll(int(action.get("amount") or 0))
        except Exception as e:
            debug_log(f"computerUse failed: {e}", "computer_use")
            return ToolExecutionResult(
                success=False, reply_text=None,
                error_message=f"Could not {description}: {e}",
            )

        debug_log(f"computerUse executed: {description}", "computer_use")
        context.user_print(f"  ✅ Done: {description}")
        return ToolExecutionResult(success=True, reply_text=f"Done: {description}.")

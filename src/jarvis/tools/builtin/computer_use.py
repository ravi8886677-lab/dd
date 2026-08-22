"""computerUse — click and type on the user's desktop.

`pyautogui` drives the mouse and keyboard, behind a confirmation gate.

## What is missing: eyes

The intended pipeline is screenshot → vision model → coordinates →
click, which needs no OCR engine because current vision models return
click points directly. The last three steps exist. The first does not:
nothing in `jarvis/llm/` sends images, so `action="screenshot"` can only
report the screen size, and the model has no way to see what is on it.

Until an image path exists, coordinates must come from the user. The
screenshot response says so explicitly, because the earlier wording
("use the attached view") invited the model to invent a coordinate and
click it. This tool is therefore usable for "click at 500,400" and not
yet for "click the Play button".

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
from ... import approval
from ..base import Tool, ToolContext
from ..types import ToolExecutionResult


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
    return _mode_from_cfg(context.cfg)


def _mode_from_cfg(cfg: Any) -> str:
    raw = str(getattr(cfg, "computer_use_confirm", "risky") or "risky").lower()
    return raw if raw in _MODES else "risky"


#: Names the rule in the action log, so a denial is explainable.
YOLO_RULE_ID = "computer_use.yolo"


def physical_action_is_permitted(cfg: Any, action: str) -> bool:
    """Whether driving the mouse or keyboard is allowed right now.

    One rule, two callers. The boundary asks before executing anything,
    so the decision is recorded before the fact; the tool asks again on
    its own path, so calling it directly cannot walk past the gate. They
    share this function rather than each carrying a copy, because two
    copies of a security rule become two rules.
    """
    if action not in _ACTIONS:
        return True
    return _mode_from_cfg(cfg) == "never" or approval.is_active()

_ACTIONS = ("click", "double_click", "right_click", "type", "key", "scroll", "move")

# Keystrokes that are destructive or hard to undo when the model has
# misread the screen. Not a security boundary — the confirmation code is
# that — but these are worth naming in the proposal so the human sees
# what they are approving.
_NOTABLE_KEYS = {"enter", "return", "delete", "backspace", "tab"}


def _announce(message: str) -> None:
    """Show the user something the gate depends on.

    Deliberately not ``context.user_print`` — that is suppressed under
    ``voice_debug``, and a confirmation code nobody can read locks the
    tool permanently instead of failing loudly.
    """
    import sys

    try:
        print(message, file=sys.stderr, flush=True)
    except Exception:
        pass


# Actions that are meaningless without a target. pyautogui treats a
# missing coordinate as "wherever the pointer already is", so an absent
# x/y silently becomes a click on whatever the cursor happens to be
# over — the one failure the confirmation cannot catch, because the
# proposal the user approved said "click on Send".
_NEEDS_COORDS = {"click", "double_click", "right_click", "move"}


def _validate_coords(action: str, args: Dict[str, Any]) -> Optional[str]:
    """Return a complaint if the coordinates are unusable."""
    if action not in _NEEDS_COORDS:
        return None

    x, y = args.get("x"), args.get("y")
    if x is None or y is None:
        return (
            f"'{action}' needs both x and y. Without them the pointer stays "
            f"where it is and clicks whatever is under it."
        )
    try:
        x, y = int(x), int(y)
    except (TypeError, ValueError):
        return f"x and y must be whole numbers, got {x!r} and {y!r}."
    if x < 0 or y < 0:
        return f"({x}, {y}) is off-screen; coordinates start at (0, 0)."

    # Bounds-check against the real screen where we can see it. pyautogui
    # silently clamps out-of-range points to an edge, which would turn a
    # wildly wrong coordinate into a plausible-looking edge click.
    try:
        width, height = _pyautogui().size()
    except Exception:
        return None
    if x >= width or y >= height:
        return f"({x}, {y}) is outside the {width}x{height} screen."
    return None


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
                "description": "What to do. 'screenshot' returns the screen size and always runs.",
            },
            "x": {"type": "integer", "description": "Horizontal pixel position."},
            "y": {"type": "integer", "description": "Vertical pixel position."},
            "text": {"type": "string", "description": "Text to type."},
            "key": {"type": "string", "description": "Key name, e.g. enter, tab, esc."},
            "amount": {"type": "integer", "description": "Scroll amount; negative scrolls down."},
            "target": {"type": "string", "description": "What is being clicked, in plain words, for the user to check."},
        },
        "required": ["action"],
    }

    def is_available(self) -> bool:
        """Only where the input injection library is installed.

        ``pyautogui`` ships with the full requirements, not with the
        audio-free chat subset, so a chat-only install has no way to move a
        pointer. Offering the tool anyway means the model picks it, gets an
        ``ImportError``, and the user watches a turn go nowhere.
        """
        import importlib.util

        try:
            return importlib.util.find_spec("pyautogui") is not None
        except (ImportError, ValueError):
            return False

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        args = args or {}
        action = str(args.get("action", "")).strip().lower()

        if action == "screenshot":
            return self._screenshot()

        if action not in _ACTIONS:
            return ToolExecutionResult(
                success=False, reply_text=None,
                error_message=f"Unknown action '{action}'. Known: {', '.join(_ACTIONS)}.",
            )

        complaint = _validate_coords(action, args)
        if complaint:
            return ToolExecutionResult(success=False, reply_text=None, error_message=complaint)

        payload = self._payload(args, action)
        description = _describe(payload)

        # Everything past here moves a real mouse or types real keys.
        # `never` means the user has opted out of being asked at all.
        if physical_action_is_permitted(context.cfg, action):
            return self._execute(payload, description, context)

        _announce(
            f"\n  🖱️ Jarvis wanted to: {description}\n"
            "  🔒 YOLO mode is off, so nothing was done.\n"
        )
        debug_log(f"computerUse blocked (YOLO off): {description}", "computer_use")
        return ToolExecutionResult(
            success=False,
            reply_text=None,
            error_message=(
                f"NOT DONE: {description}. Controlling the screen needs YOLO mode, "
                "which is currently off. Tell the user what you were about to do "
                "and ask them to turn YOLO on from the Jarvis tray menu or the "
                "dashboard. You cannot turn it on yourself."
            ),
        )

    def _payload(self, args: Dict[str, Any], action: str) -> Dict[str, Any]:
        payload = {k: args.get(k) for k in ("x", "y", "text", "key", "amount", "target")}
        payload["action"] = action
        return payload

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
        # No image is attached, because nothing in the LLM stack accepts
        # one yet — see the module docstring. Saying "use the attached
        # view" invited the model to invent a coordinate and click it.
        return ToolExecutionResult(
            success=True,
            reply_text=(
                f"Screen is {width}x{height} pixels, origin top-left. NOTE: you cannot "
                f"see this screen — no image is available. Do NOT guess coordinates. "
                f"Ask the user where the target is, or ask them to read out its position, "
                f"and use the number they give you."
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

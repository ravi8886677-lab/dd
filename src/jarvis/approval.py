"""YOLO mode: a time-boxed grant to act without asking each time.

Some actions are worth a second thought — deleting things, driving the
mouse, calling an external tool that changes something. Asking about
every one of them individually is the kind of prompt people learn to
clear without reading, so instead the user opens a window: for the next
fifteen or thirty minutes, get on with it.

The grant lives in memory only. A restart drops it, which is the right
default for something that exists to lapse.

## The one rule

**Nothing in the tool layer may call ``grant``.**

Jarvis reads web pages, MCP tool descriptions and tool results, and any
of those can carry text that someone else wrote to be read by a model.
If enabling YOLO were reachable from a tool call, that text could enable
it and then act freely for half an hour. Granting is a human action in
the UI — a tray menu item or a dashboard button — and
``tests/test_yolo.py`` asserts no tool can reach it.

That is the same property the old confirmation codes had, kept while
dropping the per-action friction that made them unusable.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, List, Optional

from .debug import debug_log

# Offered in the UI. The user picks one; these are not a policy.
GRANT_CHOICES_MINUTES = (15, 30)

# A grant is meant to lapse, so there is still a ceiling — but it is set
# at a working day rather than an hour, because the point of choosing
# your own duration is that a long task does not get interrupted.
MAX_GRANT_MINUTES = 480

# The slider's range and step, in minutes. Exposed so the UI and the
# validation agree on what is offerable.
MIN_GRANT_MINUTES = 5
GRANT_STEP_MINUTES = 5

_lock = threading.Lock()
_active_until: float = 0.0

# Callbacks the UI registers to keep a countdown or a toggle in step.
_listeners: List[Callable[[bool, float], None]] = []


def _coerce_minutes(minutes: Any) -> Optional[float]:
    """Return a usable duration, or ``None`` if this is not one.

    Deliberately strict. ``True`` is an ``int`` in Python and ``"30"``
    looks like a number, but neither is someone choosing half an hour,
    and a duration that arrives from anywhere but a UI click is a bug
    worth failing on rather than guessing at.
    """
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
        return None
    value = float(minutes)
    if value != value or value <= 0:  # NaN or non-positive
        return None
    return min(value, float(MAX_GRANT_MINUTES))


def grant(minutes: Any) -> bool:
    """Open the window for ``minutes``. Returns whether it is now open.

    Call this only from a human action in the UI. See the module
    docstring for why that matters.
    """
    duration = _coerce_minutes(minutes)
    if duration is None:
        debug_log(f"ignored a YOLO grant of {minutes!r}: not a duration", "approval")
        return False

    with _lock:
        # A fresh window, not an extension of the old one, so "15 minutes"
        # always means fifteen minutes from now.
        _set_until(time.time() + duration * 60.0)
    debug_log(f"YOLO enabled for {duration:g} minutes", "approval")
    _notify()
    return True


def revoke() -> None:
    """Close the window now."""
    with _lock:
        was_active = time.time() < _active_until
        _set_until(0.0)
    if was_active:
        debug_log("YOLO disabled", "approval")
    _notify()


def is_active() -> bool:
    """Whether Jarvis may currently act without asking."""
    with _lock:
        return time.time() < _active_until


def remaining_sec() -> float:
    """Seconds left on the window, or ``0.0`` when it is closed."""
    with _lock:
        return max(0.0, _active_until - time.time())


def describe_duration(minutes: float) -> str:
    """Render a duration the way someone would say it aloud.

    "480 min" is not a thing anyone says, and the slider now reaches
    eight hours.
    """
    total = int(round(minutes))
    if total < 60:
        return f"{total} min"
    hours, rest = divmod(total, 60)
    return f"{hours}h" if rest == 0 else f"{hours}h {rest}m"


def describe_remaining() -> str:
    """A short human phrase for the time left, for UI labels."""
    seconds = remaining_sec()
    if seconds <= 0:
        return "off"
    if seconds < 60:
        return f"{int(seconds)}s left"
    return f"{describe_duration(seconds / 60)} left"


def add_listener(callback: Callable[[bool, float], None]) -> None:
    """Register a UI callback, called with ``(is_active, remaining_sec)``."""
    with _lock:
        _listeners.append(callback)


def clear_listeners() -> None:
    with _lock:
        _listeners.clear()


def _set_until(value: float) -> None:
    """Assign the deadline. Caller holds the lock."""
    global _active_until
    _active_until = value


def _notify() -> None:
    """Tell the UI the state moved. Never raises into the caller."""
    with _lock:
        listeners = list(_listeners)
    active, remaining = is_active(), remaining_sec()
    for callback in listeners:
        try:
            callback(active, remaining)
        except Exception as e:  # noqa: BLE001 - a broken UI must not break the grant
            debug_log(f"YOLO listener error: {e}", "approval")

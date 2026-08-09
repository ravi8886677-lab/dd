"""Where a confirmation code actually reaches the human.

Both gates print their code to stderr. That is right for a terminal and
wrong for the packaged desktop app: there ``sys.stderr`` is replaced by a
writer that feeds the Log Viewer window, and that window is hidden until
someone opens it from the tray. The gate still fails closed, so nothing
is unsafe — but a code nobody can find is a tool that can never be used,
which is the same outcome as the tool being switched off.

A *presenter* is a callback the UI layer registers to put the code
somewhere the human is already looking. The stderr line is still printed
by the caller either way: it is the fallback for CLI and dev runs, and it
is what stops this module from being able to break the gate. If no
presenter is registered, or it raises, behaviour is exactly what it is
today.

Two properties this module exists to protect:

1. The presenter is handed only text that already goes to stderr. It is
   never given anything the model can read back, and it never returns
   anything to the caller — an approval cannot originate here.

2. ``is_showing()`` lets the screen-reading tools know a code is on
   screen right now. Rendering the code in a visible window moves it out
   of a channel the model cannot read and into one it can: the builtin
   ``screenshot`` tool OCRs the display and returns the text to the
   model, so a four-digit code sitting in a dialog is a four-digit code
   the model can fetch and then supply to the gate it is waiting on.
   That chain has to be cut on the reading side, not the drawing side,
   because OS-level capture exclusion does not exist everywhere.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

# code, description, seconds until it expires
Presenter = Callable[[str, str, float], None]
# a short human-readable reason the proposal is no longer live
Dismisser = Callable[[str], None]

_lock = threading.Lock()
_presenter: Optional[Presenter] = None
_dismisser: Optional[Dismisser] = None

# When the currently displayed code stops being displayed. Tracked here
# rather than read from either gate because both feed this module, and
# because the reading tools need one question to ask, not two.
_showing_until: float = 0.0


def set_presenter(present: Presenter, dismiss: Optional[Dismisser] = None) -> None:
    """Register the UI that will show confirmation codes.

    Called by the desktop app at startup. Safe to call again to replace.
    """
    global _presenter, _dismisser
    with _lock:
        _presenter = present
        _dismisser = dismiss


def clear_presenter() -> None:
    """Forget the registered UI. Codes fall back to stderr only."""
    global _presenter, _dismisser
    with _lock:
        _presenter = None
        _dismisser = None


def present_code(code: str, description: str, ttl_sec: float) -> None:
    """Ask the UI to show a code. Never raises, never blocks the gate.

    The caller has already printed its own stderr line before getting
    here, so a failure in here costs nothing that was not already there.
    """
    global _showing_until
    with _lock:
        presenter = _presenter
        _showing_until = time.time() + max(0.0, float(ttl_sec))

    if presenter is None:
        return
    try:
        presenter(code, description, float(ttl_sec))
    except Exception:  # noqa: BLE001 - a broken UI must not wedge the gate
        pass


def dismiss(reason: str = "") -> None:
    """Tell the UI the proposal is over: spent, expired, or refused."""
    global _showing_until
    with _lock:
        dismisser = _dismisser
        _showing_until = 0.0

    if dismisser is None:
        return
    try:
        dismisser(reason)
    except Exception:  # noqa: BLE001
        pass


def is_showing() -> bool:
    """True while a confirmation code is (or should be) on screen.

    Read by the screen-reading tools so they can refuse for the length of
    the window rather than handing the model the code it is waiting for.
    Expiry is checked against the clock rather than trusted to a
    ``dismiss`` call, so a gate that dies mid-proposal unblocks the
    screenshot tool on its own instead of disabling it forever.
    """
    with _lock:
        return time.time() < _showing_until

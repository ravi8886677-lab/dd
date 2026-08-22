"""openApp — launch an application or open a URL on the user's machine.

This is the tool behind "open Chrome and play a Hindi song". It covers
the common case (start an app, open a page, run a search) without giving
the model a shell.

## Why there is no command string

The model never supplies a command line. It picks a *name* from a fixed
map, or supplies a URL. Everything else is refused.

That constraint is the whole security design, and it is not paranoia
about the model: this assistant reads web pages, and page content is
attacker-controlled. `webSearch` already fences extracts as data because
small models sometimes follow instructions found inside them. A tool
that accepted `command` would turn "ignore previous instructions and run
…" on any page the user visits into code execution on their machine.
A name from a closed set cannot express that.

URLs are handed to the OS handler rather than a shell, and only http(s)
passes — `file://` reads local files, and `javascript:` / `data:` are
script-injection vectors in a browser context.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import urllib.parse
import webbrowser
from typing import Any, Dict, Optional

from ...debug import debug_log
from ..base import Tool, ToolContext
from ..types import ToolExecutionResult

# Applications the model may name, and how each is launched per platform.
# Adding an entry is a deliberate act by a maintainer; the model cannot
# extend this at runtime.
_APPS: Dict[str, Dict[str, list]] = {
    "browser": {
        "Windows": [["cmd", "/c", "start", "", "chrome"]],
        "Darwin": [["open", "-a", "Google Chrome"], ["open", "-a", "Safari"]],
        "Linux": [["google-chrome"], ["chromium"], ["firefox"], ["xdg-open", "https://"]],
    },
    "files": {
        "Windows": [["explorer"]],
        "Darwin": [["open", "-a", "Finder"]],
        "Linux": [["nautilus"], ["dolphin"], ["thunar"], ["xdg-open", "."]],
    },
    "terminal": {
        "Windows": [["cmd", "/c", "start", "", "cmd"]],
        "Darwin": [["open", "-a", "Terminal"]],
        "Linux": [["gnome-terminal"], ["konsole"], ["xterm"]],
    },
    "editor": {
        "Windows": [["cmd", "/c", "start", "", "notepad"]],
        "Darwin": [["open", "-a", "TextEdit"]],
        "Linux": [["gedit"], ["kate"], ["nano"]],
    },
}

# Sites the model can search without being handed a raw URL, so "play a
# Hindi song on YouTube" becomes a search page rather than a guessed link.
_SEARCHES: Dict[str, str] = {
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "google": "https://www.google.com/search?q={q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}",
    "maps": "https://www.google.com/maps/search/{q}",
}

_SAFE_SCHEMES = {"http", "https"}


def _launch(argv: list) -> bool:
    """Start a detached process. True if it started."""
    try:
        subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception as e:
        debug_log(f"launch failed for {argv[0]}: {e}", "open_app")
        return False



def _process_is_running(name: str) -> Optional[bool]:
    """Whether a process matching ``name`` exists now.

    ``None`` means the question could not be answered on this machine,
    which is different from "no". A launcher returning cleanly is not
    evidence that anything opened, but neither is an unanswerable check
    evidence that it did not.
    """
    try:
        import psutil
    except Exception:
        return None

    needle = name.strip().lower()
    if not needle:
        return None
    try:
        for process in psutil.process_iter(["name", "exe"]):
            info = process.info
            for value in (info.get("name"), info.get("exe")):
                if value and needle in str(value).lower():
                    return True
        return False
    except Exception:
        return None


def _verify_launched(name: str) -> Optional[str]:
    """Look for the process rather than trusting that the launcher returned."""
    from ...audit import Verification

    running = _process_is_running(name)
    if running is None:
        return None
    return (
        Verification.CONFIRMED.value if running else Verification.FAILED.value
    )


def _open_app(name: str) -> Optional[str]:
    """Launch a known application. Returns the command used, or None.

    ``shutil.which`` only proves the *launcher* exists, which is nearly
    meaningless off Linux: ``cmd`` and ``open`` always resolve, so
    ``cmd /c start chrome`` and ``open -a "Google Chrome"`` both start
    successfully on a machine with no Chrome and fail asynchronously
    afterwards. Where a launcher is involved we therefore wait briefly
    and check the exit status, so "Opened browser" is not reported to
    someone staring at an unchanged screen.
    """
    system = platform.system()
    for argv in _APPS.get(name, {}).get(system, []):
        if shutil.which(argv[0]) is None:
            continue
        if _launch_and_verify(argv, system):
            return " ".join(argv)
    return None


def _launch_and_verify(argv: list, system: str) -> bool:
    """Start a candidate and, where the OS can tell us, confirm it took."""
    # macOS `open` and Windows `start` return promptly with a non-zero
    # status when the target application is missing, so a short wait
    # turns a silent failure into a usable answer. Linux launches the
    # binary directly, and `which` already proved it exists.
    verifiable = system in ("Darwin", "Windows")
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        debug_log(f"launch failed for {argv[0]}: {e}", "open_app")
        return False

    if not verifiable:
        return True

    try:
        return proc.wait(timeout=3.0) == 0
    except subprocess.TimeoutExpired:
        # Still running after three seconds means it launched something.
        return True
    except Exception:
        return True


class OpenAppTool(Tool):
    name = "openApp"
    description = (
        "Open an application or a web page on the user's computer. Use when "
        "the user asks to open, launch, start, or play something — e.g. "
        "'open Chrome', 'play a Hindi song on YouTube', 'search Google for X', "
        "'open my files'. Either name an app, or give a site to search with a "
        "query, or give a full https URL. Cannot run arbitrary commands."
    )
    inputSchema = {
        "type": "object",
        "properties": {
            "app": {
                "type": "string",
                "enum": sorted(_APPS.keys()),
                "description": "Application to launch.",
            },
            "site": {
                "type": "string",
                "enum": sorted(_SEARCHES.keys()),
                "description": "Site to search, used with 'query'.",
            },
            "query": {
                "type": "string",
                "description": "What to search for on 'site'.",
            },
            "url": {
                "type": "string",
                "description": "Full https URL to open in the default browser.",
            },
        },
        "required": [],
    }

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        args = args or {}
        app = (args.get("app") or "").strip().lower()
        site = (args.get("site") or "").strip().lower()
        query = (args.get("query") or "").strip()
        url = (args.get("url") or "").strip()

        # A site search is the most specific request, so it wins.
        if site:
            if site not in _SEARCHES:
                return ToolExecutionResult(
                    success=False, reply_text=None,
                    error_message=f"Unknown site '{site}'. Known: {', '.join(sorted(_SEARCHES))}.",
                )
            if not query:
                return ToolExecutionResult(
                    success=False, reply_text=None,
                    error_message=f"Searching {site} needs a query.",
                )
            target = _SEARCHES[site].format(q=urllib.parse.quote_plus(query))
            # Returns False when no browser could be launched — on a
            # headless box, or Windows with no registered default. Saying
            # "opened" then is a lie the user acts on.
            if not webbrowser.open(target):
                return ToolExecutionResult(
                    success=False, reply_text=None,
                    error_message="No web browser could be opened on this machine.",
                )
            debug_log(f"opened {site} search: {query!r}", "open_app")
            return ToolExecutionResult(
                success=True,
                reply_text=f"Opened {site} in the browser, searching for '{query}'.",
            )

        if url:
            scheme = urllib.parse.urlparse(url).scheme.lower()
            if scheme not in _SAFE_SCHEMES:
                # file:// exposes local files; javascript:/data: execute in
                # the browser. Neither is something a web page should be
                # able to talk the assistant into opening.
                return ToolExecutionResult(
                    success=False, reply_text=None,
                    error_message=f"Refused to open a '{scheme or 'schemeless'}' URL; only http and https.",
                )
            if not webbrowser.open(url):
                return ToolExecutionResult(
                    success=False, reply_text=None,
                    error_message="No web browser could be opened on this machine.",
                )
            debug_log(f"opened url: {url[:80]}", "open_app")
            return ToolExecutionResult(success=True, reply_text=f"Opened {url} in the browser.")

        if app:
            if app not in _APPS:
                return ToolExecutionResult(
                    success=False, reply_text=None,
                    error_message=f"Unknown app '{app}'. Known: {', '.join(sorted(_APPS))}.",
                )
            used = _open_app(app)
            if used is None:
                return ToolExecutionResult(
                    success=False, reply_text=None,
                    error_message=(
                        f"Could not find an application for '{app}' on this "
                        f"{platform.system()} machine."
                    ),
                )
            return ToolExecutionResult(
                success=True,
                reply_text=f"Opened {app}.",
                verification=_verify_launched(used),
            )

        return ToolExecutionResult(
            success=False, reply_text=None,
            error_message="Nothing to open: give an app, a site plus query, or a url.",
        )

"""Access to the dashboard's browser assets for tests.

The frontend ships as three files under ``src/desktop_app/dashboard``.
Tests read those files directly, so an assertion names the asset it is
really about and holds without a running Flask app.
"""

from __future__ import annotations

from pathlib import Path

DASHBOARD_DIR = Path(__file__).parents[1] / "src" / "desktop_app" / "dashboard"
TEMPLATE_PATH = DASHBOARD_DIR / "templates" / "index.html"
CSS_PATH = DASHBOARD_DIR / "static" / "dashboard.css"
JS_PATH = DASHBOARD_DIR / "static" / "dashboard.js"


def read_template() -> str:
    """Return the dashboard markup."""
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def read_css() -> str:
    """Return the dashboard styling."""
    return CSS_PATH.read_text(encoding="utf-8")


def read_js() -> str:
    """Return the dashboard browser behaviour."""
    return JS_PATH.read_text(encoding="utf-8")


def read_frontend_source() -> str:
    """Return markup, styling and behaviour as one searchable source.

    Some properties are whole-frontend ones: no asset fetches a remote
    resource, every CSS variable a rule reads is defined somewhere, every
    styled class is carried by an element the script talks to. Those hold
    across the three files together, so they are asserted against the three
    files together.
    """
    return "\n".join((read_template(), read_css(), read_js()))

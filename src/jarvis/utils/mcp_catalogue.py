"""Load the curated MCP catalogue without importing the desktop package.

The catalogue is plain data, but it lives in ``src/desktop_app`` because the
setup wizard was its first consumer. ``desktop_app/__init__`` imports the
tray application, which needs Qt, so a plain package import would make the
catalogue unreachable anywhere Qt is not installed: a headless install
re-pinning its servers on startup, and the web dashboard serving the
connections directory.

The module is therefore loaded straight from its file.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List

from ..debug import debug_log

_MODULE_NAME = "_jarvis_mcp_catalogue"
# Flask serves the connections directory on several threads, and two of them
# can reach a cold catalogue at once. Without this, the second thread reads
# the half-built module the first has already published to sys.modules and
# renders an empty grid.
_load_lock = threading.Lock()
# One cache for one module, holding the outcome including failure: a
# headless install with no catalogue file would otherwise re-stat the path
# on every connections request, and a broken one would re-exec it.
_loaded = False
_module: Any = None


def _catalogue_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "desktop_app" / "mcp_catalogue.py"


def load_module() -> Any:
    """The catalogue module, or ``None`` if it cannot be loaded.

    Returns ``None`` rather than raising: a missing or broken catalogue must
    degrade to "no curated entries", never stop the dashboard or the config
    migration from running.
    """
    global _module, _loaded

    if _loaded:
        return _module

    with _load_lock:
        # Another thread may have finished while this one waited.
        if _loaded:
            return _module

        module_path = _catalogue_path()
        if not module_path.exists():
            debug_log(f"MCP catalogue not found at {module_path}", "mcp")
            _loaded = True
            return None
        spec = importlib.util.spec_from_file_location(_MODULE_NAME, module_path)
        if spec is None or spec.loader is None:
            _loaded = True
            return None
        module = importlib.util.module_from_spec(spec)
        # Registered before execution because ``dataclass`` resolves the
        # defining module by name while the class body is being built. The
        # module is only published to callers once it is fully executed, so
        # nobody outside this lock ever sees the half-built version.
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as e:  # noqa: BLE001
            debug_log(f"MCP catalogue failed to load: {e}", "mcp")
            sys.modules.pop(spec.name, None)
            _loaded = True
            return None
        _module = module
        _loaded = True
        return _module


def load_entries() -> List[Any]:
    """Catalogue entries in display order."""
    module = load_module()
    if module is None:
        return []
    return list(getattr(module, "CATALOGUE", []) or [])


def load_by_name() -> Dict[str, Any]:
    """Catalogue entries keyed by config name."""
    module = load_module()
    if module is None:
        return {}
    return getattr(module, "CATALOGUE_BY_NAME", {}) or {}

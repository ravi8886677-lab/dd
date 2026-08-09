"""Lazily-imported desktop modules must be declared to PyInstaller.

An import written at module level is found by PyInstaller's static
analysis without help. One written inside a function is found less
reliably, and when it is also wrapped in a ``try``/``except`` the miss is
silent: the feature is simply absent from the packaged build and the log
line saying so goes to a window nobody opens.

That is exactly how the confirmation dialog would have failed. This test
walks the desktop app for function-scoped ``desktop_app.*`` imports and
insists each one is named in the spec's ``hiddenimports``, so the next
module added this way cannot go missing quietly.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = _ROOT / "jarvis_desktop.spec"
_DESKTOP = _ROOT / "src" / "desktop_app"


def _declared_hidden_imports() -> set[str]:
    """Read the spec as text: importing it needs PyInstaller installed."""
    text = _SPEC.read_text(encoding="utf-8")
    block = re.search(r"hiddenimports\s*=\s*\[(.*?)\n\]", text, re.DOTALL)
    assert block, "could not find the hiddenimports list in the spec"
    return set(re.findall(r"['\"]([\w.]+)['\"]", block.group(1)))


def _lazily_imported_desktop_modules() -> set[dict]:
    """Every ``desktop_app.*`` imported from inside a function body."""
    found: set[str] = set()
    for path in sorted(_DESKTOP.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.ImportFrom) and inner.module:
                    if inner.module.startswith("desktop_app."):
                        found.add(inner.module)
                elif isinstance(inner, ast.Import):
                    for alias in inner.names:
                        if alias.name.startswith("desktop_app."):
                            found.add(alias.name)
    return found


@pytest.mark.skipif(not _SPEC.exists(), reason="spec file not present")
def test_lazily_imported_desktop_modules_are_in_hiddenimports():
    declared = _declared_hidden_imports()
    missing = sorted(m for m in _lazily_imported_desktop_modules() if m not in declared)
    assert not missing, (
        "these modules are imported inside a function and are not declared "
        f"in jarvis_desktop.spec, so they may be absent from the packaged "
        f"build with no visible error: {missing}"
    )


@pytest.mark.skipif(not _SPEC.exists(), reason="spec file not present")
def test_the_approval_module_is_packaged():
    """Without it the tray cannot grant YOLO and every gated action dies."""
    declared = _declared_hidden_imports()
    assert "jarvis.approval" in declared


def _tray_init_source() -> str:
    """The body of the tray class's __init__, as text."""
    app_py = (_ROOT / "src" / "desktop_app" / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(app_py)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "JarvisSystemTray":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    return ast.get_source_segment(app_py, item) or ""
    raise AssertionError("could not find JarvisSystemTray.__init__")


def test_no_balloon_is_fired_before_the_tray_icon_is_shown():
    """`showMessage` is a no-op on a tray icon that has not been shown.

    The failure warning here is the only signal a user gets that
    approvals are dead, so firing it while the icon is still hidden
    would send the one message that matters nowhere at all — the same
    shape as the Log Viewer problem it exists to route around.
    """
    source = _tray_init_source()
    lines = source.splitlines()

    show_at = next(
        (i for i, line in enumerate(lines) if "self.tray_icon.show()" in line), None
    )
    assert show_at is not None, "tray_icon.show() not found in __init__"

    premature = [
        i for i, line in enumerate(lines)
        if "showMessage(" in line and i < show_at
    ]
    assert not premature, (
        "tray_icon.showMessage() is called before tray_icon.show() at "
        f"__init__ line(s) {[i + 1 for i in premature]}; a balloon on a "
        "hidden tray icon is silently dropped"
    )

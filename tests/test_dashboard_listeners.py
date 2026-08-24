"""The dashboard's controls are wired when the page loads.

A scripted edit once pasted the entire YOLO section - its state, its four
functions and all three of its listeners - inside the delete-a-memory
click handler, five levels deep in an anonymous callback. The file parsed,
`node --check` passed, and every Python test went on passing, because
nothing in the suite loads the JavaScript. The visible symptom was a
button that did nothing, and the code only came into existence if you
deleted a memory.

`tests/test_no_unreachable_code.py` guards `src/**.py` against the same
shape. This is its counterpart for the dashboard script, and it has to be
behavioural rather than structural: the defect is invisible to a parser
and to a brace-depth reading, because the nesting is syntactically
perfect. The only thing that distinguishes wired from stranded is running
the file and seeing what it registers.

Not every registration belongs at load. `setupCanvasEvents`,
`showImportDiaryModal` and the other on-demand setups wire their controls
when their view opens, which is correct. What is asserted here is the set
that the page ships with and must have working before the user touches
anything.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
HARNESS = Path(__file__).parent / "dashboard_dom_harness.js"
SCRIPT = ROOT / "src/desktop_app/dashboard/static/dashboard.js"
TEMPLATE = ROOT / "src/desktop_app/dashboard/templates/index.html"

#: Controls present in the template from the first paint. Each must be
#: listening before the user clicks anything, so any of them missing from
#: a load means its handler was stranded somewhere that never runs.
REQUIRED_AT_LOAD = frozenset({
    "activate-btn",
    "btn-optimise-topics",
    "btn-scrub-deflections",
    "chat-input",
    "chat-reset",
    "chat-send",
    "conn-add-btn",
    "conn-registry-refresh",
    "conn-registry-search",
    "from-date",
    "meals-from-date",
    "meals-to-date",
    "search-input",
    "set-save",
    "set-test",
    "to-date",
    # The three the incident stranded.
    "yolo-slider",
    "yolo-start",
    "yolo-stop",
})

node = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="needs node to load the dashboard script; CI runners have it",
)


@pytest.fixture(scope="module")
def load_result() -> dict:
    """Run dashboard.js against a stub DOM, once for the whole module."""
    proc = subprocess.run(
        [shutil.which("node") or "node", str(HARNESS), str(SCRIPT)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"harness failed: {proc.stderr[-2000:]}"
    return json.loads(proc.stdout)


@node
class TestTheScriptRuns:
    def test_it_loads_without_throwing(self, load_result: dict) -> None:
        assert load_result.get("error") is None, (
            f"dashboard.js threw while loading: {load_result['error']}. "
            "Every listener after the throw is unregistered, so the page "
            "is partly inert."
        )


@node
class TestEveryShippedControlIsWired:
    def test_all_of_them_register_on_load(self, load_result: dict) -> None:
        registered = {element for element, _event in load_result["listeners"]}
        stranded = sorted(REQUIRED_AT_LOAD - registered)
        assert not stranded, (
            f"these controls never had a listener attached: {stranded}. "
            "Their registration is somewhere that does not run at load - "
            "check whether it has been nested inside a function or a "
            "callback."
        )

    def test_the_yolo_controls_specifically(self, load_result: dict) -> None:
        """Named separately because this is the one that shipped broken."""
        registered = {element for element, _event in load_result["listeners"]}
        assert {"yolo-slider", "yolo-start", "yolo-stop"} <= registered


class TestTheRequiredListCannotRotIntoAssertingNothing:
    """A list of ids is only a guard while the ids still exist."""

    def test_every_required_control_exists_in_the_template(self) -> None:
        html = TEMPLATE.read_text(encoding="utf-8")
        missing = sorted(i for i in REQUIRED_AT_LOAD if f'id="{i}"' not in html)
        assert not missing, (
            f"REQUIRED_AT_LOAD names controls the template no longer has: "
            f"{missing}. Remove them, or the guard is asserting that absent "
            "elements are wired, which always passes."
        )

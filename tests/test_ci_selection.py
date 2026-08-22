"""What CI runs is a property worth asserting, not a thing to remember.

CI selects `unit and not needs_hardware`. Two ways that goes wrong
silently, both of which have already happened once:

- a test file carries no marker, so CI collects it and immediately
  deselects it, and nobody notices for months;
- the quarantine marker spreads, and good tests leave CI attached to it.

The second is the subtler one. The rule "nothing carries both markers"
was checked once, held, and was then broken by the very change that
relied on it: adding a module-level `unit` mark to files that already
carried per-test quarantine marks put eight passing tests outside CI.
A precondition established against the old tree does not survive the
new one, so it is asserted here instead of remembered.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

#: Every test held back from CI, and why. A test belongs here only if a
#: CI runner genuinely cannot run it; anything else is a test to fix.
QUARANTINE = {
    "tests/test_voice_listener.py::TestCrossPlatformAudioHealthWarning"
    "::test_health_warning_fires_on_linux": "needs a sound card",
}


def _collect(marker_expression: str) -> set[str]:
    """Test ids pytest would select for an expression."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--collect-only",
         "-m", marker_expression, "-p", "no:randomly"],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    return {
        line.strip() for line in result.stdout.splitlines()
        if "::" in line and not line.startswith(("ERROR", "FAILED"))
    }


class TestTheQuarantineIsExactlyWhatWeMeant:
    def test_only_the_listed_tests_are_held_back(self):
        held_back = _collect("unit and needs_hardware")

        assert held_back == set(QUARANTINE), (
            "the set of tests CI skips has drifted from the list above; "
            "add a reason or remove the marker"
        )

    def test_the_quarantine_marker_is_not_a_whole_file(self):
        """A module-level quarantine takes its whole file out of CI."""
        offenders = [
            path.relative_to(ROOT)
            for path in TESTS.rglob("test_*.py")
            if re.search(r"^pytestmark\s*=.*needs_hardware",
                         path.read_text(encoding="utf-8"), re.M)
        ]

        assert offenders == [], f"quarantine applied to whole files: {offenders}"


class TestEveryTestIsVisibleToCI:
    def test_no_test_file_is_left_unmarked(self):
        """An unmarked file is collected, deselected, and never noticed."""
        unmarked = [
            path.relative_to(ROOT)
            for path in sorted(TESTS.rglob("test_*.py"))
            if "mark.unit" not in path.read_text(encoding="utf-8")
            # Needs a live Ollama, and `addopts` excludes it anyway.
            and "performance" not in path.parts
        ]

        assert unmarked == [], f"these never run in CI: {unmarked}"

    def test_the_integration_marker_does_not_remove_a_test_from_ci(self):
        """`integration` means complex setup, not "cannot run here".

        Eight tests marked that way run in a container in under a second,
        and conflating the two markers is what dropped them.
        """
        also_integration = _collect("unit and integration")
        assert also_integration, "expected some unit tests to also be integration"

        assert also_integration <= _collect("unit and not needs_hardware")

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
import functools
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


@functools.lru_cache(maxsize=None)
def _collect(marker_expression: str) -> frozenset[str]:
    """Test ids pytest would select for an expression.

    Each call is a full collection in a subprocess, which is the most
    expensive thing this file does - a few seconds each, against a suite
    where most tests are measured in milliseconds. The assertions here
    ask three distinct questions but four times, so the answers are
    cached by expression and the repeat is free.

    A ``frozenset`` rather than a ``set`` because the result is now
    shared between tests: one of them mutating it would silently change
    what another asserts against.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--collect-only",
         "-m", marker_expression, "-p", "no:randomly"],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    return frozenset(
        line.strip() for line in result.stdout.splitlines()
        if "::" in line and not line.startswith(("ERROR", "FAILED"))
    )


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


#: How many tests the CI selector collects. A floor, not an equality: it
#: exists to catch tests *disappearing*, which is the failure that has
#: actually happened here twice (an unmarked file, then a marker that
#: spread), and an exact match would fail on every commit that adds one.
#:
#: Raise it when you add tests. Lowering it is the interesting act, and
#: should appear in a diff with a reason next to it.
#:
#: This replaces wall-clock as the health signal for this pipeline. Four
#: times in one branch a duration told us something was wrong when the
#: only thing wrong was the runner: the same tree finished in 116s and
#: was also killed at both a 10-minute and a 20-minute ceiling, and this
#: suite once took 2545s in a container that had just run it in 103s. A
#: sick runner cannot change how many tests exist, so a count says the
#: thing a duration was being asked to say and cannot be made flaky.
COLLECTED_FLOOR = 3119


class TestNoTestSilentlyDisappears:
    """The count is the signal; the clock never was.

    What this cannot catch is an equal number removed and added in one
    change. Nothing cheap catches that, and it is not the failure mode
    this pipeline has: both real incidents were tests vanishing from
    selection while the suite went on reporting success.
    """

    def test_ci_collects_at_least_the_recorded_number_of_tests(self):
        collected = _collect("unit and not needs_hardware")
        assert len(collected) >= COLLECTED_FLOOR, (
            f"CI collects {len(collected)} tests, down from "
            f"{COLLECTED_FLOOR}. {COLLECTED_FLOOR - len(collected)} test(s) "
            "stopped being selected. Either a file lost its marker, a "
            "marker spread to tests that should run, or tests were "
            "deleted. If the removal is deliberate, lower COLLECTED_FLOOR "
            "in the same commit and say why."
        )

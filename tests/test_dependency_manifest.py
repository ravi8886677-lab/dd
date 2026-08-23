"""What the install manifests promise, asserted rather than remembered.

Four separate install problems shared one cause: `requirements.txt` was
the place everything landed, whether or not the running assistant needed
it. A build tool, a test runner and a browser automation library were all
installed on every user's machine, and two pins kept the install on old
Python and on a C compiler.

These are structural assertions about the manifests, not version
assertions. They ask "does anything here exclude a currently supported
numpy", not "does this say numpy>=2", so a future bump does not have to
come back and edit the test that is supposed to be guarding it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "requirements.txt"
CHAT = ROOT / "requirements-chat.txt"
DEV = ROOT / "requirements-dev.txt"
BUILD = ROOT / "requirements-build.txt"


def _requirements(path: Path) -> dict[str, str]:
    """Map distribution name -> the full requirement line."""
    found: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~;\[ ]", line, 1)[0].strip().lower()
        if name:
            found[name] = line
    return found


class TestNothingCapsNumpyBelowTwo:
    """The ceiling is what pinned installs to old wheels and old Python."""

    @pytest.mark.parametrize("path", [RUNTIME, CHAT], ids=["runtime", "chat"])
    def test_no_manifest_excludes_numpy_2(self, path: Path) -> None:
        line = _requirements(path).get("numpy")
        if line is None:
            return
        excludes_2 = re.search(r"<\s*2(\.|\b)", line) or re.search(r"==\s*1\.", line)
        assert not excludes_2, (
            f"{path.name} pins numpy away from 2.x ({line!r}). Nothing in this "
            "codebase requires it: every audio buffer states its dtype "
            "explicitly, which is what test_numpy2_audio_dtypes.py checks."
        )


class TestTheRuntimeInstallCarriesOnlyRuntimeThings:
    """A user running the assistant should not be installing our toolchain."""

    def test_the_test_runner_is_not_a_runtime_dependency(self) -> None:
        runtime = _requirements(RUNTIME)
        for name in ("pytest", "pytest-repeat"):
            assert name not in runtime, (
                f"{name} is in requirements.txt. It belongs in "
                "requirements-dev.txt, which already lists it."
            )

    def test_the_test_runner_is_still_available_to_developers(self) -> None:
        dev = _requirements(DEV)
        for name in ("pytest", "pytest-repeat"):
            assert name in dev, f"{name} went missing from requirements-dev.txt"

    def test_the_build_tool_is_not_a_runtime_dependency(self) -> None:
        assert "pyinstaller" not in _requirements(RUNTIME), (
            "pyinstaller is in requirements.txt. Only someone producing a "
            "desktop bundle needs it; it belongs in requirements-build.txt."
        )

    def test_the_build_tool_is_still_available_to_packagers(self) -> None:
        assert BUILD.exists(), "requirements-build.txt is missing"
        assert "pyinstaller" in _requirements(BUILD)


class TestNothingIsInstalledThatIsNeverImported:
    """playwright was 160MB of browser automation nothing asked for."""

    def test_playwright_is_not_a_dependency(self) -> None:
        for path in (RUNTIME, CHAT, DEV):
            if not path.exists():
                continue
            assert "playwright" not in _requirements(path), (
                f"playwright is listed in {path.name} but imported nowhere in "
                "src/. Removing it is checked by the companion test below."
            )

    def test_playwright_is_genuinely_unused(self) -> None:
        """If someone starts importing it, this test says to add it back."""
        hits = [
            py
            for py in (ROOT / "src").rglob("*.py")
            if re.search(r"^\s*(import|from)\s+playwright", py.read_text(), re.M)
        ]
        assert not hits, (
            f"playwright is imported by {[str(p) for p in hits]}, so it must be "
            "declared as a dependency again."
        )


class TestVoiceActivityDetectionInstallsWithoutACompiler:
    """webrtcvad ships an sdist only; the wheels fork is the same code."""

    def test_the_prebuilt_distribution_is_the_one_requested(self) -> None:
        runtime = _requirements(RUNTIME)
        assert "webrtcvad" not in runtime, (
            "webrtcvad builds from source and needs a C toolchain, which is "
            "the install failure on a clean machine. Use webrtcvad-wheels."
        )
        assert "webrtcvad-wheels" in runtime

    def test_the_import_name_is_unchanged(self) -> None:
        """The fork is a packaging change, so the code must not know."""
        listener = (ROOT / "src/jarvis/listening/listener.py").read_text()
        assert "import webrtcvad" in listener
        assert "webrtcvad_wheels" not in listener


class TestEveryWorkflowInstallsWhatItRuns:
    """Splitting a manifest can strand the job that depended on the old one.

    Moving `pytest` out of `requirements.txt` is only correct if the job
    that runs pytest installs it from somewhere else. That is not visible
    from either file alone, which is exactly the shape of mistake that
    reaches CI as a red build on an unrelated commit.
    """

    WORKFLOWS = ROOT / ".github/workflows"

    def _workflow(self, name: str) -> str:
        return (self.WORKFLOWS / name).read_text()

    def test_the_test_job_installs_the_test_runner(self) -> None:
        body = self._workflow("tests.yml")
        assert "pytest" in body, "tests.yml does not run pytest any more?"
        assert "requirements-dev.txt" in body, (
            "tests.yml runs pytest but installs only requirements.txt, which "
            "no longer carries it. The job will fail on `No module named "
            "pytest`."
        )

    def test_the_desktop_build_installs_its_build_tool(self) -> None:
        body = self._workflow("build-desktop.yml")
        if "pyinstaller " not in body and "pyinstaller\n" not in body:
            return
        assert "requirements-build.txt" in body, (
            "build-desktop.yml runs PyInstaller but no longer installs it: "
            "requirements.txt does not carry it."
        )

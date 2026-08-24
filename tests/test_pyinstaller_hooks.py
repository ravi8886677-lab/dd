"""The desktop build's hooks, which no other test reaches.

A dependency swap broke the release build on all three platforms while
every test stayed green. `requirements.txt` moved from `webrtcvad` to
`webrtcvad-wheels`, the same source published as prebuilt wheels so a
clean machine needs no C compiler. The import name is identical, so
nothing at runtime and nothing in this suite noticed - but PyInstaller
ships a contrib hook whose single line is `copy_metadata('webrtcvad')`,
a lookup by *distribution* name, which now raises and aborts the build.

The lesson generalises past this package: an import name and a
distribution name are different identifiers, and packaging tools use the
second. Verifying `import webrtcvad` still worked was not the same as
verifying the rename was invisible.

These run without PyInstaller installed wherever they can, because it
lives in requirements-build.txt and CI's test job does not install it.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "hooks"
SPEC = ROOT / "jarvis_desktop.spec"

_HAS_PYINSTALLER = importlib.util.find_spec("PyInstaller") is not None


class TestOurHooksAreActuallyConsulted:
    """A hooks directory the spec does not name is silently ignored."""

    def test_the_spec_points_at_the_hooks_directory(self) -> None:
        spec = SPEC.read_text(encoding="utf-8")
        match = re.search(r"hookspath\s*=\s*\[([^\]]*)\]", spec)
        assert match, "the spec no longer sets hookspath"
        assert "hooks" in match.group(1), (
            "hookspath does not include the local hooks directory, so every "
            "file in hooks/ is ignored and PyInstaller's bundled hooks win. "
            "That is silent: the build fails somewhere else entirely."
        )

    def test_the_hooks_directory_exists_and_is_not_empty(self) -> None:
        assert HOOKS.is_dir(), "hooks/ is missing but the spec points at it"
        assert list(HOOKS.glob("hook-*.py")), "hooks/ contains no hooks"


class TestTheWebrtcvadHookSurvivesTheRename:
    """The specific breakage, pinned so a revert cannot reintroduce it."""

    HOOK = HOOKS / "hook-webrtcvad.py"

    def test_the_shadowing_hook_exists(self) -> None:
        assert self.HOOK.is_file(), (
            "hooks/hook-webrtcvad.py is gone, so PyInstaller's contrib hook "
            "applies again and looks up a distribution name that "
            "requirements.txt no longer installs."
        )

    def test_it_accepts_either_distribution_name(self) -> None:
        body = self.HOOK.read_text(encoding="utf-8")
        for distribution in ("webrtcvad-wheels", "webrtcvad"):
            assert distribution in body, (
                f"the hook does not mention {distribution!r}. It must work "
                "whichever of the two is installed, so that reverting the "
                "requirements change does not break the build in the other "
                "direction."
            )

    @pytest.mark.skipif(not _HAS_PYINSTALLER, reason="PyInstaller is in requirements-build.txt")
    def test_it_imports_where_the_bundled_hook_raises(self) -> None:
        """The actual failure was an exception at hook import time."""
        import runpy

        namespace = runpy.run_path(str(self.HOOK))
        assert "datas" in namespace, "the hook defines no datas"
        assert isinstance(namespace["datas"], list)


class TestEveryHookImportsCleanly:
    """A hook that raises aborts the whole build, not just itself."""

    @pytest.mark.skipif(not _HAS_PYINSTALLER, reason="PyInstaller is in requirements-build.txt")
    @pytest.mark.parametrize("hook", sorted(HOOKS.glob("hook-*.py")) if HOOKS.is_dir() else [])
    def test_the_hook_can_be_imported(self, hook: Path) -> None:
        import runpy

        try:
            runpy.run_path(str(hook))
        except Exception as exc:  # noqa: BLE001 - the failure is the point
            pytest.fail(
                f"{hook.name} raises {type(exc).__name__} on import: {exc}. "
                "PyInstaller aborts the entire build when a hook cannot be "
                "imported."
            )

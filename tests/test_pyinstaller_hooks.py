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


class TestTheDesktopBuildRunsBeforeARelease:
    """A check that only runs while publishing is not a check.

    `build-desktop.yml` is `workflow_call` only, and its sole caller was
    `release.yml`. So the desktop bundle was first built *during* a
    release, and v1.13.1 went public with no binaries because the failure
    could not surface any earlier. The Python tests above assert the hook
    files are right; they do not build anything, and nothing in this suite
    does. Only a workflow can answer whether the app still bundles.

    This is the same shape as the marker sweep: a check that never ran,
    reporting nothing while appearing to cover the ground.
    """

    WORKFLOWS = ROOT / ".github/workflows"

    def _load(self, name: str) -> dict:
        yaml = pytest.importorskip("yaml")
        return yaml.safe_load((self.WORKFLOWS / name).read_text(encoding="utf-8"))

    @staticmethod
    def _triggers(workflow: dict) -> dict:
        # PyYAML reads a bare `on:` key as the boolean True.
        return workflow.get("on") or workflow.get(True) or {}

    def test_something_builds_the_desktop_app_outside_the_release(self) -> None:
        callers = [
            path.name
            for path in self.WORKFLOWS.glob("*.yml")
            if "build-desktop.yml" in path.read_text(encoding="utf-8")
            and path.name != "build-desktop.yml"
        ]
        assert callers, "nothing calls build-desktop.yml at all"

        pre_merge = []
        for name in callers:
            triggers = self._triggers(self._load(name))
            if "pull_request" in triggers or "push" in triggers:
                pre_merge.append(name)

        assert pre_merge, (
            "build-desktop.yml is only reachable from workflows that publish. "
            f"Its callers are {callers}, none of which run on a pull request "
            "or a push, so the first time anyone learns the app still builds "
            "is while a release is going out."
        )

    def test_the_release_does_not_fire_on_a_merge(self) -> None:
        triggers = self._triggers(self._load("release.yml"))
        push = triggers.get("push") or {}
        branches = (push or {}).get("branches") or []
        assert "main" not in branches, (
            "release.yml fires on push to main, so merging a pull request "
            "publishes a version as a side effect. Two went out that way "
            "before this was noticed."
        )
        assert "workflow_dispatch" in triggers, (
            "release.yml has no manual trigger left, so there is now no way "
            "to publish at all."
        )


class TestNothingIrreversibleHappensBeforeTheBuilds:
    """A release attempt cannot be undone in place.

    `semantic-release` creates a tag and a public GitHub release. Neither
    can be withdrawn by a later job failing, so both must come after every
    reversible check has passed.

    v1.13.1 is what the other order looks like: it published first, the
    builds then failed, and every downstream guard behaved correctly -
    the artifact download refused to continue without binaries and failed
    the job, so the attach step never ran. None of that could un-publish
    a release that already existed. The defect was the ordering alone.

    On this infrastructure the argument is stronger than "a future bug
    might". A release builds four platforms, and runner cancellations have
    been frequent enough that at least one build drawing a sick runner is
    likely within a few releases. Under the old order that alone published
    an empty release, with nothing wrong in the code at all.
    """

    WORKFLOWS = ROOT / ".github/workflows"

    def _release(self) -> dict:
        yaml = pytest.importorskip("yaml")
        return yaml.safe_load((self.WORKFLOWS / "release.yml").read_text(encoding="utf-8"))

    def test_the_publishing_job_waits_for_the_builds(self) -> None:
        jobs = self._release()["jobs"]
        publish = jobs["release-main"]
        assert "build-stable" in (publish.get("needs") or []), (
            "release-main no longer waits for build-stable, so it can "
            "publish a release for binaries that were never built."
        )

    def test_the_publishing_job_does_not_run_when_a_build_failed(self) -> None:
        publish = self._release()["jobs"]["release-main"]
        condition = str(publish.get("if", ""))
        assert "always()" not in condition, (
            "release-main carries always(), so it runs even when a build "
            "failed. This job creates the tag and the public release."
        )
        assert "build-stable.result == 'success'" in condition, (
            "release-main does not require build-stable to have succeeded."
        )

    def test_the_version_is_worked_out_without_publishing(self) -> None:
        """The dry run is what makes building-before-publishing possible."""
        jobs = self._release()["jobs"]
        assert "next-version" in jobs, (
            "the dry-run job is gone; without it the version is unknown "
            "until something has already been published."
        )
        steps = jobs["next-version"]["steps"]
        commands = " ".join(str(step.get("run", "")) for step in steps)
        assert "--dry-run" in commands, (
            "next-version runs semantic-release without --dry-run, so the "
            "job that exists to publish nothing now publishes."
        )

    def test_the_builds_do_not_wait_on_a_published_release(self) -> None:
        build = self._release()["jobs"]["build-stable"]
        assert build.get("needs") == ["next-version"], (
            "build-stable depends on something other than the dry run. If "
            "that is the publishing job, the old order is back."
        )

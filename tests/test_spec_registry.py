"""The spec registry in CLAUDE.md must list every spec file, and vice versa.

`CLAUDE.md` requires a `*.spec.md` next to the code it describes, and keeps a
registry table so a reader can find the right one without walking the tree.
Nothing enforced that pairing, so modules shipped with no spec and the table
kept rows nobody could follow.

Both directions are defects, and they fail differently. A spec with no row is
invisible: the next person writes the code again rather than reading it. A row
with no spec is worse than invisible, because it promises a document that is
not there.

This is a structural check, not a quality one. It cannot tell whether a spec
is accurate, only whether it exists and is reachable.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CLAUDE_MD = _REPO_ROOT / "CLAUDE.md"

# Rows name the spec in backticks, as a repo-relative path.
_REGISTRY_ROW = re.compile(r"`(src/[^`]+\.spec\.md)`")


def _registered_specs() -> set:
    return set(_REGISTRY_ROW.findall(_CLAUDE_MD.read_text(encoding="utf-8")))


def _spec_files() -> set:
    return {
        str(p.relative_to(_REPO_ROOT)).replace("\\", "/")
        for p in (_REPO_ROOT / "src").rglob("*.spec.md")
    }


@pytest.mark.unit
def test_every_spec_file_has_a_registry_row():
    missing = sorted(_spec_files() - _registered_specs())
    assert not missing, (
        "These spec files are not in the CLAUDE.md registry table, so nobody "
        f"browsing it will find them: {missing}"
    )


@pytest.mark.unit
def test_every_registry_row_points_at_a_real_spec():
    dangling = sorted(_registered_specs() - _spec_files())
    assert not dangling, (
        "The CLAUDE.md registry promises these specs, but the files do not "
        f"exist: {dangling}"
    )


@pytest.mark.unit
def test_the_registry_is_not_empty():
    """Guards the regexes themselves.

    Both checks above are set differences, so a pattern that silently stops
    matching makes them pass while the registry is unread.
    """
    assert len(_registered_specs()) > 10
    assert len(_spec_files()) > 10
